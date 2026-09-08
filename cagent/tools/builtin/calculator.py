"""一个最小内置工具示例，演示 @tool 用法。"""
from ..base import ToolResult, tool


@tool(name="add", description="对两个数字做加法，返回 a + b 的结果", group="math")
def add(a: int, b: int) -> int:
    """示例工具：返回 a + b。"""
    return a + b
