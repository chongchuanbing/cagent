"""LLM 抽象层：解耦具体模型供应商。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from ..schema.message import Message


@dataclass
class LLMConfig:
    """LLM 连接配置。"""

    model: str = "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 2048
    data_dir: str = ".data"


@dataclass
class LLMResponse:
    """归一化的模型返回。"""

    content: str
    # OpenAI 风格工具调用列表
    tool_calls: List[dict] = field(default_factory=list)
    usage: Optional[dict] = None
    # 原始响应对象，便于调试
    raw: Optional[object] = None


class LLMClient(ABC):
    """所有 LLM 供应商实现需继承此抽象类。"""

    def __init__(self, config: LLMConfig):
        self.config = config

    @abstractmethod
    def complete(self, messages: List[Message]) -> LLMResponse:
        """纯文本补全。"""
        raise NotImplementedError

    @abstractmethod
    def complete_with_tools(self, messages: List[Message], tools: List[dict]) -> LLMResponse:
        """带工具声明的补全，可能返回 tool_calls。"""
        raise NotImplementedError
