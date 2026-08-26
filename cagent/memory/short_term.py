"""短期记忆：当前任务的上下文窗口。"""
from typing import List

from ..schema.message import Message
from .base import Memory


class ShortTermMemory(Memory):
    """保存当前任务对话轨迹，可整体拼回 messages。"""

    def __init__(self, max_turns: int = 50):
        self._items: List[Message] = []
        self.max_turns = max_turns

    def add(self, message: Message) -> None:
        self._items.append(message)
        if len(self._items) > self.max_turns:
            self._items = self._items[-self.max_turns:]

    def get_context(self) -> List[Message]:
        return list(self._items)

    def retrieve(self, query: str, k: int = 5) -> List[Message]:  # noqa: ARG002
        # 短期记忆不做语义检索，直接返回最近 k 条
        return self._items[-k:]

    def clear(self) -> None:
        self._items.clear()
