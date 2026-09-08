"""执行后端基座：抽象接口、Local 后端、统一的子进程运行器。

统一运行器 run_argv 解决两个问题：
- 进程组管理：start_new_session + killpg，超时杀整个进程树（原
  subprocess.run 超时后可能遗留孤儿子进程）；
- 异常兼容：超时抛 subprocess.TimeoutExpired、命令不存在抛 FileNotFoundError，
  与 ShellExecutor 既有错误处理链保持一致。
"""
import os
import signal
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from .profile_gen import SandboxRules, build_rules


@dataclass(frozen=True)
class SandboxSettings:
    """系统级沙箱设置（由端上配置转换而来，runtime 不依赖 config 模块）。

    - mode：off / auto / strict（见 select_backend）
    - network_deny：True = 默认禁网（内置联网工具走主进程 httpx，不受影响）
    - extra_readable：额外只读前缀（罕见场景，一般用 PathSpace 声明）
    - user：UserBackend 专用低权限用户名（需预先配置 sudoers）
    """

    mode: str = "off"
    network_deny: bool = True
    extra_readable: tuple = field(default_factory=tuple)
    user: Optional[str] = None


class ExecutionBackend:
    """shell 命令执行后端。

    约定：
    - wrap_argv 返回可直接交给 subprocess.Popen 的 argv（不使用 shell=True，
      避免 argv 注入一层转义问题；命令体仍是完整 shell 字符串，由
      /bin/sh -c 解释）；
    - scope 为 None 或无 PathSpace 时（无会话作用域，如单测直调），
      沙箱无法确定可写区 → 退化为普通 sh 执行（不产生第二套猜测规则）。
    """

    name: str = "local"

    def __init__(self, settings: Optional[SandboxSettings] = None):
        self.settings = settings or SandboxSettings()

    def available(self) -> bool:
        """平台能力探测（是否可启用此后端）。"""
        return True

    def wrap_argv(
        self,
        command: str,
        scope=None,
        path_space=None,
    ) -> List[str]:
        """把 shell 命令包装为最终 argv。"""
        return ["/bin/sh", "-c", command]

    # ── 规则编译助手 ──────────────────────────────────────

    def _rules(self, scope, path_space=None) -> Optional[SandboxRules]:
        """从当前会话作用域编译沙箱规则；无作用域时返回 None。"""
        return build_rules(
            path_space,
            scope,
            extra_readable=self.settings.extra_readable,
            network_deny=self.settings.network_deny,
        )


class LocalBackend(ExecutionBackend):
    """现状后端：直接 /bin/sh -c，无系统级隔离（校验层仍在）。"""

    name = "local"


def run_argv(argv: List[str], cwd: str, env: dict, timeout: int):
    """统一子进程运行器：进程组管理 + 超时杀全组。

    返回 subprocess.CompletedProcess；超时抛 subprocess.TimeoutExpired
    （杀组后尽力回收再抛）；argv[0] 不存在抛 FileNotFoundError。
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,  # 子进程自成进程组，超时可整组击杀
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # 杀组后回收输出，避免管道悬挂
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """击杀整个进程组（SIGKILL），组不存在时退化为杀主进程。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
