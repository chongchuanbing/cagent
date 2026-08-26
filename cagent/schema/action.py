"""ReAct 范式下的思考 / 行动 / 观察定义。"""
from typing import Optional

from pydantic import BaseModel, Field


class Thought(BaseModel):
    """ReAct 中的「思考」环节。"""

    content: str
    reasoning: str = ""


class Action(BaseModel):
    """ReAct 中的「行动」环节：一次工具调用意图。"""

    tool_name: str
    args: dict = Field(default_factory=dict)
    # 关联的 Thought id（用于回溯推理链）
    thought_ref: Optional[str] = None


class Observation(BaseModel):
    """ReAct 中的「观察」环节：工具返回的结果。"""

    action_ref: Optional[str] = None
    content: str
    ok: bool = True
