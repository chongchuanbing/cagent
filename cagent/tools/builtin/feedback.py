"""向用户收集补充信息的反馈工具。

当模型判断目标信息不完整、需要用户确认或补充细节时，可调用 `ask_user`。
- 调用时会发布 `USER_INPUT_REQUEST` 事件（CLI/Web 端可据此渲染提问）；
- 实际输入通过注入的 `input_fn` 获取：CLI 读 stdin、Web 等待前端提交，核心库不关心实现。
"""
from typing import Callable, Optional

from ...events import EventEmitter, EventType
from ...utils import sanitize_text
from ..base import Tool, ToolResult

# 输入回调签名：接收问题文本，返回用户回答
InputFn = Callable[[str], str]


class FeedbackTool(Tool):
    """向用户提问，返回用户的回答。适用于信息不完整或需要确认的场景。"""

    name = "ask_user"
    description = (
        "向用户提问以获取必要的补充信息。"
        "当目标任务的信息不完整、存在歧义或需要用户确认时使用；"
        "参数 question 为要问的问题。返回用户的回答文本。"
    )
    group = "interaction"

    def __init__(
        self,
        emitter: Optional[EventEmitter] = None,
        input_fn: Optional[InputFn] = None,
    ):
        self._emitter = emitter
        self._input_fn = input_fn or self._default_input

    def run(self, question: str) -> ToolResult:
        # 先发事件，让展示层（CLI/Web）渲染问题
        if self._emitter is not None:
            self._emitter.emit(
                EventType.USER_INPUT_REQUEST, payload={"question": question}
            )
        try:
            answer = self._input_fn(question)
        except (EOFError, KeyboardInterrupt):
            return ToolResult(ok=False, content="", error="用户未提供输入")
        if answer is None or not str(answer).strip():
            return ToolResult(ok=False, content="", error="用户输入为空")
        # stdin 输入同样可能带 \udcXX 代理字符（非 UTF-8 终端），需清洗
        return ToolResult(ok=True, content=sanitize_text(str(answer).strip()))

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "需要用户补充或确认的问题",
                        }
                    },
                    "required": ["question"],
                },
            },
        }

    @staticmethod
    def _default_input(question: str) -> str:
        """默认实现：直接读 stdin（问题由 USER_INPUT_REQUEST 事件负责渲染）。"""
        return input()
