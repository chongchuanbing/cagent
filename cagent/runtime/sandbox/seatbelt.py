"""macOS Seatbelt 后端：sandbox-exec 包裹执行。

sandbox-exec 是 Apple 的私有沙箱机制（OpenAI Codex CLI 同款路线），
无官方文档、profile 全靠试错，因此：
- available() 严格探测（darwin + sandbox-exec 存在）；
- profile 采用相对宽松的「系统操作放行 + 文件写/网络收紧」骨架，
  追求常规命令（git / rg / python ...）可用，而非极限收紧；
- 拒绝信号（Operation not permitted）由 ShellExecutor._classify_exit_code
  统一映射为 error_kind="SANDBOX"。
"""
import shutil
import sys
from pathlib import Path
from typing import List

from .base import ExecutionBackend


def _q(path) -> str:
    """sbpl 字符串字面量转义。"""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


class SeatbeltBackend(ExecutionBackend):
    """macOS：sandbox-exec -p <profile> /bin/sh -c <command>。"""

    name = "seatbelt"

    def available(self) -> bool:
        if sys.platform != "darwin":
            return False
        return shutil.which("sandbox-exec") is not None

    def wrap_argv(self, command: str, scope=None, path_space=None) -> List[str]:
        rules = self._rules(scope, path_space)
        if rules is None:
            return ["/bin/sh", "-c", command]
        profile = self.render_profile(rules)
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", command]

    # ── profile 渲染 ──────────────────────────────────────

    def render_profile(self, rules) -> str:
        """把 SandboxRules 渲染为 sbpl（Seatbelt profile 语言）。

        骨架：deny default（默认全拒）+ 显式放行。
        """
        lines = [
            "(version 1)",
            "(deny default)",
            # 进程与系统服务：常规命令（fork/exec、dyld、launchd 查询）普遍依赖
            "(allow process*)",
            "(allow process-info*)",
            "(allow mach-lookup)",
            "(allow sysctl-read)",
            "(allow sysctl-write)",
            "(allow signal)",
        ]

        # 只读区：文件读取 + mmap（动态库加载需要 file-map-executable）
        if rules.readable:
            filters = " ".join(
                f'(subpath "{_q(r)}")' for r in rules.readable if r != Path("/")
            )
            if filters:
                lines.append(f"(allow file-read* {filters})")
                lines.append(f"(allow file-map-executable {filters})")

        # 唯一可写区：会话 scratch + /dev/null
        write_filters = " ".join(
            f'(subpath "{_q(w)}")' for w in rules.writable
        )
        lines.append(f'(allow file-write* {write_filters} (literal "/dev/null"))')

        # 网络：默认禁（deny default 已覆盖，仅在显式放行时声明）
        if not rules.network_deny:
            lines.append("(allow network*)")

        return "\n".join(lines)
