"""执行后端抽象：系统级沙箱（Seatbelt / bubblewrap / 低权限用户）。

设计详见 docs/design/sandbox-design.md：

- 应用层校验（ShellExecutor 七层链）保留在前，系统级沙箱是兜底防线；
- 规则单一事实源：SandboxRules 由 PathSpace + SessionScope 编译（profile_gen），
  各后端只负责把规则翻译为平台语法（sbpl / bwrap 参数）；
- 错误路径：系统级拒绝统一由 ShellExecutor._classify_exit_code 映射回
  error_kind="SANDBOX"，保证 ReAct 可自纠。

模块分层：本包不依赖 cagent.config / cagent.plugins（runtime 下层），
配置由端上转换为 SandboxSettings 后注入。
"""
from .base import (
    ExecutionBackend,
    LocalBackend,
    SandboxSettings,
    run_argv,
)
from .bwrap import BwrapBackend
from .profile_gen import SandboxRules, build_rules
from .seatbelt import SeatbeltBackend
from .user import UserBackend


def select_backend(settings, logger=None) -> ExecutionBackend:
    """按配置选择执行后端。

    降级链：bwrap → seatbelt → user → local
    - mode=off    ：直接 local（现状）
    - mode=auto  ：探测降级，落到 local 时记警告
    - mode=strict：探测失败抛错（安全优先，拒绝裸执行）
    """
    if settings.mode == "off":
        return LocalBackend()

    candidates = [BwrapBackend(settings), SeatbeltBackend(settings)]
    if settings.user:
        candidates.append(UserBackend(settings))

    for backend in candidates:
        if backend.available():
            return backend

    if settings.mode == "strict":
        raise RuntimeError(
            "sandbox.mode=strict 但无可用系统级沙箱后端"
            "（bwrap / seatbelt / user 均不可用），拒绝裸执行 shell"
        )

    if logger is not None:
        logger.warning(
            "sandbox.mode=auto 未探测到系统级沙箱（bwrap/seatbelt/user），"
            "降级为 local 裸执行"
        )
    return LocalBackend()


__all__ = [
    "ExecutionBackend",
    "LocalBackend",
    "SandboxSettings",
    "SandboxRules",
    "build_rules",
    "SeatbeltBackend",
    "BwrapBackend",
    "UserBackend",
    "run_argv",
    "select_backend",
]
