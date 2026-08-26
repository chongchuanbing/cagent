"""晋升管理器：候选记忆的去重合并、age 累计、晋升与淘汰。

借鉴 JVM 分代晋升规则：
- 每次吸收（run 结束）相当于一次 Minor GC：新候选与既有候选做相似度匹配，
  同一记忆再次出现 → 合并并 age+1（按不同 session 计数，同会话重复不累计）；
- age 达到 tenuring_threshold（MaxTenuringThreshold）或 pinned（用户显式指定，
  相当于大对象直通老年代）→ 晋升为 tenured；
- 与老年代记录冲突（同主题不同内容）→ 新事实覆盖旧内容，保留 id 与 age；
- 候选超过 candidate_ttl_days 未复发 → 淘汰（新生代清理）。
"""
from datetime import datetime
from typing import List, Optional

from ..config.provider import ConfigProvider
from ..events import EventEmitter, EventType
from .long_term import LongTermMemory, similarity
from .schema import MemoryRecord, MemoryStatus


class TenuringManager:
    """管理候选记忆的分代流转。"""

    def __init__(
        self,
        store: LongTermMemory,
        config: Optional[ConfigProvider] = None,
        emitter: Optional[EventEmitter] = None,
    ):
        self.store = store
        self.config = config
        self.emitter = emitter

    def _cfg(self):
        if self.config is not None:
            return self.config.get_config().memory
        from ..config.schema import MemoryConfig

        return MemoryConfig()

    # ---------- 主流程 ----------

    def absorb(self, records: List[MemoryRecord], session_id: str) -> List[MemoryRecord]:
        """吸收一批新提取的候选记忆，返回本次晋升为长期记忆的记录。"""
        mcfg = self._cfg()
        # 先做新生代清理（长期未复发的候选淘汰）
        self.store.evict_stale(mcfg.candidate_ttl_days)

        promoted: List[MemoryRecord] = []
        for rec in records:
            merged = self._absorb_one(rec, session_id, mcfg)
            if (
                merged is not None
                and merged.status == MemoryStatus.CANDIDATE
                and (merged.pinned or merged.age >= mcfg.tenuring_threshold)
            ):
                self.store.migrate(merged, MemoryStatus.TENURED)
                promoted.append(merged)
                self._emit_promoted(merged)
        return promoted

    def _absorb_one(
        self, rec: MemoryRecord, session_id: str, mcfg
    ) -> Optional[MemoryRecord]:
        """吸收单条：冲突覆盖 / 合并累计 / 新建候选。"""
        threshold = mcfg.match_threshold

        # 1) 与老年代冲突：同主题不同内容 → 新事实覆盖旧内容（保留 id/age/hits）
        tenured_match = self._find_match(
            self.store.load(MemoryStatus.TENURED), rec.content, threshold
        )
        if tenured_match is not None:
            merged = tenured_match.model_copy(
                update={
                    "content": rec.content,
                    "tags": {**tenured_match.tags, **rec.tags},
                    "pinned": tenured_match.pinned or rec.pinned,
                    "updated_at": datetime.now(),
                }
            )
            self.store.upsert(merged)
            return None  # 已在老年代，无晋升动作

        # 2) 与既有候选匹配：同一记忆再次出现
        candidates = self.store.load(MemoryStatus.CANDIDATE)
        cand_match = self._find_match(candidates, rec.content, threshold)
        if cand_match is not None:
            sessions = list(cand_match.sessions)
            if session_id and session_id not in sessions:
                sessions.append(session_id)
            merged = cand_match.model_copy(
                update={
                    "content": rec.content,          # 内容以最新为准
                    "tags": {**cand_match.tags, **rec.tags},
                    "age": len(sessions) if sessions else cand_match.age,
                    "sessions": sessions,
                    "pinned": cand_match.pinned or rec.pinned,
                    "updated_at": datetime.now(),
                }
            )
            self.store.upsert(merged)
            return merged

        # 3) 全新候选
        fresh = rec.model_copy(
            update={
                "sessions": [session_id] if session_id else [],
                "age": 1,
                "status": MemoryStatus.CANDIDATE,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
            }
        )
        self.store.upsert(fresh)
        return fresh

    # ---------- 快速晋升（remember 工具路径） ----------

    def pin(self, content: str, tags: Optional[dict] = None) -> MemoryRecord:
        """用户显式指定的记忆：直通老年代（大对象直接晋升）。"""
        mcfg = self._cfg()
        threshold = mcfg.match_threshold

        tenured_match = self._find_match(
            self.store.load(MemoryStatus.TENURED), content, threshold
        )
        if tenured_match is not None:
            merged = tenured_match.model_copy(
                update={
                    "content": content,
                    "tags": {**tenured_match.tags, **(tags or {})},
                    "pinned": True,
                    "updated_at": datetime.now(),
                }
            )
            self.store.upsert(merged)
            return merged

        record = MemoryRecord(
            content=content,
            tags=tags or {},
            status=MemoryStatus.TENURED,
            pinned=True,
        )
        self.store.upsert(record)
        self._emit_promoted(record)
        return record

    # ---------- 内部 ----------

    def _find_match(
        self, records: List[MemoryRecord], content: str, threshold: float
    ) -> Optional[MemoryRecord]:
        """按 Jaccard 相似度找最相近的既有记录（纯关键词，无额外 LLM）。"""
        best, best_sim = None, 0.0
        for r in records:
            sim = similarity(r.content, content)
            if sim >= threshold and sim > best_sim:
                best, best_sim = r, sim
        return best

    def _emit_promoted(self, record: MemoryRecord) -> None:
        if self.emitter is not None:
            self.emitter.emit(
                EventType.MEMORY_PROMOTED,
                payload={
                    "id": record.id,
                    "content": record.content,
                    "tags": record.tags,
                    "age": record.age,
                    "pinned": record.pinned,
                },
            )
