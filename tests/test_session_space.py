"""会话路径隔离测试：session:// / workspace:// 双区挂载、审计与 shell 加固。"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from cagent.runtime.paths import PathSpace
from cagent.runtime.session_scope import SessionScope, current_scope, enter_scope
from cagent.storage import SessionRecorder, get_storage


# ── PathSpace.for_session ──────────────────────────────────


def _base_space(tmp: Path) -> PathSpace:
    return PathSpace.build_default(tmp, data_dir=tmp / ".data")


def test_for_session_without_space():
    """无空间：workspace:// 即 scratch（别名），所有文件落会话目录。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base = _base_space(tmp)
        scratch = Path(os.path.realpath(tmp / ".data" / "sessions" / "s1" / "scratch"))
        space = base.for_session(scratch)

        ws = space.mounts["workspace"]
        sm = space.mounts["session"]
        assert ws.physical == sm.physical == scratch
        assert sm.modes >= {"read", "write"}
        # 裸相对路径解析到 scratch
        assert space.resolve("out/a.txt") == scratch / "out" / "a.txt"
        assert space.resolve("session://out/a.txt") == scratch / "out" / "a.txt"


def test_for_session_with_space():
    """空间模式：workspace:// 挂空间根，session:// 挂 scratch，两者分离。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base = _base_space(tmp)
        scratch = Path(os.path.realpath(tmp / ".data" / "sessions" / "s1" / "scratch"))
        proj = Path(os.path.realpath(tmp / "myproj"))
        proj.mkdir()
        space = base.for_session(scratch, space_root=proj)

        assert space.mounts["workspace"].physical == proj
        assert space.mounts["session"].physical == scratch
        assert space.resolve("a.txt") == scratch / "a.txt"  # 相对路径默认落 scratch
        assert space.resolve("workspace://src/main.py") == proj / "src" / "main.py"
        # 空间内可写（结构化工具通道）
        p = space.resolve("workspace://src/main.py")
        assert space.assert_safe(p, mode="write") == p
        # path_card 提示双区语义
        card = space.path_card()
        assert "session://" in card
        assert "workspace://" in card
        assert "临时" in card


def test_path_card_merged_without_space():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base = _base_space(tmp)
        scratch = tmp / ".data" / "sessions" / "s1" / "scratch"
        space = base.for_session(scratch)
        card = space.path_card()
        assert "所有生成文件默认写这里" in card


# ── filesystem.apply_patch + 会话作用域 ─────────────────────


@pytest.fixture()
def fs_env(tmp_path):
    """构造「storage + 会话作用域 + filesystem executor」环境。"""
    from cagent.plugins.filesystem import executor as fs_exec

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text("print('v1')\n", encoding="utf-8")

    storage = get_storage(str(tmp_path / ".data"))
    base_space = PathSpace.build_default(tmp_path, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    recorder = SessionRecorder(storage, "s1")
    recorder.record_meta("测试目标")

    fs_exec.configure({"work_dir": str(tmp_path)})
    return fs_exec, scratch, proj, recorder, session_space


def _scope(scratch, proj, session_space, recorder):
    def record(rel, op):
        recorder.record_workspace_write(rel, op)

    return SessionScope(
        scratch=scratch, space_root=proj,
        path_space=session_space, record_write=record,
    )


def test_apply_patch_relative_goes_to_scratch(fs_env):
    """相对路径 → 会话 scratch（临时文件不落空间）。"""
    fs_exec, scratch, proj, recorder, session_space = fs_env
    scope = _scope(scratch, proj, session_space, recorder)
    with enter_scope(scope):
        r = fs_exec.apply_patch(
            "*** Begin Patch\n"
            "*** Add File: notes/tmp.txt\n"
            "+hello\n"
            "*** End Patch"
        )
    assert r.ok, r.error
    assert (scratch / "notes" / "tmp.txt").read_text() == "hello"
    assert not (proj / "notes").exists()
    # 临时文件不登记审计
    assert recorder.load_meta().get("workspace_writes") is None


def test_apply_patch_workspace_scheme_writes_space_and_audits(fs_env):
    """workspace:// 前缀 → 写空间 + meta 登记 + 事件回调。"""
    fs_exec, scratch, proj, recorder, session_space = fs_env
    writes = []
    scope = SessionScope(
        scratch=scratch, space_root=proj, path_space=session_space,
        record_write=lambda rel, op: writes.append((rel, op)),
    )
    with enter_scope(scope):
        r = fs_exec.apply_patch(
            "*** Begin Patch\n"
            "*** Update File: workspace://src/main.py\n"
            "-print('v1')\n"
            "+print('v2')\n"
            "*** End Patch"
        )
    assert r.ok, r.error
    assert "v2" in (proj / "src" / "main.py").read_text()
    assert writes == [("src/main.py", "update")]
    # recorder 登记通道（scope.record_write 走 recorder）
    scope2 = _scope(scratch, proj, session_space, recorder)
    with enter_scope(scope2):
        r2 = fs_exec.apply_patch(
            "*** Begin Patch\n"
            "*** Add File: workspace://docs/new.md\n"
            "+doc\n"
            "*** End Patch"
        )
    assert r2.ok, r2.error
    meta_writes = recorder.load_meta()["workspace_writes"]
    assert meta_writes[-1]["path"] == "docs/new.md"
    assert meta_writes[-1]["op"] == "add"


def test_apply_patch_absolute_outside_rejected(fs_env):
    """绝对路径越界（既非 scratch 也非空间）→ 拒绝。"""
    fs_exec, scratch, proj, recorder, session_space = fs_env
    outside = Path(tempfile.mkdtemp()) / "evil.txt"
    scope = _scope(scratch, proj, session_space, recorder)
    with enter_scope(scope):
        r = fs_exec.apply_patch(
            f"*** Begin Patch\n"
            f"*** Add File: {outside}\n"
            "+bad\n"
            f"*** End Patch"
        )
    assert not r.ok
    assert "越界" in r.error


def test_apply_patch_without_scope_falls_back(fs_env):
    """无作用域：回退原 PathGuard（work_dir 内相对路径可用）。"""
    fs_exec, scratch, proj, recorder, session_space = fs_env
    assert current_scope() is None
    r = fs_exec.apply_patch(
        "*** Begin Patch\n"
        "*** Add File: legacy.txt\n"
        "+old behavior\n"
        "*** End Patch"
    )
    assert r.ok, r.error


# ── ShellExecutor 会话加固 ─────────────────────────────────


@pytest.fixture()
def shell_env(tmp_path):
    from cagent.plugins.shell_exec import ShellExecutor

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "a.txt").write_text("x", encoding="utf-8")

    base_space = PathSpace.build_default(tmp_path, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(tmp_path)}, path_space=base_space)
    scope = SessionScope(
        scratch=scratch, space_root=proj, path_space=session_space,
    )
    return ex, scratch, proj, scope


def test_shell_cwd_pinned_to_scratch(shell_env):
    ex, scratch, proj, scope = shell_env
    with enter_scope(scope):
        r = ex.execute("pwd")
    assert r.ok, r.error
    assert str(scratch) in r.content


def test_shell_relative_output_lands_in_scratch(shell_env):
    ex, scratch, proj, scope = shell_env
    with enter_scope(scope):
        r = ex.execute("echo hi > out.txt")
    assert r.ok, r.error
    assert (scratch / "out.txt").read_text().strip() == "hi"


def test_shell_redirect_into_space_rejected(shell_env):
    ex, scratch, proj, scope = shell_env
    with enter_scope(scope):
        r = ex.execute(f"echo bad > {proj}/evil.txt")
    assert not r.ok
    assert r.error_kind == "SANDBOX"
    assert "apply_patch" in r.hint
    assert not (proj / "evil.txt").exists()


def test_shell_redirect_outside_all_rejected(shell_env):
    ex, scratch, proj, scope = shell_env
    with enter_scope(scope):
        r = ex.execute("echo x > /tmp/cagent_should_not_write.txt")
    assert not r.ok
    assert not Path("/tmp/cagent_should_not_write.txt").exists()


def test_shell_cwd_space_allowed_for_build(shell_env):
    """cd 到空间目录执行（构建/测试）允许，相对输出落空间 cwd（已知边界）。"""
    ex, scratch, proj, scope = shell_env
    with enter_scope(scope):
        r = ex.execute("ls", cwd=str(proj))
    assert r.ok, r.error
    assert "a.txt" in r.content


def test_shell_cwd_outside_rejected(shell_env):
    ex, scratch, proj, scope = shell_env
    outside = Path(tempfile.mkdtemp())
    with enter_scope(scope):
        r = ex.execute("ls", cwd=str(outside))
    assert not r.ok
    assert r.error_kind == "SANDBOX"


def test_shell_cd_space_with_relative_output_rejected(shell_env):
    """qiaoma-wudao 场景回归：cd 进空间后 --out 相对路径会被拦截。"""
    ex, scratch, proj, scope = shell_env
    cmd = f"cd {proj} && python gen.py --out workspace/qiaoma-wudao/img.png"
    with enter_scope(scope):
        r = ex.execute(cmd)
    assert not r.ok
    assert r.error_kind == "SANDBOX"
    assert "会话目录" in r.hint
    # 相对重定向目标同样拦截
    with enter_scope(scope):
        r2 = ex.execute(f"cd {proj} && python gen.py > report.txt")
    assert not r2.ok
    # cd 进空间但无输出目标（构建/测试）放行
    with enter_scope(scope):
        r3 = ex.execute(f"cd {proj} && ls")
    assert r3.ok, r3.error


def test_shell_relative_path_missing_in_scratch_but_in_project_hint(tmp_path):
    """回归 7168fea8：会话作用域下 run_command 用裸相对路径 (docs/design/)
    在 scratch 找不到，但项目根存在该路径时，错误 hint 应主动指出它在项目根存在，
    并引导用 workspace:// 前缀或绝对路径，而非泛泛的「用 ls 确认路径」。

    这复现了「docs/design/ 明明存在却报 No such file」的困惑：shell 默认 cwd
    是会话 scratch（写安全），裸相对路径不会相对项目根解析。
    """
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    # 项目根下确实存在 docs/design/（用户原始场景）
    (proj / "docs" / "design").mkdir(parents=True)
    (proj / "docs" / "design" / "arch.md").write_text("x", encoding="utf-8")

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=proj, path_space=session_space)

    with enter_scope(scope):
        r = ex.execute("ls -la docs/design/")

    assert not r.ok
    assert r.error_kind == "EXEC_ERROR"
    # 关键断言：hint 指出该路径在项目根存在，并给出 workspace:// / 绝对路径 的写法
    assert str(proj / "docs" / "design") in r.hint
    assert "workspace://" in r.hint
    # 不应再出现误导性的「默认项目根」语义
    assert "项目根" in r.hint


def test_shell_workspace_scheme_reaches_project_in_scope(tmp_path):
    """对照：会话作用域下用 workspace:// 前缀即可正确访问项目根（框架层面的正确写法）。"""
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "docs" / "design").mkdir(parents=True)

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=proj, path_space=session_space)

    with enter_scope(scope):
        r = ex.execute("ls -la workspace://docs/design/")
    assert r.ok, r.error
    assert "design" in r.content or r.content.strip() != ""


# ── Agent.run 端到端 ───────────────────────────────────────


def test_agent_run_creates_scratch(tmp_path):
    """无空间模式：run 自动创建会话 scratch 目录。"""
    from cagent.core import Agent
    from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
    from cagent.storage import get_storage
    from cagent.tools import ToolRegistry
    from cagent.tools.builtin import add

    class FakeLLM(LLMClient):
        def __init__(self, scripted):
            super().__init__(LLMConfig())
            self.scripted = list(scripted)

        def complete(self, messages):
            return self.scripted.pop(0)

        def complete_with_tools(self, messages, tools):
            return self.scripted.pop(0)

    plan = json.dumps({"steps": [{"id": "s1", "description": "算一下", "depends_on": []}]})
    llm = FakeLLM([
        LLMResponse(content=plan),
        LLMResponse(content="3+5=8"),
        LLMResponse(content="答案：8"),
    ])
    tools = ToolRegistry()
    tools.register(add)
    storage = get_storage(str(tmp_path / ".data"))
    base_space = PathSpace.build_default(tmp_path, data_dir=tmp_path / ".data")
    agent = Agent(llm=llm, tools=tools, storage=storage, path_space=base_space)
    agent.run("算 3+5", session_id="e2e1")

    scratch = tmp_path / ".data" / "sessions" / "e2e1" / "scratch"
    assert scratch.is_dir()
    # 无空间：meta 无空间写入记录
    meta = json.loads((tmp_path / ".data" / "sessions" / "e2e1" / "meta.json").read_text())
    assert "workspace_writes" not in meta
