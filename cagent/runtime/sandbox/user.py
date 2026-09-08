"""低权限用户降权后端（Linux / 服务端部署的兜底降级方案）。

无 bwrap 且可配置 sudoers 时的替代：所有 shell 命令以专用低权限用户执行。

限制（实验性，文档 docs/design/sandbox-design.md §8）：
- 不做路径规则（sudo 无法表达），仅缩小权限面；
- scratch 属主问题需部署方预处理（共享组 / umask），否则全是
  permission denied；
- 依赖 sudoers 免密白名单：cagent ALL=(cagent-agent) NOPASSWD: ALL
"""
import shutil
from typing import List

from .base import ExecutionBackend


class UserBackend(ExecutionBackend):
    """sudo -n -u <user> -- /bin/sh -c <command>。"""

    name = "user"

    def available(self) -> bool:
        if not self.settings.user:
            return False
        return shutil.which("sudo") is not None

    def wrap_argv(self, command: str, scope=None, path_space=None) -> List[str]:
        if not self.settings.user:
            return ["/bin/sh", "-c", command]
        return [
            "sudo", "-n", "-u", self.settings.user, "--",
            "/bin/sh", "-c", command,
        ]
