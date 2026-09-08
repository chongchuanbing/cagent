"""六层防线集成测试：L1 参数化、L4 路径沙箱、L6 结构化错误。"""
import os
import pytest
from cagent.plugins.shell_exec import ShellExecutor
from cagent.tools.base import ToolResult


class TestL1ParametricToolContract:
    """L1 参数化工具契约测试。"""

    def test_render_command_basic(self):
        """基础参数渲染。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        template = "rg {pattern} {type_opt} {path}"
        params = {"pattern": "def main", "type": "py", "path": "/tmp/test/src"}

        result = executor.render_command(template, params, {"pattern": {"required": True}})

        assert result.ok is True
        assert "rg def main" in result.content
        assert "--type py" in result.content
        assert "/tmp/test/src" in result.content

    def test_render_command_missing_required(self):
        """缺少必填参数时返回 PARAM_MISSING。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        template = "rg {pattern} {path}"
        params = {"path": "/tmp/test"}  # pattern 缺失
        schema = {"pattern": {"required": True}}

        result = executor.render_command(template, params, schema)

        assert result.ok is False
        assert result.error_kind == "PARAM_MISSING"
        assert "缺少必填参数: pattern" in result.error

    def test_render_command_optional_flags(self):
        """可选参数（布尔值）不传时不渲染。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        template = "fd {pattern} {hidden_opt}"
        params = {"pattern": "test"}  # hidden 未传

        result = executor.render_command(template, params)

        assert result.ok is True
        assert "--hidden" not in result.content

    def test_render_command_optional_flags_true(self):
        """可选参数（布尔值）为 True 时渲染。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        template = "fd {pattern} {hidden_opt}"
        params = {"pattern": "test", "hidden": True}

        result = executor.render_command(template, params)

        assert result.ok is True
        assert "--hidden" in result.content

    def test_render_command_with_string_value(self):
        """字符串值参数渲染。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        template = "git log --author=\"{author}\""
        params = {"author": "张三"}

        result = executor.render_command(template, params)

        assert result.ok is True
        assert "--author=\"张三\"" in result.content


class TestL4PathSandbox:
    """L4 路径沙箱测试。"""

    def test_check_paths_in_workdir(self):
        """路径在工作目录内，允许。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        command = "ls /tmp/test/src"

        result = executor._check_command_paths(command)

        assert result is None  # 无错误

    def test_check_paths_out_workdir(self):
        """路径超出工作目录，拦截。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        command = "ls /etc/passwd"

        result = executor._check_command_paths(command)

        assert result is not None
        assert result.ok is False
        assert result.error_kind == "SANDBOX"
        assert "路径越界" in result.error
        assert "/etc/passwd" in result.error

    def test_check_paths_whitelist(self):
        """白名单路径（如 /usr/bin/grep）允许。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        command = "/usr/bin/grep -r 'def' /tmp/test"

        result = executor._check_command_paths(command)

        assert result is None  # 白名单，允许

    def test_check_paths_dev_null(self):
        """/dev/null 允许。"""
        executor = ShellExecutor({"work_dir": "/tmp/test"})
        command = "ls /tmp/test > /dev/null"

        result = executor._check_command_paths(command)

        assert result is None  # /dev/null 允许

    def test_check_paths_disabled(self, tmp_path):
        """关闭路径沙箱后 execute 不检查路径。"""
        executor = ShellExecutor({"work_dir": str(tmp_path), "enable_path_sandbox": False})
        # /etc/hostname 在 macOS 上存在且可读；改用 sed -n（cat 已被框架默认禁止）
        result = executor.execute("sed -n '1,1p' /etc/hostname")
        # 沙箱关闭后不应因路径越界而报错
        # （可能因文件不存在而 EXEC_ERROR，但不会是 SANDBOX）
        if not result.ok:
            assert result.error_kind != "SANDBOX"


class TestL6StructuredError:
    """L6 结构化错误回流测试。"""

    def test_command_not_found(self, tmp_path):
        """命令不存在时返回 TOOL_UNAVAILABLE。"""
        executor = ShellExecutor({"work_dir": str(tmp_path)})
        command = "nonexistent_command_xyz_12345"

        result = executor.execute(command)

        assert result.ok is False
        assert result.error_kind == "TOOL_UNAVAILABLE"
        assert "未安装" in result.hint or "不在 PATH" in result.hint

    def test_timeout_error(self, tmp_path):
        """超时返回 TIMEOUT。"""
        executor = ShellExecutor({"work_dir": str(tmp_path)})
        command = "sleep 10"

        result = executor.execute(command, timeout=1)

        assert result.ok is False
        assert result.error_kind == "TIMEOUT"
        assert "超时" in result.error

    def test_sandbox_error(self, tmp_path):
        """路径越界返回 SANDBOX。"""
        executor = ShellExecutor({"work_dir": str(tmp_path)})
        command = "ls /etc/passwd"

        result = executor.execute(command)

        assert result.ok is False
        assert result.error_kind == "SANDBOX"
        assert "路径越界" in result.error

    def test_blocked_pattern(self, tmp_path):
        """黑名单模式返回 SANDBOX。"""
        executor = ShellExecutor({
            "work_dir": str(tmp_path),
            "blocked_patterns": ["rm -rf"]
        })
        command = "rm -rf /tmp"

        result = executor.execute(command)

        assert result.ok is False
        assert result.error_kind == "SANDBOX"
        assert "黑名单" in result.error

    def test_empty_command(self, tmp_path):
        """空命令返回 PARAM_MISSING。"""
        executor = ShellExecutor({"work_dir": str(tmp_path)})

        result = executor.execute("")

        assert result.ok is False
        assert result.error_kind == "PARAM_MISSING"

    def test_successful_command(self, tmp_path):
        """成功执行返回 ok=True，无 error_kind。"""
        executor = ShellExecutor({"work_dir": str(tmp_path)})
        command = "echo 'hello'"

        result = executor.execute(command)

        assert result.ok is True
        assert result.error_kind is None
        assert "hello" in result.content


class TestToolResultFields:
    """ToolResult 扩展字段测试。"""

    def test_toolresult_has_error_kind(self):
        """ToolResult 包含 error_kind 字段。"""
        result = ToolResult(ok=False, content="", error="test", error_kind="SANDBOX")
        assert result.error_kind == "SANDBOX"

    def test_toolresult_has_hint(self):
        """ToolResult 包含 hint 字段。"""
        result = ToolResult(ok=False, content="", error="test", hint="建议...")
        assert result.hint == "建议..."

    def test_toolresult_defaults(self):
        """ToolResult 默认值。"""
        result = ToolResult(ok=True, content="ok")
        assert result.error_kind is None
        assert result.hint is None


class TestL2EnvironmentProbe:
    """L2 环境探测测试。"""

    def test_probe_basic_tools(self):
        """探测常见工具可用性。"""
        from cagent.plugins.capability import CapabilityProbe
        probe = CapabilityProbe()
        result = probe.probe()

        # 至少应该探测到 git（cagent 是 git 仓库）
        assert "git" in result
        assert result["git"]["available"] is True
        assert result["git"]["path"] is not None

    def test_probe_nonexistent_tool(self):
        """探测不存在的工具。"""
        from cagent.plugins.capability import CapabilityProbe
        probe = CapabilityProbe(probe_list={"fake_tool_xyz": "fake_tool_xyz"})
        result = probe.probe()

        assert "fake_tool_xyz" in result
        assert result["fake_tool_xyz"]["available"] is False
        assert result["fake_tool_xyz"]["path"] is None

    def test_probe_cache_works(self):
        """缓存机制正常工作。"""
        from cagent.plugins.capability import CapabilityProbe
        probe = CapabilityProbe()

        # 第一次调用
        result1 = probe.probe()
        # 第二次调用应该返回缓存结果
        result2 = probe.probe()

        assert result1 == result2

    def test_is_available(self):
        """is_available 方法正确判断。"""
        from cagent.plugins.capability import CapabilityProbe
        probe = CapabilityProbe(probe_list={"echo": "echo"})
        probe.probe()

        assert probe.is_available("echo") is True
        assert probe.is_available("nonexistent_xyz") is False

    def test_to_env_facts_format(self):
        """环境事实格式化输出。"""
        from cagent.plugins.capability import CapabilityProbe
        probe = CapabilityProbe(probe_list={"echo": "echo", "fake_xyz": "fake_xyz"})
        probe.probe()

        facts = probe.to_env_facts()
        assert "echo=可用" in facts
        assert "fake_xyz=不可用" in facts


class TestL3ToolFiltering:
    """L3 工具裁剪测试。"""

    def test_filter_by_capability_basic(self):
        """基于可用性过滤工具。"""
        from cagent.plugins.capability import CapabilityProbe
        from cagent.core.react import ReActEngine

        # 模拟只有 echo 可用
        probe = CapabilityProbe(probe_list={"echo": "echo", "fake_xyz": "fake_xyz"})
        probe.probe()

        # 创建工具，一个可用，一个不可用
        class MockTool:
            def __init__(self, name, cli):
                self.name = name
                self.requires = {"cli": cli} if cli else None
            def run(self, **kwargs): return ToolResult(ok=True, content="ok")
            def schema(self): return {}

        available_tool = MockTool("echo_tool", "echo")
        unavailable_tool = MockTool("fake_tool", "fake_xyz")
        no_requires_tool = MockTool("general_tool", None)

        tools = [available_tool, unavailable_tool, no_requires_tool]

        # 模拟 ReActEngine 的过滤逻辑
        engine = ReActEngine.__new__(ReActEngine)
        engine.capability = probe

        filtered = engine._filter_by_capability(tools)

        # 只保留可用的工具
        assert len(filtered) == 2
        assert any(t.name == "echo_tool" for t in filtered)
        assert any(t.name == "general_tool" for t in filtered)
        assert not any(t.name == "fake_tool" for t in filtered)


class TestL5FailureLedger:
    """L5 失败账本测试。"""

    def test_record_failure(self):
        """记录失败。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger()
        ledger.record_failure("shell.run_command", "EXEC_ERROR")

        assert ledger.get_failure_count("shell.run_command") == 1
        assert ledger.get_last_error_kind("shell.run_command") == "EXEC_ERROR"

    def test_multiple_failures(self):
        """多次失败累积。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger()
        ledger.record_failure("shell.run_command", "EXEC_ERROR")
        ledger.record_failure("shell.run_command", "TIMEOUT")
        ledger.record_failure("shell.rg", "EXEC_ERROR")

        assert ledger.get_failure_count("shell.run_command") == 2
        assert ledger.get_failure_count("shell.rg") == 1
        assert ledger.get_last_error_kind("shell.run_command") == "TIMEOUT"

    def test_should_retry(self):
        """重试预算控制（硬错误按 max_retries_per_tool 熔断）。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger(max_retries_per_tool=2)

        assert ledger.should_retry("tool_a") is True

        ledger.record_failure("tool_a", "TOOL_UNAVAILABLE")
        assert ledger.should_retry("tool_a") is True

        ledger.record_failure("tool_a", "TOOL_UNAVAILABLE")
        assert ledger.should_retry("tool_a") is False  # 达到最大重试次数

        assert ledger.should_retry("tool_b") is True  # 其他工具仍可重试

    def test_circuit_breaker(self):
        """熔断器机制。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger(max_total_failures=3)

        assert ledger.is_circuit_broken() is False

        ledger.record_failure("tool_a", "PARAM_MISSING")
        ledger.record_failure("tool_b", "PARAM_MISSING")
        assert ledger.is_circuit_broken() is False

        ledger.record_failure("tool_c", "PARAM_MISSING")
        assert ledger.is_circuit_broken() is True  # 硬错误达到总失败上限

    def test_reset(self):
        """重置账本。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger()
        ledger.record_failure("tool_a", "ERROR")
        ledger.record_failure("tool_b", "ERROR")

        ledger.reset()

        assert ledger.get_failure_count("tool_a") == 0
        assert ledger.get_failure_count("tool_b") == 0
        assert ledger._total_failures == 0

    def test_retry_hint(self):
        """重试提示。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger()
        ledger.record_failure("shell.run_command", "EXEC_ERROR")

        hint = ledger.get_retry_hint("shell.run_command")
        assert "shell.run_command" in hint
        assert "1" in hint  # 失败次数
        assert "EXEC_ERROR" in hint


class TestL6MemoryIntegration:
    """L6 错误沉淀到长期记忆测试。"""

    def test_extract_env_from_errors(self):
        """从失败账本提取环境事实。"""
        from cagent.core.failure_ledger import FailureLedger

        ledger = FailureLedger()
        # 模拟 fd 命令不可用
        ledger.record_failure("shell.fd", "TOOL_UNAVAILABLE")
        ledger.record_failure("shell.rg", "TOOL_UNAVAILABLE")

        # 提取环境事实
        facts = {}
        for tool_name, error_kind in ledger._failure_reasons.items():
            if error_kind == "TOOL_UNAVAILABLE":
                facts[tool_name] = "unavailable"

        assert facts.get("shell.fd") == "unavailable"
        assert facts.get("shell.rg") == "unavailable"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
