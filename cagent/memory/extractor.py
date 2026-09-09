"""记忆提取器：run 结束时从执行素材中提取候选记忆并打标（一次 LLM 调用完成）。

相当于分代模型中的 Minor GC：
- 无 LLM 时跳过提取（返回空列表，不阻塞主流程）；
- 标签模式（implicit / explicit / hybrid / off）由配置决定，提示词可覆盖；
- 输出 JSON 容错解析（兼容 ```json 代码块）。
"""
import json
from typing import Callable, List, Optional

from ..llm.base import LLMClient
from ..config.provider import ConfigProvider
from ..prompts.memory_prompt import (
    MEMORY_EXTRACT_SYSTEM_PROMPT,
    build_memory_extract_user_prompt,
    build_tags_instruction,
)
from ..schema.message import Message, MessageRole
from ..utils import sanitize_text
from ..utils.logging import get_logger
from .schema import MemoryRecord

logger = get_logger(__name__)


def _parse_json_array(text: str) -> list:
    """从 LLM 文本中提取 JSON 数组（兼容 ```json 代码块与多余文本）。"""
    text = text.strip()
    if "```" in text:
        block = text.split("```")[1]
        if block.lower().startswith("json"):
            block = block[4:]
        text = block.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


class MemoryExtractor:
    """从 run 素材提取候选记忆（含打标与 pinned 识别）。"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        config: Optional[ConfigProvider] = None,
        llm_factory: Optional[Callable[[], LLMClient]] = None,
    ):
        self._llm = llm
        self._llm_factory = llm_factory
        self.config = config

    def _resolve_llm(self) -> Optional[LLMClient]:
        if self._llm is not None:
            return self._llm
        if self._llm_factory is None:
            return None
        try:
            return self._llm_factory()
        except Exception as e:  # noqa: BLE001 —— 无可用 LLM 时跳过提取
            logger.warning(f"LLM factory failed: {type(e).__name__}: {e}")
            return None

    def _cfg(self):
        """当前 memory 配置（每次调用读取，支持热加载）。"""
        if self.config is not None:
            return self.config.get_config().memory
        from ..config.schema import MemoryConfig

        return MemoryConfig()

    def extract(self, goal: str, history: List[dict], answer: str) -> List[MemoryRecord]:
        """提取候选记忆；无 LLM 或调用失败时返回空列表。"""
        llm = self._resolve_llm()
        if llm is None:
            return []
        mcfg = self._cfg()
        tags_cfg = mcfg.tags

        system = MEMORY_EXTRACT_SYSTEM_PROMPT
        if self.config is not None:
            system = self.config.get_prompt(
                "memory_extract", default=MEMORY_EXTRACT_SYSTEM_PROMPT
            )
        try:
            system = system.format(
                tags_instruction=build_tags_instruction(
                    tags_cfg.mode,
                    {k: v.model_dump() for k, v in tags_cfg.categories.items()},
                    tags_cfg.implicit_max_tags,
                )
            )
        except (KeyError, IndexError):
            pass

        user = build_memory_extract_user_prompt(goal, history, answer)
        try:
            resp = llm.complete([
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=user),
            ])
        except Exception:  # noqa: BLE001 —— 提取失败不影响主流程
            return []

        records: List[MemoryRecord] = []
        for item in _parse_json_array(resp.content or ""):
            if not isinstance(item, dict):
                continue
            content = sanitize_text(str(item.get("content", "")).strip())
            if not content:
                continue
            tags = self._validate_tags(item.get("tags") or {}, tags_cfg)
            records.append(
                MemoryRecord(
                    content=content,
                    tags=tags,
                    pinned=bool(item.get("pinned", False)),
                )
            )
        return records

    @staticmethod
    def _validate_tags(tags: dict, tags_cfg) -> dict:
        """按标签模式清洗 LLM 输出的标签。

        explicit：只保留 schema 中枚举分类，取值必须在合法列表内；
        hybrid：固定分类同样校验，额外自由标签放行；
        implicit / off：原样保留（off 丢弃全部）。
        """
        if not isinstance(tags, dict) or tags_cfg.mode == "off":
            return {}
        fixed = {k: set(v.values) for k, v in tags_cfg.categories.items()}
        out: dict = {}
        for key, value in tags.items():
            key, value = str(key).strip(), str(value).strip()
            if not key or not value:
                continue
            if key in fixed and value not in fixed[key]:
                continue  # 显式分类取值非法 → 丢弃
            if key not in fixed and tags_cfg.mode == "explicit":
                continue  # explicit 模式不允许固定分类之外的标签（hybrid 放行）
            out[key] = value
        return out
