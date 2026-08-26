"""长期记忆的数据契约：分代记录 + 状态。

借鉴 JVM 分代设计：
- candidate（新生代/Survivor）：刚提取、尚未多次确认的候选记忆；
- tenured（老年代）：跨多个会话反复出现（age 达到阈值）或被用户显式
  指定（pinned）后晋升的长期记忆，跨会话召回。
"""
from datetime import datetime
from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, Field


class MemoryStatus(str, Enum):
    """记忆所处分代。"""

    CANDIDATE = "candidate"    # 新生代：候选，等待多次确认
    TENURED = "tenured"        # 老年代：已晋升的长期记忆


class MemoryRecord(BaseModel):
    """一条长期记忆。

    - age：出现过的**不同会话**数（同会话重复出现不累计），
      达到 tenuring_threshold 后晋升；
    - pinned：用户显式指定（remember 工具 / "记住这个"），直通晋升；
    - hits：被召回次数（后续淘汰机制的依据）；
    - tags：分类 -> 取值，分类体系见配置 memory.tags。
    """

    id: str = ""                    # 缺省按内容 hash 生成（同一内容天然去重）
    content: str
    status: MemoryStatus = MemoryStatus.CANDIDATE
    tags: Dict[str, str] = Field(default_factory=dict)
    age: int = 1
    sessions: List[str] = Field(default_factory=list)
    pinned: bool = False
    hits: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def model_post_init(self, __context) -> None:
        if not self.id:
            import hashlib

            object.__setattr__(
                self, "id", hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:12]
            )
