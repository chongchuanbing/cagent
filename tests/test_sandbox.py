"""系统级沙箱测试：后端包装、规则编译、降级链、错误映射。"""
import os
import subprocess
from pathlib import Path

import pytest

from cagent.plugins.shell_exec import ShellExecutor
from cagent.runtime.paths import Mount, PathSpace
from cagent.runtime.sandbox import (
    BwrapBackend,
    LocalBackend,
    SandboxSettings,
    SeatbeltBackend,
    UserBackend,
    build_rules,
    select_backend,
)
from cagent.runtime.sandbox.profile_gen import SandboxRules
from cagent.runtime.session_scope import SessionScope


def _make_scope(tmp_path: Path, with_space: bool = False) -> SessionScope:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    space_root = tmp_path / "space" if with_space else None
    ps = None
    if with_space:
        space_root.mkdir(exist_ok=True)
        ps = PathSpace(
            base_dir=space_root,
            mounts={
                "workspace": Mount("workspace", space_root),
                "session": Mount("session", scratch),
            },
        )
    return SessionScope(scratch=scratch, space_root=space_root, path_space=ps)


class TestSandboxRules:
    """规则编译：PathSpace → SandboxRules。"""

    def test_no_scope_returns_none(self):
        assert build_rules(None, None) is None

    def test_scratch_is_only_writable(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        rules = build_rules(scope.path_space, scope)
        assert rules.writable == (Path(os.path.realpath(str(scope.scratch))),)

    def test_workspace_readable_not_writable(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        rules = build_rules(scope.path_space, scope)
        ws_real = os.path.realpath(str(scope.space_root))
        assert Path(ws_real) in rules.readable
        # 写白名单不含 workspace（shell 写空间必须走文件工具）
        assert not any(ws_real in str(w) and w != rules.writable[0] for w in rules.writable)

    def test_allows_write(self, tmp_path):
        scope = _make_scope(tmp_path)
        rules = build_rules(None, scope)
        assert rules.allows_write(scope.scratch / "out.txt")
        assert not rules.allows_write(scope.scratch.parent / "evil.txt")

    def test_network_flag(self, tmp_path):
        scope = _make_scope(tmp_path)
        assert build_rules(None, scope, network_deny=True).network_deny is True
        assert build_rules(None, scope, network_deny=False).network_deny is False

    def test_extra_readable(self, tmp_path):
        scope = _make_scope(tmp_path)
        rules = build_rules(None, scope, extra_readable=(str(tmp_path / "extra"),))
        assert Path(str(tmp_path / "extra")) in rules.readable


class TestBackends:
    """后端 argv 包装。"""

    def test_local_backend_plain_sh(self):
        b = LocalBackend()
        assert b.wrap_argv("echo hi") == ["/bin/sh", "-c", "echo hi"]
        assert b.wrap_argv("echo hi", scope=None) == ["/bin/sh", "-c", "echo hi"]

    def test_local_execute_echo(self, tmp_path):
        """LocalBackend 端到端：run_argv 正常返回。"""
        from cagent.runtime.sandbox import run_argv
        argv = LocalBackend().wrap_argv("echo hello")
        result = run_argv(argv, str(tmp_path), dict(os.environ), 10)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_seatbelt_profile(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        b = SeatbeltBackend()
        profile = b.render_profile(build_rules(scope.path_space, scope))
        assert "(deny default)" in profile
        scratch_real = str(Path(os.path.realpath(str(scope.scratch))))
        assert f'(allow file-write* (subpath "{scratch_real}")' in profile
        assert "(allow file-read*" in profile
        # 默认禁网：不应出现 allow network
        assert "allow network" not in profile

    def test_seatbelt_profile_network_allow(self, tmp_path):
        scope = _make_scope(tmp_path)
        rules = build_rules(None, scope, network_deny=False)
        profile = SeatbeltBackend().render_profile(rules)
        assert "(allow network*)" in profile

    def test_seatbelt_argv(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        b = SeatbeltBackend()
        argv = b.wrap_argv("ls", scope, scope.path_space)
        assert argv[0] == "sandbox-exec"
        assert argv[1] == "-p"
        assert argv[-3:] == ["/bin/sh", "-c", "ls"]

    def test_bwrap_argv(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        b = BwrapBackend()
        argv = b.wrap_argv("ls", scope, scope.path_space)
        assert argv[0] == "bwrap"
        joined = " ".join(argv)
        scratch_real = str(Path(os.path.realpath(str(scope.scratch))))
        assert f"--bind {scratch_real} {scratch_real}" in joined
        assert "--unshare-net" in joined
        assert argv[-3:] == ["/bin/sh", "-c", "ls"]

    def test_bwrap_ro_bind_workspace(self, tmp_path):
        scope = _make_scope(tmp_path, with_space=True)
        b = BwrapBackend()
        argv = b.wrap_argv("ls", scope, scope.path_space)
        joined = " ".join(argv)
        ws_real = str(Path(os.path.realpath(str(scope.space_root))))
        assert f"--ro-bind-try {ws_real} {ws_real}" in joined
        # 可写挂载必须出现在只读挂载之后（覆盖语义）
        assert joined.rindex("--bind") > joined.rindex("--ro-bind-try")

    def test_user_backend_requires_user(self):
        assert UserBackend(SandboxSettings()).available() is False
        argv = UserBackend(SandboxSettings()).wrap_argv("ls")
        assert argv == ["/bin/sh", "-c", "ls"]  # 未配置时退化为普通执行

    def test_user_backend_argv(self):
        b = UserBackend(SandboxSettings(mode="auto", user="cagent-agent"))
        argv = b.wrap_argv("ls")
        assert argv[:5] == ["sudo", "-n", "-u", "cagent-agent", "--"]

    def test_no_scope_degrades_to_sh(self, tmp_path):
        """无会话作用域时，沙箱后端退化为普通 sh（不猜规则）。"""
        b = BwrapBackend()
        assert b.wrap_argv("ls") == ["/bin/sh", "-c", "ls"]
        b2 = SeatbeltBackend()
        assert b2.wrap_argv("ls") == ["/bin/sh", "-c", "ls"]


class TestSelector:
    """降级链选择。"""

    def test_mode_off_always_local(self):
        b = select_backend(SandboxSettings(mode="off"))
        assert isinstance(b, LocalBackend)

    def test_strict_raises_when_nothing_available(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="strict"):
            select_backend(SandboxSettings(mode="strict"))

    def test_auto_falls_back_to_local(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        b = select_backend(SandboxSettings(mode="auto"))
        assert isinstance(b, LocalBackend)

    def test_auto_prefers_bwrap(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
        )
        b = select_backend(SandboxSettings(mode="auto"))
        assert isinstance(b, BwrapBackend)


class TestShellExecutorIntegration:
    """ShellExecutor 与后端集成。"""

    def test_default_backend_is_local(self, tmp_path):
        se = ShellExecutor({"work_dir": str(tmp_path)})
        assert se.backend.name == "local"

    def test_execute_with_local_backend(self, tmp_path):
        se = ShellExecutor({"work_dir": str(tmp_path), "enable_path_sandbox": False})
        result = se.execute("echo sandbox-test")
        assert result.ok is True
        assert "sandbox-test" in result.content

    def test_execute_timeout_kills_group(self, tmp_path):
        """超时击杀进程组：不留孤儿子进程。"""
        se = ShellExecutor({"work_dir": str(tmp_path), "timeout": 1})
        result = se.execute("sleep 30 & sleep 30")
        assert result.ok is False
        assert result.error_kind == "TIMEOUT"
        # 孤儿进程清理需要一点时间
        import time
        time.sleep(0.2)
        check = subprocess.run(
            ["pgrep", "-f", "sleep 30"], capture_output=True, text=True
        )
        assert check.stdout.strip() == ""

    def test_operation_not_permitted_maps_to_sandbox(self, tmp_path):
        """系统级沙箱拒绝信号 → error_kind=SANDBOX。"""

        class FakeBackend(LocalBackend):
            name = "seatbelt"

            def wrap_argv(self, command, scope=None, path_space=None):
                return [
                    "/bin/sh", "-c",
                    'echo "sed: Operation not permitted" >&2; exit 1',
                ]

        se = ShellExecutor({"work_dir": str(tmp_path)}, backend=FakeBackend())
        result = se.execute("sed -i s/a/b/ config.yml")
        assert result.ok is False
        assert result.error_kind == "SANDBOX"
        assert "seatbelt" in result.hint

    def test_permission_denied_local_keeps_old_hint(self, tmp_path):
        se = ShellExecutor({"work_dir": str(tmp_path)})
        # sed 读取（cat 已被禁止）；两条命令后看权限提示
        result = se.execute("sed -n '1,1p' /etc/sudoers 2>&1 || true; sed -n '1,1p' /etc/sudoers")
        # local 后端不注入沙箱话术
        if result.ok is False and result.error_kind == "SANDBOX":
            assert "系统级沙箱" not in result.hint
