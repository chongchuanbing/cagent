"""Linux bubblewrap 后端：命名空间隔离执行。

bubblewrap（bwrap）是 Flatpak 同款的轻量沙箱，比手写 seccomp 简单一个
数量级。挂载策略 = 规则的物理投影：
- readable → --ro-bind-try（只读挂载，路径不存在时静默跳过）
- writable → --bind（可写挂载，后声明覆盖先声明的只读挂载）
- network_deny → --unshare-net（shell 无网络；内置联网工具走主进程
  httpx，不经 shell，天然不受影响）
"""
import shutil
from pathlib import Path
from typing import List

from .base import ExecutionBackend


class BwrapBackend(ExecutionBackend):
    """Linux：bwrap --ro-bind ... --bind <scratch> -- /bin/sh -c <command>。"""

    name = "bwrap"

    def available(self) -> bool:
        return shutil.which("bwrap") is not None

    def wrap_argv(self, command: str, scope=None, path_space=None) -> List[str]:
        rules = self._rules(scope, path_space)
        if rules is None:
            return ["/bin/sh", "-c", command]
        return self.render_argv(rules, command)

    def render_argv(self, rules, command: str) -> List[str]:
        argv = [
            "bwrap",
            "--dev", "/dev",        # 最小 /dev（null/zero/random/tty）
            "--proc", "/proc",
            "--die-with-parent",    # 父进程退出（含超时击杀）后不残留
            "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", str(rules.writable[0]),  # HOME 指向 scratch
        ]

        # 只读挂载：系统前缀 + mounts + 受信外挂区（--try = 不存在时跳过）
        for r in rules.readable:
            r = str(r)
            if r in ("/dev", "/proc"):
                continue  # 已由 --dev / --proc 覆盖
            argv += ["--ro-bind-try", r, r]

        # 可写挂载：会话 scratch（后声明，覆盖可能命中的只读挂载）
        for w in rules.writable:
            argv += ["--bind", str(w), str(w)]

        if rules.network_deny:
            argv.append("--unshare-net")

        argv += ["--", "/bin/sh", "-c", command]
        return argv
