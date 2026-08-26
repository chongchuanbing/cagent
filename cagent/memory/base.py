"""记忆抽象层。"""
from abc import ABC, abstractmethod
from typing import List

from ..schema.message import Message


class Memory(ABC):
    """记忆接口：短期 / 长期记忆统一抽象。"""

    @abstractmethod
    def add(self, message: Message) -> None:
        raise NotImplementedError

    @abstractmethod
    def retrieve(self, query: str, k: int = 5) -> List[Message]:
        """按查询召回相关记忆。"""
        raise NotImplementedError
