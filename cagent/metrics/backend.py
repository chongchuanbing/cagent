"""度量存储后端抽象与实现。

设计原则：
  - 抽象接口 MetricsBackend 统一 save/load 操作
  - 当前实现 JsonlMetricsBackend（本地 jsonl 文件）
  - 后续可实现 SqliteMetricsBackend，无缝替换
"""
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from .schema import TurnMetrics


class MetricsBackend(ABC):
    """度量存储后端抽象接口。"""

    @abstractmethod
    def save_turn(self, session_id: str, turn: TurnMetrics) -> None:
        """保存单次 turn 的度量数据。"""
        pass

    @abstractmethod
    def load_turns(self, session_id: str) -> List[TurnMetrics]:
        """加载指定 session 的所有 turn 度量。"""
        pass

    @abstractmethod
    def list_sessions(self) -> List[str]:
        """列出所有有度量数据的 session_id。"""
        pass


class JsonlMetricsBackend(MetricsBackend):
    """基于本地 jsonl 文件的度量存储实现。

    存储路径约定：
      <storage_root>/sessions/<session_id>/metrics.jsonl

    每行一个 TurnMetrics 的 JSON 序列化，追加写入。
    """

    def __init__(self, storage_root: str):
        """初始化。

        Args:
            storage_root: 存储根目录（通常是 .data）
        """
        self._root = Path(storage_root)

    def save_turn(self, session_id: str, turn: TurnMetrics) -> None:
        """追加写入 metrics.jsonl。"""
        session_dir = self._root / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        metrics_file = session_dir / "metrics.jsonl"
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(turn.model_dump_json() + "\n")

    def load_turns(self, session_id: str) -> List[TurnMetrics]:
        """从 metrics.jsonl 加载所有 turn。"""
        metrics_file = self._root / "sessions" / session_id / "metrics.jsonl"
        if not metrics_file.exists():
            return []
        turns: List[TurnMetrics] = []
        with open(metrics_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        turns.append(TurnMetrics.model_validate_json(line))
                    except Exception:
                        # 忽略损坏的行
                        pass
        return turns

    def list_sessions(self) -> List[str]:
        """扫描 sessions 目录，返回所有有 metrics.jsonl 的 session_id。"""
        sessions_dir = self._root / "sessions"
        if not sessions_dir.exists():
            return []
        result: List[str] = []
        for session_dir in sessions_dir.iterdir():
            if session_dir.is_dir():
                metrics_file = session_dir / "metrics.jsonl"
                if metrics_file.exists():
                    result.append(session_dir.name)
        return result
