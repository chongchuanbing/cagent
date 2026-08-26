"""渐进式披露工具：ToolGuideTool + RunToolTool。

两种工具配合实现多层按需披露：
  1. tool_guide — 查询操作树任意节点的说明（中间节点列子节点，叶子节点返回格式）
  2. run_tool — 执行叶子操作（支持 executor / shell / mcp 三种执行模式）
"""
from typing import Dict, Optional

from ..tools.base import Tool, ToolResult
from .tree import OperationTree, OperationNode
from .shell_exec import ShellExecutor
from .mcp_client import McpClientManager


class ToolGuideTool(Tool):
    """查询操作树任意节点的说明（渐进式披露入口）。"""

    name = "tool_guide"
    description = (
        "查询工具操作的格式说明。传入 path 查看该路径下的操作列表或具体操作的格式。"
        "例如：tool_guide(path='filesystem') 查看 filesystem 组下有哪些操作；"
        "tool_guide(path='filesystem.apply_patch') 查看 apply_patch 的完整格式。"
    )
    group = "default"

    def __init__(self, tree: OperationTree):
        self._tree = tree

    def run(self, path: str = "") -> ToolResult:
        if not path or not path.strip():
            # 无参数时列出所有可用组
            groups = self._tree.list_groups()
            if not groups:
                return ToolResult(ok=True, content="（暂无已加载的工具插件）")
            lines = ["可用工具组："]
            for g in groups:
                node = self._tree.query(g)
                summary = node.summary if node else ""
                lines.append(f"  - {g}: {summary}")
            lines.append("\n调用 tool_guide(path='组名') 查看该组下的操作。")
            return ToolResult(ok=True, content="\n".join(lines))

        node = self._tree.query(path)
        if node is None:
            available = ", ".join(self._tree.list_groups())
            return ToolResult(
                ok=False,
                content="",
                error=f"路径 '{path}' 不存在。可用组：{available}",
            )

        if node.is_branch:
            # 中间节点：列出子节点
            lines = [f"「{node.name}」下有以下操作："]
            for name, child in node.children.items():
                kind = "子组" if child.is_branch else "操作"
                lines.append(f"  - {name}（{kind}）: {child.summary}")
            lines.append(
                f"\n调用 tool_guide(path='{node.path}.子操作名') 查看具体格式。"
            )
            return ToolResult(ok=True, content="\n".join(lines))
        else:
            # 叶子节点：返回完整格式
            detail = node.detail or node.summary or "（无详细说明）"
            # 附带参数提示
            if node.params:
                param_lines = ["\n参数："]
                for pname, pschema in node.params.items():
                    req = "必填" if pschema.get("required") else "可选"
                    default = pschema.get("default")
                    default_str = f"，默认 {default!r}" if default is not None else ""
                    desc = pschema.get("description", "")
                    param_lines.append(
                        f"  - {pname}（{req}{default_str}）: {desc}"
                    )
                detail += "\n".join(param_lines)
            # 执行方式提示
            if node.execute:
                mode = node.execute.get("mode", "executor")
                if mode == "shell":
                    tmpl = node.execute.get("command_template")
                    if tmpl:
                        detail += f"\n执行方式：shell 模板，框架将拼命令：{tmpl}"
                    else:
                        detail += "\n执行方式：shell，请生成完整命令传入 command 参数"
                elif mode == "mcp":
                    detail += "\n执行方式：MCP 协议转发"
                else:
                    detail += "\n执行方式：框架代码执行"
                detail += f"\n调用 run_tool(path='{node.path}', params={{...}}) 执行。"
            return ToolResult(ok=True, content=detail)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "操作路径，如 'filesystem' 或 'filesystem.apply_patch'。"
                                "不传则列出所有可用组。"
                            ),
                        }
                    },
                    "required": [],
                },
            },
        }


class RunToolTool(Tool):
    """执行工具操作。"""

    name = "run_tool"
    description = (
        "执行工具操作。先调用 tool_guide 查看操作格式，再调用本工具执行。"
        "传入 path 指定操作路径，params 传入操作参数。"
    )
    group = "default"

    def __init__(
        self,
        tree: OperationTree,
        executors: Optional[Dict[str, "PluginExecutor"]] = None,
        shell_executor: Optional[ShellExecutor] = None,
        mcp_manager: Optional[McpClientManager] = None,
    ):
        self._tree = tree
        self._executors = executors or {}
        self._shell = shell_executor
        self._mcp = mcp_manager

    def set_executors(self, executors: dict) -> None:
        self._executors = executors

    def set_shell_executor(self, shell: ShellExecutor) -> None:
        self._shell = shell

    def set_mcp_manager(self, mcp: McpClientManager) -> None:
        self._mcp = mcp

    def run(self, path: str, params: Optional[dict] = None) -> ToolResult:
        node = self._tree.query(path)
        if node is None:
            return ToolResult(
                ok=False, content="", error=f"操作路径 '{path}' 不存在"
            )
        if not node.is_leaf:
            return ToolResult(
                ok=False,
                content="",
                error=f"'{path}' 不是可执行操作（是子组），请调 tool_guide 查看其子操作",
            )

        # 参数校验
        params = self._validate_params(node, params or {})
        if params is None:
            return ToolResult(ok=False, content="", error="参数校验失败")

        # 按 execute.mode 分发
        mode = node.execute.get("mode", "executor") if node.execute else "executor"
        if mode == "executor":
            return self._exec_executor(node, params)
        elif mode == "shell":
            return self._exec_shell(node, params)
        elif mode == "mcp":
            return self._exec_mcp(node, params)
        else:
            return ToolResult(
                ok=False, content="", error=f"未知执行模式: {mode}"
            )

    def _validate_params(self, node: OperationNode, params: dict) -> Optional[dict]:
        """参数校验：required 检查 + default 填充。"""
        result = {}
        for pname, pschema in node.params.items():
            if pschema.get("required") and pname not in params:
                # required 参数缺失
                return None
            if pname in params:
                result[pname] = params[pname]
            elif "default" in pschema:
                result[pname] = pschema["default"]
        # 合并额外参数（框架执行可能需要 kwargs）
        for k, v in params.items():
            if k not in result:
                result[k] = v
        return result

    def _exec_executor(self, node: OperationNode, params: dict) -> ToolResult:
        """框架代码执行模式。"""
        plugin_name = node.path.split(".")[0]
        handler_name = node.execute.get("handler")
        if not handler_name:
            return ToolResult(ok=False, content="", error=f"操作 '{node.path}' 未声明 handler")
        executor = self._executors.get(plugin_name)
        if executor is None:
            return ToolResult(
                ok=False, content="", error=f"插件 '{plugin_name}' 未加载"
            )
        func = executor.get(handler_name)
        if func is None:
            return ToolResult(
                ok=False, content="",
                error=f"插件 '{plugin_name}' 未导出执行函数 '{handler_name}'",
            )
        try:
            return func(**params)
        except Exception as e:
            return ToolResult(ok=False, content="", error=f"执行异常: {e}")

    def _exec_shell(self, node: OperationNode, params: dict) -> ToolResult:
        """shell 驱动模式。"""
        if self._shell is None:
            return ToolResult(ok=False, content="", error="shell 执行器未配置")
        template = node.execute.get("command_template")
        if template:
            # 模板模式：框架拼命令
            command = self._shell.render_command(template, params)
            timeout = params.get("timeout")
            cwd = params.get("cwd")
        else:
            # 完整命令模式：模型生成命令
            command = params.get("command", "")
            timeout = params.get("timeout")
            cwd = params.get("cwd")
        if not command:
            return ToolResult(ok=False, content="", error="命令为空")
        return self._shell.execute(command, timeout=timeout, cwd=cwd)

    def _exec_mcp(self, node: OperationNode, params: dict) -> ToolResult:
        """MCP 协议执行模式：转发调用给 MCP 服务器。"""
        if self._mcp is None:
            return ToolResult(ok=False, content="", error="MCP 客户端管理器未配置")
        group = node.path.split(".")[0]
        client = self._mcp.get(group)
        if client is None:
            return ToolResult(
                ok=False, content="", error=f"MCP 服务器 '{group}' 未连接"
            )
        tool_name = node.execute.get("handler")
        if not tool_name:
            return ToolResult(ok=False, content="", error=f"操作 '{node.path}' 未声明 handler")
        try:
            result = client.call_tool(tool_name, params)
            return ToolResult(ok=True, content=result)
        except Exception as e:
            return ToolResult(ok=False, content="", error=f"MCP 调用失败: {e}")

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "操作路径，如 'filesystem.apply_patch'",
                        },
                        "params": {
                            "type": "object",
                            "description": "操作参数，格式参见 tool_guide 的返回说明",
                        },
                    },
                    "required": ["path"],
                },
            },
        }
