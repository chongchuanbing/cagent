"""插件管理器：扫描插件目录，按类型分发给加载器。"""
import importlib.util
import os
import sys
from typing import Dict, List, Optional, Tuple

import yaml

from .tree import OperationTree


class PluginExecutor:
    """从插件包加载执行函数。

    executor.py 中的函数签名：
      - 与 tool.yaml 中 handler 名对应
      - 可选导出 configure(config: dict) 用于注入配置
    """

    def __init__(self, plugin_name: str, executor_path: str, config: Optional[dict] = None):
        self._plugin_name = plugin_name
        self._module = self._load_module(executor_path)
        self._config = config or {}
        # 注入配置
        configure = getattr(self._module, "configure", None)
        if callable(configure):
            configure(self._config)

    def _load_module(self, path: str):
        """从文件路径加载 Python 模块。"""
        spec_name = f"cagent_plugin_{self._plugin_name}"
        spec = importlib.util.spec_from_file_location(spec_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载执行器: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec_name] = module
        spec.loader.exec_module(module)
        return module

    def get(self, handler_name: str):
        """获取执行函数。"""
        func = getattr(self._module, handler_name, None)
        if func is None or not callable(func):
            return None
        return func


class ToolsPluginLoader:
    """tools 类型插件加载器。"""

    def __init__(self, plugin_dir: str, meta: dict, config: dict):
        self.dir = plugin_dir
        self.meta = meta
        self.config = config

    def load(self) -> Tuple[List[str], Optional[PluginExecutor]]:
        """加载插件，返回 (操作组列表, 执行器)。

        返回的组列表用于注册到 OperationTree；
        执行器可能为 None（shell 驱动插件不需要 executor.py）。
        """
        yaml_path = os.path.join(self.dir, "tool.yaml")
        if not os.path.exists(yaml_path):
            raise FileNotFoundError(f"插件缺少 tool.yaml: {self.dir}")
        # 执行器（可选）
        executor = None
        executor_path = os.path.join(self.dir, "executor.py")
        if os.path.exists(executor_path):
            executor = PluginExecutor(
                self.meta["plugin"], executor_path, self.config.get("config", {})
            )
        return [yaml_path], executor


class PluginManager:
    """插件管理器：扫描目录，按类型加载插件。"""

    LOADERS = {
        "tools": ToolsPluginLoader,
        # "mcp": McpPluginLoader,          # 二期
        # "browser": BrowserPluginLoader,  # 二期
    }

    def __init__(self, plugins_dir: str, config: Optional[dict] = None):
        self._plugins_dir = plugins_dir
        self._config = config or {}

    def load_all(self) -> dict:
        """加载所有已启用插件。

        返回包含以下 key 的 dict：
          - tree: OperationTree
          - executors: {plugin_name: PluginExecutor}
          - shell_config: shell 插件的配置（用于 ShellExecutor）
          - loaded_plugins: [plugin_name, ...]
        """
        tree = OperationTree()
        executors = {}
        shell_config = {}
        loaded = []

        for plugin_dir in self._scan_plugin_dirs():
            meta = self._read_plugin_meta(plugin_dir)
            if meta is None:
                continue
            plugin_name = meta.get("plugin")
            plugin_type = meta.get("type", "tools")
            loader_cls = self.LOADERS.get(plugin_type)
            if loader_cls is None:
                continue
            # 检查是否启用
            plugin_config = self._config.get(plugin_name, {})
            if not plugin_config.get("enabled", True):
                continue
            # 加载
            loader = loader_cls(plugin_dir, meta, plugin_config)
            yaml_paths, executor = loader.load()
            for yp in yaml_paths:
                tree.load_plugin(yp)
            if executor is not None:
                executors[plugin_name] = executor
            if plugin_name == "shell":
                shell_config = plugin_config.get("config", {})
            loaded.append(plugin_name)

        return {
            "tree": tree,
            "executors": executors,
            "shell_config": shell_config,
            "loaded_plugins": loaded,
        }

    def _scan_plugin_dirs(self) -> List[str]:
        """扫描 plugins 目录下的子目录（含 tool.yaml 或 plugin.yaml）。"""
        result = []
        if not os.path.isdir(self._plugins_dir):
            return result
        for name in sorted(os.listdir(self._plugins_dir)):
            if name.startswith("_") or name.startswith("."):
                continue
            path = os.path.join(self._plugins_dir, name)
            if not os.path.isdir(path):
                continue
            # 有 plugin.yaml 或 tool.yaml 都算插件目录
            has_yaml = (
                os.path.exists(os.path.join(path, "plugin.yaml"))
                or os.path.exists(os.path.join(path, "tool.yaml"))
            )
            if has_yaml:
                result.append(path)
        return result

    def _read_plugin_meta(self, plugin_dir: str) -> Optional[dict]:
        """读取插件元信息（plugin.yaml 或从 tool.yaml 提取）。"""
        # 优先读 plugin.yaml
        plugin_yaml = os.path.join(plugin_dir, "plugin.yaml")
        if os.path.exists(plugin_yaml):
            with open(plugin_yaml, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        # 回退到 tool.yaml
        tool_yaml = os.path.join(plugin_dir, "tool.yaml")
        if os.path.exists(tool_yaml):
            with open(tool_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return {
                "plugin": data.get("plugin") or data.get("group"),
                "type": "tools",
                "version": data.get("version", "1.0"),
                "group": data.get("group") or data.get("plugin"),
                "summary": data.get("summary", ""),
                "detail": data.get("detail", ""),
            }
        return None
