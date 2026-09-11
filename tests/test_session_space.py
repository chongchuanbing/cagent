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
    """无空间：workspace:// = 项目根/当前目录，session:// = 会话 scratch（分离）。

    设计：no-space 下 workspace:// 恒等于用户当前目录（项目根），裸相对路径相对
    项目根解析；session:// 作为框架层临时/中间文件输出目录，与 workspace 物理分离。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(os.path.realpath(d))
        base = _base_space(tmp)
        scratch = Path(os.path.realpath(tmp / ".data" / "sessions" / "s1" / "scratch"))
        space = base.for_session(scratch)

        ws = space.mounts["workspace"]
        sm = space.mounts["session"]
        # workspace:// 仍指向项目根（当前目录），不再被重挂到 scratch
        assert ws.physical == tmp
        assert sm.physical == scratch
        assert ws.physical != sm.physical
        assert sm.modes >= {"read", "write"}
        # default_mount=workspace → 裸相对路径解析到项目根
        assert space.resolve("out/a.txt") == tmp / "out" / "a.txt"
        # session:// 独立指向 scratch
        assert space.resolve("session://out/a.txt") == scratch / "out" / "a.txt"
        # workspace:// 仍指向项目根
        assert space.resolve("workspace://src/main.py") == tmp / "src" / "main.py"


def test_for_session_with_space():
    """空间模式：workspace:// 挂空间根，session:// 挂 scratch，两者分离；
    裸相对路径默认落 scratch（保护空间根）。"""
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


def test_path_card_no_space_distinct_workspace_and_session():
    """无空间模式：path_card 区分 workspace（当前目录/项目根）与 session（框架临时输出）。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base = _base_space(tmp)
        scratch = tmp / ".data" / "sessions" / "s1" / "scratch"
        space = base.for_session(scratch)
        card = space.path_card()
        assert "session://" in card
        assert "workspace://" in card
        # workspace 不再是「会话工作目录」语义，而是当前目录/项目根
        assert "当前工作目录" in card
        assert "框架层临时" in card


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


def test_shell_read_command_relative_path_falls_back_to_project_root(tmp_path):
    """回归 03809787：用户给出的项目相对路径（docs/design/arch.md）应直接可用。

    会话作用域下 cwd 被钉在 scratch，裸相对路径本会在 scratch 下找不到。
    对只读命令，框架应自动回退到项目根解析，让「用户给的真实路径」直接生效，
    而不是要求模型手工转成 workspace:// 或绝对路径（否则模型会失败并转 ask_user）。
    """
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "docs" / "design").mkdir(parents=True)
    (proj / "docs" / "design" / "arch.md").write_text("hello\n", encoding="utf-8")

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=proj, path_space=session_space)

    # 读命令：裸相对路径自动回退到项目根
    with enter_scope(scope):
        r = ex.execute("ls -la docs/design/")
    assert r.ok, r.error
    assert "arch.md" in r.content

    with enter_scope(scope):
        r2 = ex.execute("sed -n '1,5p' docs/design/arch.md")
    assert r2.ok, r2.error
    assert "hello" in r2.content
    # 改写痕迹对 trace 可见（透明可审计）：空间模式下回退到空间根
    assert "空间根回退" in r2.content


def test_shell_read_fallback_does_not_leak_writes(tmp_path):
    """回退只作用于读命令：写命令的相对路径仍必须落 scratch，不能污染项目根。"""
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "docs").mkdir()

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=proj, path_space=session_space)

    with enter_scope(scope):
        r = ex.execute("echo hi > out.txt")
    assert r.ok, r.error
    assert (scratch / "out.txt").read_text().strip() == "hi"
    # 项目根未被污染
    assert not (proj / "out.txt").exists()


def test_shell_non_read_command_still_hints_project_root(tmp_path):
    """非只读命令（如 cp）不在回退范围内，失败时仍应给出「项目根存在」的 hint。

    保证回退收窄到只读命令后，其余场景的错误反馈没有退化。
    """
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "docs" / "design").mkdir(parents=True)
    (proj / "docs" / "design" / "arch.md").write_text("x", encoding="utf-8")

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    session_space = base_space.for_session(scratch, space_root=proj)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=proj, path_space=session_space)

    with enter_scope(scope):
        r = ex.execute("cp docs/design/arch.md ./copy.md")

    assert not r.ok
    assert r.error_kind == "EXEC_ERROR"
    # hint 仍指出该路径在项目根存在，并给出 workspace:// / 绝对路径 写法
    assert str(proj / "docs" / "design" / "arch.md") in r.hint
    assert "workspace://" in r.hint


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


def test_shell_nospace_bare_relative_reads_project_root(tmp_path):
    """回归 03809787（no-space）：用户给出的裸相对路径（docs/design/）应直接可读。

    无空间模式下 workspace:// = 项目根/当前目录，shell 默认 cwd = 项目根，
    因此裸相对路径相对项目根解析，无需 workspace:// 前缀或绝对路径。
    这是用户决策的核心：no-space 下 workspace:// 就是当前目录。
    """
    from cagent.plugins.shell_exec import ShellExecutor
    from cagent.runtime.session_scope import SessionScope, enter_scope
    from cagent.runtime.paths import PathSpace

    scratch = tmp_path / ".data" / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "docs" / "design").mkdir(parents=True)
    (proj / "docs" / "design" / "arch.md").write_text("hello\n", encoding="utf-8")

    base_space = PathSpace.build_default(proj, data_dir=tmp_path / ".data")
    # 无空间会话：space_root=None
    session_space = base_space.for_session(scratch, space_root=None)
    ex = ShellExecutor({"work_dir": str(proj)}, path_space=base_space)
    scope = SessionScope(scratch=scratch, space_root=None, path_space=session_space)

    # 关键断言：裸相对路径直接生效（cwd=项目根），不发生回退改写
    with enter_scope(scope):
        r = ex.execute("ls -la docs/design/")
    assert r.ok, r.error
    assert "arch.md" in r.content
    # 无空间模式下 cwd 即项目根，读取类命令不应触发「项目根/空间根回退」改写
    assert "回退" not in r.content

    with enter_scope(scope):
        r2 = ex.execute("sed -n '1,5p' docs/design/arch.md")
    assert r2.ok, r2.error
    assert "hello" in r2.content


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
