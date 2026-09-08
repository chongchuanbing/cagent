"""OpenAI 兼容 LLM 实现。"""
from typing import List

from openai import OpenAI

from ..schema.message import Message, MessageRole
from .base import LLMClient, LLMConfig, LLMResponse


class OpenAIClient(LLMClient):
    """基于 openai SDK 的实现，兼容任意 OpenAI 风格端点。"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    def _to_openai_messages(self, messages: List[Message]) -> List[dict]:
        out = []
        for m in messages:
            item: dict = {"role": m.role.value, "content": m.content}
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

    def complete(self, messages: List[Message]) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self.config.model,
            messages=self._to_openai_messages(messages),
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
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
        resp = self._client.chat.completions.create(
            model=self.config.model,
            messages=self._to_openai_messages(messages),
            tools=tools,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        choice = resp.choices[0]
        msg = choice.message
        return LLMResponse(
            content=msg.content or "",
            tool_calls=self._normalize_tool_calls(getattr(msg, "tool_calls", None)),
            finish_reason=getattr(choice, "finish_reason", None),
            usage=resp.usage.model_dump() if resp.usage else None,
            raw=resp,
        )
