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


class ImageRef(BaseModel):
    """图片引用：URL 或本地路径，用于 vision 模型的多模态输入。"""

    url: Optional[str] = None          # http(s) / data: URI
    path: Optional[str] = None         # 本地文件 → 读成 base64
    detail: str = "auto"               # openai: low/auto/high


class Message(BaseModel):
    """统一对话消息模型，贯穿 LLM / ReAct / memory 各层。"""

    role: MessageRole
    content: str = ""
    # LLM 返回的工具调用意图（OpenAI 风格）
    tool_calls: Optional[List[dict]] = None
    # 工具消息回写时关联的调用 id
    tool_call_id: Optional[str] = None
    # 图片输入（vision 模型序列化为多模态 content；非 vision 模型忽略并告警）
    images: List[ImageRef] = Field(default_factory=list)
