"""工具系统：Tool 基类与 @tool 装饰器。"""
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ToolResult:
    """工具统一返回。"""

    ok: bool
    content: str
    error: Optional[str] = None


class Tool(ABC):
    """工具基类。子类需定义 name / description 并实现 run。

    group 为工具所属分组（命名空间），用于按启用组裁剪工具面；
    default 组始终视为启用。
    """

    name: str = ""
    description: str = ""
    group: str = "default"

    @abstractmethod
    def run(self, **kwargs) -> ToolResult:
        """执行工具逻辑。"""
        raise NotImplementedError

    def schema(self) -> dict:
        """返回 OpenAI 风格 JSON Schema 片段，供 LLM 选参。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }


class FunctionTool(Tool):
    """由 @tool 装饰器包裹普通函数得到的具体实现。"""

    def __init__(self, func: Callable, name: str, description: str, group: str = "default"):
        self._func = func
        self.name = name
        self.description = description
        self.group = group

    def run(self, **kwargs) -> ToolResult:
        try:
            out = self._func(**kwargs)
            return ToolResult(ok=True, content=str(out))
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, content="", error=str(e))

    def schema(self) -> dict:
        sig = inspect.signature(self._func)
        props: Dict[str, dict] = {}
        required: List[str] = []
        for pname, param in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            props[pname] = {"type": "string", "description": ""}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }


def tool(name: Optional[str] = None, description: str = "", group: str = "default"):
    """装饰普通函数，将其注册为 Tool。

    >>> @tool(name="calculator", description="做加法", group="math")
    ... def add(a: int, b: int) -> int:
    ...     return a + b
    """

    def decorator(func: Callable) -> FunctionTool:
        return FunctionTool(
            func=func,
            name=name or func.__name__,
            description=description or (func.__doc__ or "").strip(),
            group=group,
        )

    return decorator
