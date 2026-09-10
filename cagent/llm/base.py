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
    # —— 模型上下文上限（来自 models.json 的 maxInputTokens）——
    # 用于 history 窗口的 token 预算裁剪：history 部分占用不超过
    # max_input_tokens * history_token_budget_ratio，超出则淘汰最旧条目。
    max_input_tokens: Optional[int] = None
    # —— 多模型能力声明（带默认值，旧调用零改动）——
    tool_calling: bool = True
    vision: bool = False
    reasoning_enabled: bool = False
    reasoning_effort: Optional[str] = None
    reasoning_budget_tokens: Optional[int] = None
    reasoning_extra_body: dict = field(default_factory=dict)
    vendor: str = "openai"


@dataclass
class LLMResponse:
    """归一化的模型返回。"""

    content: str
    # OpenAI 风格工具调用列表
    tool_calls: List[dict] = field(default_factory=list)
    # API 返回的停止原因：stop/tool_calls(length)/content_filter 等
    # 用于 ReAct 引擎判断收敛状态（对标 AgentScope 的 GenerateReason）
    # 默认 None 兼容非 OpenAI 供应商
    finish_reason: Optional[str] = None
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
