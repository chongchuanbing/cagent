"""MemoryService：长期记忆的对外统一入口。

组合三件套：
- LongTermMemory（双区存储 + 召回）
- MemoryExtractor（run 结束提取候选，一次 LLM 调用含打标）
- TenuringManager（去重合并 / age 累计 / 晋升 / 淘汰）

供 AgentLoop 在 run 开始召回、结束沉淀；供 remember 工具做快速晋升。
所有行为参数（enabled / tenuring_threshold / 标签模式等）每次调用时
从 ConfigProvider 读取，支持配置热加载。
"""
from typing import Callable, List, Optional

from ..config.provider import ConfigProvider
from ..events import EventEmitter, EventType
from ..llm.base import LLMClient
from ..storage.base import StorageBackend
from ..utils import sanitize_text
from .extractor import MemoryExtractor
from .long_term import LongTermMemory
from .schema import MemoryRecord
from .tenuring import TenuringManager


class MemoryService:
    """长期记忆服务：召回 / 沉淀 / 快速晋升。"""

    def __init__(
        self,
        storage: StorageBackend,
        config: Optional[ConfigProvider] = None,
        emitter: Optional[EventEmitter] = None,
        namespace: Optional[str] = None,
        llm: Optional[LLMClient] = None,
        llm_factory: Optional[Callable[[], LLMClient]] = None,
    ):
        mcfg = self._memory_config(config)
        self.config = config
        self.emitter = emitter
        self.store = LongTermMemory(
            storage, namespace=namespace or mcfg.namespace, k=mcfg.recall_k
        )
        self.extractor = MemoryExtractor(llm=llm, config=config, llm_factory=llm_factory)
        self.tenuring = TenuringManager(self.store, config=config, emitter=emitter)

    @staticmethod
    def _memory_config(config: Optional[ConfigProvider]):
        if config is not None:
            return config.get_config().memory
        from ..config.schema import MemoryConfig

        return MemoryConfig()

    def _enabled(self) -> bool:
        if self.config is None:
            return True
        return self.config.get_config().memory.enabled

    # ---------- run 开始：召回 ----------

    def recall(
        self, goal: str, session_id: Optional[str] = None, k: Optional[int] = None
    ) -> List[MemoryRecord]:
        """召回与目标相关的长期记忆（默认只查老年代）。"""
        if not self._enabled():
            return []
        mcfg = self._memory_config(self.config)
        k = mcfg.recall_k if k is None else k
        if k <= 0:
            return []
        records = self.store.retrieve(
            goal, k=k, include_candidates=mcfg.recall_candidates
        )
        if records and self.emitter is not None:
            self.emitter.emit(
                EventType.MEMORY_RECALLED,
                payload={
                    "count": len(records),
                    "contents": [r.content for r in records],
                },
                session_id=session_id,
            )
        return records

    # ---------- run 结束：提取 + 晋升 ----------

    def absorb(
        self,
        goal: str,
        history: List[dict],
        answer: str,
        session_id: Optional[str] = None,
    ) -> List[MemoryRecord]:
        """提取本次 run 的候选记忆并走分代流转，返回本次晋升的记录。"""
        if not self._enabled():
            return []
        candidates = self.extractor.extract(goal, history, answer)
        if not candidates:
            return []
        return self.tenuring.absorb(candidates, session_id or "")

    # ---------- 快速晋升：remember 工具 ----------

    def remember(self, content: str, tags: Optional[dict] = None) -> MemoryRecord:
        """用户显式指定的记忆：清洗后直通老年代（不受 enabled 限制，尊重用户显式意图）。"""
        return self.tenuring.pin(sanitize_text(content), tags)
