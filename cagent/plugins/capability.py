"""L2 启动期环境探测：扫描本机 CLI 工具可用性。

在 Agent 启动时扫描常见命令行工具（fd/rg/git 等），缓存结果到 .data/env.json，
供 L3 工具面裁剪和 ReAct prompt 注入使用。
"""
import json
import logging
import os
import shutil
import subprocess
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 默认探测清单：工具名 → 探测命令（通常同名）
DEFAULT_PROBE_LIST = {
    "fd": "fd",
    "rg": "rg",
    "find": "find",
    "grep": "grep",
    "sed": "sed",
    "git": "git",
    "python3": "python3",
    "node": "node",
    "npm": "npm",
}


class CapabilityProbe:
    """启动期环境探测：扫描本机 CLI 工具可用性。

    结果缓存在内存和 .data/env.json 中，避免每次 run 重复扫描。
    """

    def __init__(self, cache_path: Optional[str] = None, probe_list: Optional[Dict[str, str]] = None):
        self.cache_path = cache_path
        self.probe_list = probe_list or DEFAULT_PROBE_LIST
        self._capabilities: Optional[Dict[str, dict]] = None

    def probe(self) -> Dict[str, dict]:
        """扫描所有目标工具，返回 {tool: {available, path, version}}。

        优先从缓存读取；缓存不存在或已过期则重新扫描。
        """
        if self._capabilities is not None:
            return self._capabilities

        # 尝试从缓存文件读取
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, dict) and cached:
                    self._capabilities = cached
                    return cached
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"能力缓存读取失败，将重新扫描: {e}")

        # 重新扫描
        result = {}
        for tool_name, cmd in self.probe_list.items():
            info = self._probe_single(cmd)
            result[tool_name] = info

        self._capabilities = result

        # 写入缓存
        if self.cache_path:
            self._save_cache(result)

        return result

    def is_available(self, tool_name: str) -> bool:
        """查询指定工具是否可用。"""
        caps = self.probe()
        return caps.get(tool_name, {}).get("available", False)

    def get_path(self, tool_name: str) -> Optional[str]:
        """获取指定工具的可执行路径。"""
        caps = self.probe()
        return caps.get(tool_name, {}).get("path")

    def to_env_facts(self) -> str:
        """生成环境事实字符串，用于注入 ReAct system prompt。

        格式示例：
          fd=可用, rg=不可用, git=可用, python3=可用
        """
        caps = self.probe()
        parts = []
        for tool_name, info in caps.items():
            status = "可用" if info.get("available") else "不可用"
            parts.append(f"{tool_name}={status}")
        return ", ".join(parts)

    def _probe_single(self, cmd: str) -> dict:
        """探测单个命令，返回 {available, path, version}。"""
        path = shutil.which(cmd)
        if path is None:
            return {"available": False, "path": None, "version": None}

        # 尝试获取版本
        version = self._get_version(cmd, path)
        return {"available": True, "path": path, "version": version}

    def _get_version(self, cmd: str, path: str) -> Optional[str]:
        """尝试获取命令版本。"""
        version_flags = ["--version", "-V", "version"]
        for flag in version_flags:
            try:
                result = subprocess.run(
                    [path, flag],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (result.stdout or result.stderr or "").strip()
                if output:
                    # 取第一行
                    return output.split("\n")[0][:80]
            except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
                continue
        return None

    def _save_cache(self, capabilities: dict) -> None:
        """保存探测结果到缓存文件。"""
        if not self.cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(capabilities, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.debug(f"能力缓存写入失败: {e}")

    def invalidate(self) -> None:
        """清除内存缓存，强制下次 probe() 重新扫描。"""
        self._capabilities = None
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                os.remove(self.cache_path)
            except OSError:
                pass
