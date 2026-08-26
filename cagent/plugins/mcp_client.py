"""MCP 客户端：连接 MCP 服务器、拉取工具清单、转发调用。

支持 stdio 和 SSE 两种传输方式。
MCP 协议基于 JSON-RPC 2.0 over stdio/SSE。
"""
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional


class McpClient:
    """单个 MCP 服务器的客户端连接。

    生命周期：
      connect() → list_tools() / call_tool() → disconnect()
    """

    def __init__(self, name: str, config: dict, defaults: Optional[dict] = None):
        self._name = name
        self._config = config
        self._defaults = defaults or {}
        self._transport = config.get("transport", "stdio")
        self._process: Optional[subprocess.Popen] = None
        self._connected = False
        self._request_id = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """启动 MCP 服务器进程并完成握手。"""
        if self._transport == "stdio":
            self._connect_stdio()
        elif self._transport == "sse":
            self._connect_sse()
        else:
            raise ValueError(f"不支持的传输方式: {self._transport}")

    def _connect_stdio(self) -> None:
        """stdio 模式：启动子进程。"""
        command = self._config.get("command")
        if not command:
            raise ValueError(f"MCP 服务器 '{self._name}' 缺少 command 配置")
        args = self._config.get("args", [])
        env = {**os.environ}
        # 环境变量展开 ${ENV}
        for key, value in self._config.get("env", {}).items():
            env[key] = os.path.expandvars(value)
        timeout = self._defaults.get("connect_timeout", 30)
        try:
            self._process = subprocess.Popen(
                [command] + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise ConnectionError(f"无法启动 MCP 服务器 '{self._name}': {e}")

        # MCP 握手（initialize）
        try:
            self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cagent", "version": "1.0"},
            }, timeout=timeout)
            # 发送 initialized 通知
            self._send_notification("notifications/initialized", {})
            self._connected = True
        except Exception as e:
            self._kill_process()
            raise ConnectionError(f"MCP 服务器 '{self._name}' 握手失败: {e}")

    def _connect_sse(self) -> None:
        """SSE 模式（二期实现，当前抛出未实现异常）。"""
        raise NotImplementedError("SSE 传输模式尚未实现，请使用 stdio")

    def list_tools(self) -> List[dict]:
        """拉取服务器提供的工具清单。"""
        if not self._connected:
            raise RuntimeError(f"MCP 服务器 '{self._name}' 未连接")
        timeout = self._defaults.get("call_timeout", 60)
        resp = self._send_request("tools/list", {}, timeout=timeout)
        return resp.get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """转发工具调用给 MCP 服务器。"""
        if not self._connected:
            raise RuntimeError(f"MCP 服务器 '{self._name}' 未连接")
        timeout = self._defaults.get("call_timeout", 60)
        resp = self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        }, timeout=timeout)
        # MCP 返回 content 数组
        contents = resp.get("content", [])
        texts = [c.get("text", "") for c in contents if c.get("type") == "text"]
        return "\n".join(texts) if texts else str(resp)

    def disconnect(self) -> None:
        """关闭连接。"""
        self._kill_process()
        self._connected = False

    def _kill_process(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def _send_request(self, method: str, params: dict, timeout: int = 30) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        self._request_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        data = json.dumps(req) + "\n"
        if self._process and self._process.stdin:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        else:
            raise RuntimeError("进程未启动或 stdin 不可用")

        # 读取响应行（跳过非 JSON-RPC 的日志行）
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._process.stdout.readline()
            if not line:
                raise TimeoutError(f"MCP 服务器 '{self._name}' 响应超时")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过非 JSON 的日志行
            if resp.get("id") == self._request_id:
                if "error" in resp:
                    error = resp["error"]
                    raise RuntimeError(
                        f"MCP 错误 [{error.get('code', '?')}]: {error.get('message', '')}"
                    )
                return resp.get("result", {})
        raise TimeoutError(f"MCP 服务器 '{self._name}' 响应超时")

    def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（不等待响应）。"""
        notif = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        data = json.dumps(notif) + "\n"
        if self._process and self._process.stdin:
            self._process.stdin.write(data)
            self._process.stdin.flush()


class McpClientManager:
    """管理多个 MCP 客户端的连接和断开。"""

    def __init__(self):
        self._clients: Dict[str, McpClient] = {}
        self._defaults: dict = {}

    def connect_all(self, mcp_config: Any) -> Dict[str, McpClient]:
        """按配置连接所有启用的 MCP 服务器。

        返回成功连接的 {group_name: McpClient} 映射。
        连接失败的服务器被跳过，不阻塞其他服务器。
        """
        # 先断开已有连接
        self.disconnect_all()

        # 读取全局默认配置
        defaults = getattr(mcp_config, "defaults", None)
        if defaults is not None:
            self._defaults = {
                "connect_timeout": defaults.connect_timeout,
                "call_timeout": defaults.call_timeout,
                "max_tools_per_server": defaults.max_tools_per_server,
                "auto_detail": defaults.auto_detail,
            }
        else:
            self._defaults = {}

        servers = getattr(mcp_config, "servers", {})
        for server_name, server_cfg in servers.items():
            if not server_cfg.enabled:
                continue
            config = {
                "transport": server_cfg.transport,
                "command": server_cfg.command,
                "args": server_cfg.args,
                "env": server_cfg.env,
                "url": server_cfg.url,
            }
            client = McpClient(server_name, config, self._defaults)
            try:
                client.connect()
                group = server_cfg.group or f"mcp_{server_name}"
                self._clients[group] = client
            except Exception:
                # 连接失败不阻塞其他服务器
                pass

        return dict(self._clients)

    def get(self, group: str) -> Optional[McpClient]:
        """按 group 名获取客户端。"""
        return self._clients.get(group)

    def disconnect_all(self) -> None:
        """断开所有 MCP 连接。"""
        for client in self._clients.values():
            try:
                client.disconnect()
            except Exception:
                pass
        self._clients.clear()

    @property
    def connected_groups(self) -> List[str]:
        return list(self._clients.keys())
