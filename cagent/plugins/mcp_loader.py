"""MCP 加载器：从配置启动 MCP 服务器，拉取工具清单，注册到操作树。"""
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .tree import OperationTree
from .mcp_client import McpClient, McpClientManager


class McpPluginLoader:
    """加载 MCP 服务器，拉取工具清单，构建操作树。

    与 tools 类型的独立插件包不同，MCP 服务器配置在 agent.yaml 的 mcp.servers 中，
    不需要独立的 plugin.yaml / mcp.yaml 文件。
    """

    def __init__(self, mcp_config: Any):
        self._config = mcp_config
        self._manager = McpClientManager()

    def load_all(self, tree: OperationTree) -> Dict[str, McpClient]:
        """连接所有启用的 MCP 服务器，拉取工具，注册到操作树。

        返回 {group_name: McpClient} 映射。
        """
        clients = self._manager.connect_all(self._config)
        defaults = self._defaults_dict()
        max_tools = defaults.get("max_tools_per_server", 30)

        for group, client in clients.items():
            try:
                mcp_tools = client.list_tools()
            except Exception:
                continue  # 拉取失败跳过

            # 工具过滤
            server_name = self._find_server_name_by_group(group)
            server_cfg = self._get_server_config(server_name)
            mcp_tools = self._filter_tools(mcp_tools, server_cfg)

            # 截断
            if len(mcp_tools) > max_tools:
                mcp_tools = mcp_tools[:max_tools]

            # 生成临时 tool.yaml 并加载到操作树
            temp_yaml = self._build_temp_yaml(
                group, mcp_tools,
                auto_detail=defaults.get("auto_detail", True),
                server_name=server_name,
            )
            tree.load_plugin(temp_yaml)

        return clients

    def disconnect_all(self) -> None:
        """断开所有 MCP 连接。"""
        self._manager.disconnect_all()

    def _defaults_dict(self) -> dict:
        defaults = getattr(self._config, "defaults", None)
        if defaults is None:
            return {}
        return {
            "connect_timeout": defaults.connect_timeout,
            "call_timeout": defaults.call_timeout,
            "max_tools_per_server": defaults.max_tools_per_server,
            "auto_detail": defaults.auto_detail,
        }

    def _find_server_name_by_group(self, group: str) -> str:
        """根据 group 反查 server 名。"""
        servers = getattr(self._config, "servers", {})
        for name, cfg in servers.items():
            g = cfg.group or f"mcp_{name}"
            if g == group:
                return name
        return group

    def _get_server_config(self, server_name: str) -> Any:
        servers = getattr(self._config, "servers", {})
        return servers.get(server_name)

    def _filter_tools(self, tools: List[dict], server_cfg: Any) -> List[dict]:
        """工具过滤：include/exclude/rename。"""
        tool_filter = getattr(server_cfg, "tool_filter", None) if server_cfg else None
        if not tool_filter:
            return tools
        include = tool_filter.get("include", [])
        exclude = tool_filter.get("exclude", [])
        rename = tool_filter.get("rename", {})

        result = []
        for tool in tools:
            name = tool.get("name", "")
            if name in exclude:
                continue
            if include and name not in include:
                continue
            if name in rename:
                tool = {**tool, "name": rename[name]}
            result.append(tool)
        return result

    def _build_temp_yaml(
        self,
        group: str,
        mcp_tools: List[dict],
        auto_detail: bool = True,
        server_name: str = "",
    ) -> str:
        """将 MCP 工具列表转为临时 tool.yaml 文件，返回路径。"""
        tree_nodes = []
        for tool in mcp_tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            input_schema = tool.get("inputSchema", {})
            params = self._convert_schema(input_schema)
            detail = desc if auto_detail else desc[:120]
            tree_nodes.append({
                "name": name,
                "summary": desc[:120] if len(desc) > 120 else desc,
                "detail": detail,
                "execute": {
                    "mode": "mcp",
                    "handler": name,
                },
                "params": params,
            })

        yaml_data = {
            "plugin": group,
            "group": group,
            "summary": f"MCP 服务器: {server_name or group}",
            "detail": f"通过 MCP 协议连接 {server_name or group}，提供 {len(mcp_tools)} 个工具",
            "tree": tree_nodes,
        }

        # 写入临时文件
        fd, temp_path = tempfile.mkstemp(suffix=f"_{group}.yaml", prefix="mcp_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(yaml_data, f, allow_unicode=True, sort_keys=False)
        return temp_path

    def _convert_schema(self, json_schema: dict) -> dict:
        """将 JSON Schema 转为 tool.yaml 的 params 格式。"""
        params = {}
        props = json_schema.get("properties", {})
        required = set(json_schema.get("required", []))
        for name, prop in props.items():
            params[name] = {
                "type": prop.get("type", "string"),
                "required": name in required,
                "description": prop.get("description", ""),
            }
            default = prop.get("default")
            if default is not None:
                params[name]["default"] = default
        return params
