"""对话消息与角色定义。"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """消息角色。继承 str 以便直接参与 JSON 序列化。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    """统一对话消息模型，贯穿 LLM / ReAct / memory 各层。"""

    role: MessageRole
    content: str = ""
    # LLM 返回的工具调用意图（OpenAI 风格）
    tool_calls: Optional[List[dict]] = None
    # 工具消息回写时关联的调用 id
    tool_call_id: Optional[str] = None
