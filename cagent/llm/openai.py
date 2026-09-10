"""OpenAI 兼容 LLM 实现。"""
import logging
from typing import List, Optional

from openai import OpenAI

from ..schema.message import ImageRef, Message, MessageRole
from .base import LLMClient, LLMConfig, LLMResponse
from .reasoning import build_reasoning_extra_body, effective_temperature

logger = logging.getLogger("cagent.llm.openai")


class OpenAIClient(LLMClient):
    """基于 openai SDK 的实现，兼容任意 OpenAI 风格端点。

    按 LLMConfig 的能力声明分支：
    - tool_calling=False → complete_with_tools 退化为不带 tools 的调用（兜底）；
    - vision=True → 序列化 Message.images 为多模态 content 块；
    - reasoning_enabled → 经 ReasoningAdapter 翻译思考参数（extra_body），并强制 temperature=0。
    """

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    @staticmethod
    def _resolve_image_url(img: ImageRef) -> str:
        if img.url:
            return img.url
        if img.path:
            import base64
            import mimetypes

            with open(img.path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            mime = mimetypes.guess_type(img.path)[0] or "image/png"
            return f"data:{mime};base64,{b64}"
        raise ValueError("ImageRef 必须提供 url 或 path")

    def _to_openai_messages(self, messages: List[Message]) -> List[dict]:
        out = []
        for m in messages:
            if m.images and self.config.vision:
                content = [{"type": "text", "text": m.content}]
                for img in m.images:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": self._resolve_image_url(img),
                                "detail": img.detail,
                            },
                        }
                    )
                item = {"role": m.role.value, "content": content}
            elif m.images and not self.config.vision:
                logger.warning(
                    "模型 %s 不支持 vision，已忽略 %d 张图片",
                    self.config.model,
                    len(m.images),
                )
                item = {"role": m.role.value, "content": m.content}
            else:
                item = {"role": m.role.value, "content": m.content}
            if m.tool_calls is not None:
                item["tool_calls"] = m.tool_calls
            if m.tool_call_id is not None:
                item["tool_call_id"] = m.tool_call_id
            out.append(item)
        return out

    @staticmethod
    def _normalize_tool_calls(tool_calls) -> List[dict]:
        """将 OpenAI SDK 的 tool_calls 对象统一为纯 dict 列表。"""
        if not tool_calls:
            return []
        out = []
        for tc in tool_calls:
            out.append(
                {
                    "id": getattr(tc, "id", None),
                    "type": getattr(tc, "type", "function"),
                    "function": {
                        "name": getattr(tc.function, "name", ""),
                        "arguments": getattr(tc.function, "arguments", "{}"),
                    },
                }
            )
        return out

    def _create(self, messages: List[Message], tools: Optional[List[dict]] = None):
        kwargs = dict(
            model=self.config.model,
            messages=self._to_openai_messages(messages),
            temperature=effective_temperature(self.config),
            max_tokens=self.config.max_tokens,
        )
        # tool_calling=False 时即便传入 tools 也退化为不带工具的调用（兜底）
        if tools is not None and self.config.tool_calling:
            kwargs["tools"] = tools
        rb = build_reasoning_extra_body(self.config)
        if rb:
            kwargs["extra_body"] = rb
        return self._client.chat.completions.create(**kwargs)

    def complete(self, messages: List[Message]) -> LLMResponse:
        resp = self._create(messages)
        choice = resp.choices[0]
        msg = choice.message
        return LLMResponse(
            content=msg.content or "",
            tool_calls=self._normalize_tool_calls(getattr(msg, "tool_calls", None)),
            finish_reason=getattr(choice, "finish_reason", None),
            usage=resp.usage.model_dump() if resp.usage else None,
            raw=resp,
        )

    def complete_with_tools(self, messages: List[Message], tools: List[dict]) -> LLMResponse:
        resp = self._create(messages, tools)
        choice = resp.choices[0]
        msg = choice.message
        return LLMResponse(
            content=msg.content or "",
            tool_calls=self._normalize_tool_calls(getattr(msg, "tool_calls", None)),
            finish_reason=getattr(choice, "finish_reason", None),
            usage=resp.usage.model_dump() if resp.usage else None,
            raw=resp,
        )
