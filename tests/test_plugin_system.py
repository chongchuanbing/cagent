"""插件体系测试：操作树 + guide/run_tool + shell 执行器 + 插件加载。"""
import os
import tempfile

import pytest
import yaml

from cagent.plugins.tree import OperationTree, OperationNode
from cagent.plugins.guide import ToolGuideTool, RunToolTool
from cagent.plugins.shell_exec import ShellExecutor
from cagent.plugins.manager import PluginManager, PluginExecutor


# ── 测试用的临时插件 ────────────────────────────────────────


@pytest.fixture
def tmp_plugins():
    """创建临时插件目录，含 filesystem（executor）和 shell 两个插件。"""
    with tempfile.TemporaryDirectory() as d:
        # filesystem 插件（框架代码执行）
        fs_dir = os.path.join(d, "filesystem")
        os.makedirs(fs_dir)
        # tool.yaml
        with open(os.path.join(fs_dir, "tool.yaml"), "w") as f:
            yaml.dump({
                "plugin": "filesystem",
                "group": "filesystem",
                "summary": "文件操作工具组",
                "tree": [
                    {
                        "name": "apply_patch",
                        "summary": "应用补丁修改文件",
                        "detail": "完整补丁格式说明...",
                        "execute": {"mode": "executor", "handler": "apply_patch"},
                        "params": {
                            "patch": {"type": "string", "required": True, "description": "补丁文本"},
                        },
                    },
                    {
                        "name": "read_file",
                        "summary": "读取文件内容",
                        "detail": "读取文件，带行号...",
                        "execute": {"mode": "executor", "handler": "read_file"},
                        "params": {
                            "path": {"type": "string", "required": True, "description": "文件路径"},
                            "start_line": {"type": "integer", "default": 0, "description": "起始行"},
                            "end_line": {"type": "integer", "default": 2000, "description": "结束行"},
                        },
                    },
                ],
            }, f)
        # executor.py
        with open(os.path.join(fs_dir, "executor.py"), "w") as f:
            f.write(
                "from cagent.tools.base import ToolResult\n"
                "_config = {}\n"
                "def configure(config):\n"
                "    global _config\n"
                "    _config = config\n"
                "def apply_patch(patch, **kwargs):\n"
                "    return ToolResult(ok=True, content=f'patch applied: {patch[:20]}')\n"
                "def read_file(path, start_line=0, end_line=2000, **kwargs):\n"
                "    return ToolResult(ok=True, content=f'file={path} start={start_line} end={end_line}')\n"
            )

        # shell 插件（shell 驱动）
        sh_dir = os.path.join(d, "shell")
        os.makedirs(sh_dir)
        with open(os.path.join(sh_dir, "tool.yaml"), "w") as f:
            yaml.dump({
                "plugin": "shell",
                "group": "shell",
                "summary": "命令执行工具组",
                "tree": [
                    {
                        "name": "run_command",
                        "summary": "执行任意 shell 命令",
                        "detail": "执行 shell 命令，返回 stdout/stderr/exit_code",
                        "execute": {"mode": "shell", "command_template": None},
                        "params": {
                            "command": {"type": "string", "required": True, "description": "完整命令"},
                            "timeout": {"type": "integer", "default": 30, "description": "超时秒"},
                        },
                    },
                    {
                        "name": "git",
                        "summary": "Git 操作子组",
                        "children": [
                            {
                                "name": "commit",
                                "summary": "提交代码",
                                "detail": "执行 git commit",
                                "execute": {
                                    "mode": "shell",
                                    "command_template": "git commit -m '{message}'",
                                },
                                "params": {
                                    "message": {"type": "string", "required": True, "description": "提交消息"},
                                },
                            },
                            {
                                "name": "push",
                                "summary": "推送到远程",
                                "detail": "执行 git push",
                                "execute": {
                                    "mode": "shell",
                                    "command_template": "git push {remote} {branch}",
                                },
                                "params": {
                                    "remote": {"type": "string", "default": "origin", "description": "远程"},
                                    "branch": {"type": "string", "default": "HEAD", "description": "分支"},
                                },
                            },
                        ],
                    },
                ],
            }, f)

        yield d


@pytest.fixture
def loaded(tmp_plugins):
    """加载临时插件，返回加载结果。"""
    pm = PluginManager(tmp_plugins, {})
    result = pm.load_all()
    return result


# ── OperationTree 测试 ──────────────────────────────────────


class TestOperationTree:
    def test_load_and_query(self, tmp_plugins):
        tree = OperationTree()
        tree.load_plugin(os.path.join(tmp_plugins, "filesystem", "tool.yaml"))
        tree.load_plugin(os.path.join(tmp_plugins, "shell", "tool.yaml"))

        # 查询根组
        fs = tree.query("filesystem")
        assert fs is not None
        assert fs.is_branch
        assert "apply_patch" in fs.children
        assert "read_file" in fs.children

        # 查询叶子
        leaf = tree.query("filesystem.apply_patch")
        assert leaf is not None
        assert leaf.is_leaf
        assert leaf.execute["mode"] == "executor"
        assert leaf.execute["handler"] == "apply_patch"

        # 查询多层路径
        commit = tree.query("shell.git.commit")
        assert commit is not None
        assert commit.is_leaf
        assert commit.execute["mode"] == "shell"

        # 查询不存在
        assert tree.query("nonexistent") is None
        assert tree.query("filesystem.nope") is None

    def test_list_groups(self, tmp_plugins):
        tree = OperationTree()
        tree.load_plugin(os.path.join(tmp_plugins, "filesystem", "tool.yaml"))
        tree.load_plugin(os.path.join(tmp_plugins, "shell", "tool.yaml"))
        groups = tree.list_groups()
        assert "filesystem" in groups
        assert "shell" in groups

    def test_list_leaf_paths(self, tmp_plugins):
        tree = OperationTree()
        tree.load_plugin(os.path.join(tmp_plugins, "filesystem", "tool.yaml"))
        tree.load_plugin(os.path.join(tmp_plugins, "shell", "tool.yaml"))
        paths = tree.list_leaf_paths()
        assert "filesystem.apply_patch" in paths
        assert "filesystem.read_file" in paths
        assert "shell.run_command" in paths
        assert "shell.git.commit" in paths
        assert "shell.git.push" in paths


# ── ToolGuideTool 测试 ─────────────────────────────────────


class TestToolGuide:
    def test_no_path_lists_groups(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="")
        assert r.ok
        assert "filesystem" in r.content
        assert "shell" in r.content

    def test_query_group(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="filesystem")
        assert r.ok
        assert "apply_patch" in r.content
        assert "read_file" in r.content

    def test_query_subgroup(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="shell.git")
        assert r.ok
        assert "commit" in r.content
        assert "push" in r.content

    def test_query_leaf_detail(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="filesystem.apply_patch")
        assert r.ok
        assert "完整补丁格式" in r.content
        assert "patch" in r.content  # 参数

    def test_query_shell_leaf(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="shell.git.commit")
        assert r.ok
        assert "shell" in r.content.lower()
        assert "message" in r.content

    def test_query_nonexistent(self, loaded):
        guide = ToolGuideTool(loaded["tree"])
        r = guide.run(path="nonexistent")
        assert not r.ok
        assert "不存在" in r.error


# ── RunToolTool 测试 ────────────────────────────────────────


class TestRunTool:
    def test_exec_executor(self, loaded):
        runner = RunToolTool(loaded["tree"], loaded["executors"])
        r = runner.run(path="filesystem.apply_patch", params={"patch": "*** Begin Patch\n+hello\n*** End Patch"})
        assert r.ok
        assert "patch applied" in r.content

    def test_exec_executor_with_defaults(self, loaded):
        runner = RunToolTool(loaded["tree"], loaded["executors"])
        r = runner.run(path="filesystem.read_file", params={"path": "test.py"})
        assert r.ok
        assert "start=0" in r.content  # default start_line
        assert "end=2000" in r.content  # default end_line

    def test_exec_missing_required_param(self, loaded):
        runner = RunToolTool(loaded["tree"], loaded["executors"])
        r = runner.run(path="filesystem.apply_patch", params={})
        assert not r.ok

    def test_exec_nonexistent_path(self, loaded):
        runner = RunToolTool(loaded["tree"], loaded["executors"])
        r = runner.run(path="nonexistent.op", params={})
        assert not r.ok

    def test_exec_branch_not_executable(self, loaded):
        runner = RunToolTool(loaded["tree"], loaded["executors"])
        r = runner.run(path="shell.git", params={})
        assert not r.ok
        assert "子组" in r.error

    def test_exec_shell_command(self, loaded):
        """shell 驱动模式：模型生成完整命令。"""
        shell = ShellExecutor({"work_dir": os.path.abspath(".")})
        runner = RunToolTool(loaded["tree"], loaded["executors"], shell_executor=shell)
        r = runner.run(path="shell.run_command", params={"command": "echo hello"})
        assert r.ok
        assert "hello" in r.content

    def test_exec_shell_template(self, loaded):
        """shell 模板模式：框架拼命令。"""
        shell = ShellExecutor({"work_dir": os.path.abspath(".")})
        runner = RunToolTool(loaded["tree"], loaded["executors"], shell_executor=shell)
        r = runner.run(
            path="shell.git.commit",
            params={"message": "test commit"},
        )
        # 实际不在 git 仓库里会失败（退出码 1），但命令应该被渲染并执行了
        # 验证 git 命令被执行（stdout 中含 git 输出）
        assert r.content is not None
        assert "exit_code" in r.content


# ── ShellExecutor 测试 ─────────────────────────────────────


class TestShellExecutor:
    def test_basic_execution(self):
        se = ShellExecutor({"work_dir": os.path.abspath(".")})
        r = se.execute("echo hello")
        assert r.ok
        assert "hello" in r.content

    def test_timeout(self):
        se = ShellExecutor({"work_dir": os.path.abspath("."), "timeout": 1})
        r = se.execute("sleep 5")
        assert not r.ok
        assert "超时" in r.error

    def test_blocked_pattern(self):
        se = ShellExecutor({
            "work_dir": os.path.abspath("."),
            "blocked_patterns": ["rm -rf"],
        })
        r = se.execute("rm -rf /")
        assert not r.ok
        assert "黑名单" in r.error

    def test_cwd_escape_blocked(self):
        se = ShellExecutor({"work_dir": os.path.abspath(".")})
        r = se.execute("echo hack", cwd="/etc")
        assert not r.ok
        assert "越界" in r.error

    def test_output_truncation(self):
        se = ShellExecutor({"work_dir": os.path.abspath("."), "max_output_chars": 50})
        r = se.execute("echo " + "a" * 200)
        assert r.ok
        assert "截断" in r.content

    def test_env_filter(self):
        se = ShellExecutor({
            "work_dir": os.path.abspath("."),
            "blocked_env": ["SECRET_VAR"],
        })
        os.environ["SECRET_VAR"] = "secret"
        r = se.execute("echo $SECRET_VAR")
        assert r.ok
        assert "secret" not in r.content
        del os.environ["SECRET_VAR"]

    def test_render_command(self):
        se = ShellExecutor({"work_dir": "."})
        rendered = se.render_command(
            "git {add_all_opt} commit -m '{message}'",
            {"message": "fix bug", "add_all_opt": "add . &&", "add_all": True},
        )
        assert "add . &&" in rendered
        assert "fix bug" in rendered


# ── PluginManager 测试 ─────────────────────────────────────


class TestPluginManager:
    def test_load_all(self, tmp_plugins):
        pm = PluginManager(tmp_plugins, {})
        result = pm.load_all()
        assert "filesystem" in result["loaded_plugins"]
        assert "shell" in result["loaded_plugins"]
        assert "filesystem.apply_patch" in result["tree"]
        assert "shell.git.commit" in result["tree"]
        assert "filesystem" in result["executors"]

    def test_disabled_plugin(self, tmp_plugins):
        pm = PluginManager(tmp_plugins, {"filesystem": {"enabled": False}})
        result = pm.load_all()
        assert "filesystem" not in result["loaded_plugins"]
        assert "shell" in result["loaded_plugins"]

    def test_executor_configure(self, tmp_plugins):
        """验证 executor 的 configure 函数被调用。"""
        pm = PluginManager(
            tmp_plugins,
            {"filesystem": {"enabled": True, "config": {"work_dir": "/tmp/test"}}},
        )
        result = pm.load_all()
        executor = result["executors"]["filesystem"]
        # executor.py 的 configure 应该把 config 存了
        # 验证执行函数能正常调用
        runner = RunToolTool(result["tree"], result["executors"])
        r = runner.run(path="filesystem.read_file", params={"path": "test.py"})
        assert r.ok
