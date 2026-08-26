"""长期记忆快速晋升工具（remember）。

当用户在对话中显式要求记住某事（如「记住我喜欢简洁的回答」）时，
模型调用本工具将该事实直通晋升为长期记忆（相当于大对象直接进入老年代）：
- 晋升时由 TenuringManager 发布 MEMORY_PROMOTED 事件（CLI/Web 端可据此提示）；
- 实际写入通过注入的 MemoryService 完成，核心库不关心存储实现。
"""
from typing import Optional

from ...memory.service import MemoryService
from ..base import Tool, ToolResult


class RememberTool(Tool):
    """把用户显式指定的事实写入长期记忆（快速晋升）。"""

    name = "remember"
    description = (
        "把用户明确要求记住的信息保存为长期记忆（跨会话生效）。"
        "仅当用户显式表达「记住/以后记住/帮我记下」等意图时调用；"
        "参数 content 为要记住的事实，tags 为可选的标签对象（如 {\"主题\": \"偏好\"}）。"
    )
    group = "memory"

    def __init__(self, memory: Optional[MemoryService] = None):
        self._memory = memory

    def set_memory(self, memory: MemoryService) -> None:
        """延迟注入记忆服务（Agent 构造完成后绑定）。"""
        self._memory = memory

    def run(self, content: str, tags: Optional[dict] = None) -> ToolResult:
        if self._memory is None:
            return ToolResult(ok=False, content="", error="长期记忆服务不可用")
        if not content or not str(content).strip():
            return ToolResult(ok=False, content="", error="记忆内容为空")
        record = self._memory.remember(str(content).strip(), tags or {})
        return ToolResult(
            ok=True,
            content=f"已记住（长期记忆 #{record.id}）：{record.content}",
        )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "要长期记住的事实内容",
                        },
                        "tags": {
                            "type": "object",
                            "description": "可选标签，如 {\"主题\": \"偏好\"}",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["content"],
                },
            },
        }
