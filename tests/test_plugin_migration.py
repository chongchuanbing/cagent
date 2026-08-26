"""插件迁移验证测试：filesystem 插件通过 tool_guide + run_tool 执行 apply_patch/read_file/list_dir。"""
import os
import tempfile

import pytest

from cagent.plugins import PluginManager, ToolGuideTool, RunToolTool, ShellExecutor


@pytest.fixture
def work_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def loaded(work_dir):
    """加载实际项目中的 filesystem 和 shell 插件。"""
    # 找到项目的 plugins 目录
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_dir = os.path.join(project_root, "cagent", "plugins")
    pm = PluginManager(plugins_dir, {
        "filesystem": {"enabled": True, "config": {"work_dir": work_dir, "allow_delete": True}},
        "shell": {"enabled": True, "config": {"work_dir": work_dir}},
    })
    result = pm.load_all()
    # 注入 work_dir 到 filesystem executor 的 PathGuard
    # （configure 已经在加载时调用，但 work_dir 可能不是临时目录）
    # 需要重新 configure
    fs_executor = result["executors"].get("filesystem")
    if fs_executor:
        fs_executor._module.configure({"work_dir": work_dir, "allow_delete": True})
    return result, work_dir


@pytest.fixture
def guide(loaded):
    result, _ = loaded
    return ToolGuideTool(result["tree"])


@pytest.fixture
def runner(loaded, work_dir):
    result, wd = loaded
    shell = ShellExecutor({"work_dir": work_dir})
    return RunToolTool(result["tree"], result["executors"], shell_executor=shell)


# ── tool_guide 披露验证 ────────────────────────────────────


class TestGuide:
    def test_list_groups(self, guide):
        r = guide.run(path="")
        assert r.ok
        assert "filesystem" in r.content
        assert "shell" in r.content

    def test_query_filesystem(self, guide):
        r = guide.run(path="filesystem")
        assert r.ok
        assert "apply_patch" in r.content

    def test_query_shell(self, guide):
        r = guide.run(path="shell")
        assert r.ok
        assert "run_command" in r.content
        assert "rg" in r.content
        assert "git" in r.content  # 子组

    def test_query_shell_git(self, guide):
        r = guide.run(path="shell.git")
        assert r.ok
        assert "commit" in r.content
        assert "push" in r.content

    def test_query_apply_patch_detail(self, guide):
        r = guide.run(path="filesystem.apply_patch")
        assert r.ok
        assert "*** Begin Patch" in r.content
        assert "Add" in r.content
        assert "Update" in r.content
        assert "patch" in r.content  # 参数


# ── run_tool 执行验证 ──────────────────────────────────────


class TestApplyPatchViaPlugin:
    def test_add_file(self, runner, work_dir):
        patch = """*** Begin Patch
*** Add File: new.py
+import os
+def hello():
+    return 'world'
*** End Patch"""
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert r.ok
        assert "新增" in r.content
        assert os.path.exists(os.path.join(work_dir, "new.py"))

    def test_update_file(self, runner, work_dir):
        # 先创建文件
        path = os.path.join(work_dir, "mod.py")
        with open(path, "w") as f:
            f.write("def get_value():\n    return None\n")
        patch = """*** Begin Patch
*** Update File: mod.py
def get_value():
-    return None
+    return 42
*** End Patch"""
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert r.ok
        with open(path) as f:
            content = f.read()
        assert "return 42" in content
        assert "return None" not in content

    def test_delete_file(self, runner, work_dir):
        path = os.path.join(work_dir, "del.py")
        with open(path, "w") as f:
            f.write("content")
        patch = """*** Begin Patch
*** Delete File: del.py
*** End Patch"""
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert r.ok
        assert not os.path.exists(path)

    def test_move_file(self, runner, work_dir):
        old_path = os.path.join(work_dir, "old.py")
        with open(old_path, "w") as f:
            f.write("data")
        patch = """*** Begin Patch
*** Move File: old.py -> new.py
*** End Patch"""
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert r.ok
        assert not os.path.exists(old_path)
        assert os.path.exists(os.path.join(work_dir, "new.py"))

    def test_path_traversal_blocked(self, runner, work_dir):
        patch = """*** Begin Patch
*** Add File: ../../etc/evil
+hacked
*** End Patch"""
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert not r.ok
        assert "越界" in r.error

    def test_empty_patch(self, runner):
        r = runner.run(path="filesystem.apply_patch", params={"patch": ""})
        assert not r.ok

    def test_malformed_patch_hint(self, runner):
        """格式错误时提示调用 tool_guide。"""
        patch = "*** Begin Patch\n*** Bad Op\n*** End Patch"
        r = runner.run(path="filesystem.apply_patch", params={"patch": patch})
        assert not r.ok
        assert "tool_guide" in r.error


class TestShellViaPlugin:
    def test_run_command(self, runner, work_dir):
        r = runner.run(path="shell.run_command", params={"command": "echo hello"})
        assert r.ok
        assert "hello" in r.content

    def test_run_command_with_timeout(self, runner, work_dir):
        r = runner.run(path="shell.run_command", params={"command": "echo hi", "timeout": 10})
        assert r.ok
        assert "hi" in r.content

    def test_run_command_timeout(self, runner, work_dir):
        """超时命令应被拦截。"""
        r = runner.run(path="shell.run_command", params={"command": "sleep 5", "timeout": 1})
        assert not r.ok
        assert "超时" in r.error

    def test_run_command_exit_code(self, runner, work_dir):
        """非零退出码应返回 ok=False。"""
        r = runner.run(path="shell.run_command", params={"command": "false"})
        assert not r.ok
        assert "exit_code" in r.content

    def test_rg(self, runner, work_dir):
        # 创建测试文件
        with open(os.path.join(work_dir, "test.py"), "w") as f:
            f.write("def main():\n    pass\n")
        r = runner.run(path="shell.rg", params={"command": "rg --line-number 'def main' test.py"})
        assert r.ok
        assert "def main" in r.content

    def test_fd(self, runner, work_dir):
        """fd/find 文件搜索。"""
        import shutil as _sh
        with open(os.path.join(work_dir, "find_me.py"), "w") as f:
            f.write("")
        if _sh.which("fd"):
            cmd = "fd 'find_me'"
        else:
            cmd = f"find . -name 'find_me.py'"
        r = runner.run(path="shell.fd", params={"command": cmd})
        assert r.ok
        assert "find_me" in r.content

    def test_sed(self, runner, work_dir):
        """sed 查看文件指定行内容。"""
        with open(os.path.join(work_dir, "sed_me.txt"), "w") as f:
            f.write("line1\nline2\nline3\n")
        r = runner.run(path="shell.sed", params={"command": "sed -n '1,3p' sed_me.txt"})
        assert r.ok
        assert "line1" in r.content
        assert "line2" in r.content
        assert "line3" in r.content

    def test_git_status(self, runner, work_dir):
        r = runner.run(path="shell.git.status", params={"command": "git status --short"})
        # 可能不在 git 仓库，但命令应该被执行
        assert r.content is not None

    def test_git_log(self, runner, work_dir):
        r = runner.run(path="shell.git.log", params={"command": "git log --oneline -1"})
        assert r.content is not None

    def test_command_blacklist(self, runner, work_dir):
        """黑名单命令应被拦截。"""
        # loaded fixture 的 shell 配置没有黑名单，需要直接测 ShellExecutor
        from cagent.plugins import ShellExecutor
        se = ShellExecutor({
            "work_dir": work_dir,
            "blocked_patterns": ["rm -rf"],
        })
        r = se.execute("rm -rf /")
        assert not r.ok
        assert "黑名单" in r.error

    def test_cwd_escape_blocked(self, runner, work_dir):
        """工作目录越界应被拦截。"""
        r = runner.run(path="shell.run_command", params={
            "command": "echo hack", "cwd": "/etc"
        })
        assert not r.ok
        assert "越界" in r.error
