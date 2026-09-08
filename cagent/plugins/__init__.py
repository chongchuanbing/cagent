"""插件系统：操作树 + 渐进式披露 + 插件加载 + MCP + Skills。"""
from .tree import OperationNode, OperationTree
from .guide import ToolGuideTool, RunToolTool
from .shell_exec import ShellExecutor
from .manager import PluginManager, PluginExecutor, ToolsPluginLoader
from .mcp_client import McpClient, McpClientManager
from .mcp_loader import McpPluginLoader
from .skill_loader import SkillDefinition, SkillsPluginLoader
from .skill_executor import SkillGuideTool, RunSkillTool, ReadSkillFileTool

__all__ = [
    "OperationNode",
    "OperationTree",
    "ToolGuideTool",
    "RunToolTool",
    "ShellExecutor",
    "PluginManager",
    "PluginExecutor",
    "ToolsPluginLoader",
    "McpClient",
    "McpClientManager",
    "McpPluginLoader",
    "SkillDefinition",
    "SkillsPluginLoader",
    "SkillGuideTool",
    "RunSkillTool",
    "ReadSkillFileTool",
]
