"""长期记忆：分代存储（candidate / tenured 双区），跨会话沉淀到本地存储。

借鉴 JVM 分代模型：
- `memory/long_term/<ns>/candidates.jsonl` —— 新生代：候选记忆，age 累计中；
- `memory/long_term/<ns>/tenured.jsonl` —— 老年代：多次确认或用户显式指定的记忆。

记录会随晋升 / 合并 / 冲突覆盖而变更，因此写入采用「全量重写分区文件」策略
（记忆条目数量级小，代价可忽略），不做 append-only。
召回打分 = 关键词重叠 + 标签命中加权，无需向量库。
"""
from datetime import datetime, timedelta
from typing import List, Optional

from ..storage.base import StorageBackend
from ..utils import tokenize
from .base import Memory
from .schema import MemoryRecord, MemoryStatus


def similarity(a: str, b: str) -> float:
    """Jaccard 相似度：判断两条记忆是否为「同一事实」再次出现。"""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class LongTermMemory(Memory):
    """跨会话长期记忆：双区存储 + 记录级读写 + 标签加权召回。"""

    def __init__(self, storage: StorageBackend, namespace: str = "default", k: int = 5):
        self.storage = storage
        self.namespace = namespace
        self.k = k
        self._base = f"memory/long_term/{namespace}"

    # ---------- 分区文件 ----------

    def _key(self, status: MemoryStatus) -> str:
        name = "tenured" if status == MemoryStatus.TENURED else "candidates"
        return f"{self._base}/{name}.jsonl"

    def load(self, status: MemoryStatus) -> List[MemoryRecord]:
        text = self.storage.read_text(self._key(status))
        if not text:
            return []
        records: List[MemoryRecord] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(MemoryRecord.model_validate_json(line))
            except Exception:  # pragma: no cover - 跳过损坏行
                continue
        return records

    def save(self, status: MemoryStatus, records: List[MemoryRecord]) -> None:
        lines = [r.model_dump_json() for r in records]
        self.storage.write_text(self._key(status), "\n".join(lines) + ("\n" if lines else ""))

    # ---------- 记录级操作 ----------

    def upsert(self, record: MemoryRecord) -> None:
        """按 id 写入所属分区（存在即替换，分区由 status 决定）。"""
        records = self.load(record.status)
        for i, r in enumerate(records):
            if r.id == record.id:
                records[i] = record
                break
        else:
            records.append(record)
        self.save(record.status, records)

    def migrate(self, record: MemoryRecord, to: MemoryStatus) -> None:
        """把记录迁移到另一分区（晋升用）：先从原分区移除，再写入目标分区。"""
        old = [r for r in self.load(record.status) if r.id != record.id]
        self.save(record.status, old)
        record.status = to
        # 目标分区可能已有同 id（异常场景），同样做替换
        self.upsert(record)

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        for status in MemoryStatus:
            for r in self.load(status):
                if r.id == record_id:
                    return r
        return None

    def remove(self, record_id: str) -> bool:
        for status in MemoryStatus:
            records = self.load(status)
            if any(r.id == record_id for r in records):
                self.save(status, [r for r in records if r.id != record_id])
                return True
        return False

    def list_all(self, status: Optional[MemoryStatus] = None) -> List[MemoryRecord]:
        statuses = [status] if status else list(MemoryStatus)
        out: List[MemoryRecord] = []
        for s in statuses:
            out.extend(self.load(s))
        return out

    def evict_stale(self, ttl_days: int, now: Optional[datetime] = None) -> int:
        """淘汰长期未复发的候选（新生代清理）；ttl_days <= 0 表示不过期。"""
        if ttl_days <= 0:
            return 0
        now = now or datetime.now()
        deadline = now - timedelta(days=ttl_days)
        records = self.load(MemoryStatus.CANDIDATE)
        kept = [r for r in records if r.updated_at >= deadline]
        evicted = len(records) - len(kept)
        if evicted:
            self.save(MemoryStatus.CANDIDATE, kept)
        return evicted

    # ---------- 召回 ----------

    def retrieve(self, query: str, k: int = None, include_candidates: bool = False) -> List[MemoryRecord]:
        """按「关键词重叠 + 标签命中加权」召回最相关的 k 条记忆。

        默认只召回老年代（tenured）；include_candidates 时合并候选区。
        """
        k = k or self.k
        pool = self.load(MemoryStatus.TENURED)
        if include_candidates:
            pool = pool + self.load(MemoryStatus.CANDIDATE)
        if not pool:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scored = []
        for idx, r in enumerate(pool):
            score = self._score(q_tokens, r)
            scored.append((score, idx, r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out: List[MemoryRecord] = []
        for score, _, r in scored[:k]:
            if score > 0:
                out.append(r)
        return out

    def add(self, message) -> None:
        """兼容 Memory 抽象：把任意可 str() 的对象作为 pinned 记忆直接晋升。"""
        content = getattr(message, "content", None) or str(message)
        record = MemoryRecord(content=content, status=MemoryStatus.TENURED, pinned=True)
        self.upsert(record)

    # ---------- 打分 ----------

    @staticmethod
    def _score(q_tokens: set, record: MemoryRecord) -> float:
        """召回打分：内容 token 重叠数 + 标签取值命中加权（标签命中 2.0/个）。"""
        c_tokens = tokenize(record.content)
        score = float(len(q_tokens & c_tokens))
        if record.tags:
            for value in record.tags.values():
                v_tokens = tokenize(value)
                if v_tokens and (q_tokens & v_tokens):
                    score += 2.0
        return score
