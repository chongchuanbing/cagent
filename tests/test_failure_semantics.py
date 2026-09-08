"""失败语义回归：软硬错误分层 + 未收敛不标成功 + 熔断回合落 trace。

背景事故（会话 646b53c406994e65baecdcfafac82d03，s13）：
- 探测阶段 2 次 `find` 失败（TIMEOUT + SANDBOX，均为用法级错误）把 run_tool
  打到重试上限 → 后续写文件的 run_tool 调用被预熔断，从未真正执行；
- 熔断回合不落 last_trace（UI 可见、trace 无记录）；
- 迭代耗尽后未收敛兜底强制 success=True → step 假 done → planner 追加重复步骤。
"""
import json

from cagent.core.failure_ledger import (
    FailureLedger,
    HARD_ERROR_KINDS,
    SOFT_ERROR_KINDS,
)
from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.schema import Step
from cagent.tools import ToolRegistry
from cagent.tools.base import Tool, ToolResult
from cagent.core import ReActEngine


class FakeLLM(LLMClient):
    """按预设脚本返回 LLMResponse。"""

    def __init__(self, scripted):
        super().__init__(LLMConfig())
        self.scripted = list(scripted)

    def complete(self, messages):
        return self._next()

    def complete_with_tools(self, messages, tools):
        return self._next()

    def _next(self) -> LLMResponse:
        assert self.scripted, "FakeLLM: 脚本响应已用尽"
        nxt = self.scripted.pop(0)
        return nxt() if callable(nxt) else nxt


def _tool_call(name: str, args: dict = None, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content="调用工具",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ],
    )


class FlakyTool(Tool):
    """前 fail_times 次返回指定软错误，之后成功——模拟 run_tool 门面下
    find 越界/超时（失败）与 apply_patch 写文件（成功）共用一个工具名。"""

    def __init__(self, fail_times: int, error_kind: str = "SANDBOX"):
        self.fail_times = fail_times
        self.error_kind = error_kind
        self.calls = 0

    name = "run_tool"
    description = "测试用门面工具"

    def run(self) -> ToolResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult(
                ok=False,
                content=f"第 {self.calls} 次失败",
                error="模拟失败",
                error_kind=self.error_kind,
            )
        return ToolResult(ok=True, content="写入成功")

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }


# ---------- 1. 账本层：软硬分层 ----------

def test_soft_errors_do_not_break_circuit():
    """软错误不计入总熔断：大量 SANDBOX/TIMEOUT 失败不触发全局熔断。"""
    ledger = FailureLedger(max_total_failures=3)
    for _ in range(10):
        ledger.record_failure("run_tool", "SANDBOX")
        ledger.record_failure("shell.run_command", "TIMEOUT")
    assert ledger.is_circuit_broken() is False


def test_hard_errors_count_toward_circuit():
    """硬错误（TOOL_UNAVAILABLE/PARAM_MISSING）计入总熔断。"""
    ledger = FailureLedger(max_total_failures=2)
    ledger.record_failure("tool_a", "TOOL_UNAVAILABLE")
    ledger.record_failure("tool_b", "PARAM_MISSING")
    assert ledger.is_circuit_broken() is True


def test_soft_error_higher_retry_budget():
    """软错误使用宽松重试预算（soft_max_retries，默认 3），硬错误仍用紧预算。"""
    ledger = FailureLedger(max_retries_per_tool=2, soft_max_retries=3)

    # 软错误：2 次（旧实现在此熔断）后仍可重试——事故场景的直接回归
    ledger.record_failure("run_tool", "TIMEOUT")
    ledger.record_failure("run_tool", "SANDBOX")
    assert ledger.should_retry("run_tool") is True

    # 第 3 次软失败后达到软预算上限
    ledger.record_failure("run_tool", "SANDBOX")
    assert ledger.should_retry("run_tool") is False

    # 硬错误：2 次即熔断
    ledger2 = FailureLedger(max_retries_per_tool=2, soft_max_retries=3)
    ledger2.record_failure("tool_x", "TOOL_UNAVAILABLE")
    ledger2.record_failure("tool_x", "TOOL_UNAVAILABLE")
    assert ledger2.should_retry("tool_x") is False


def test_retry_hint_soft_error_says_tool_still_usable():
    """软错误的提示不再误导模型'改用其他工具'——事故中正是该文案把模型推向绕路。"""
    ledger = FailureLedger()
    ledger.record_failure("run_tool", "SANDBOX")
    hint = ledger.get_retry_hint("run_tool")
    assert "工具本身仍然可用" in hint
    assert "改用其他工具" not in hint

    ledger.record_failure("tool_h", "TOOL_UNAVAILABLE")
    assert "改用其他工具" in ledger.get_retry_hint("tool_h")


def test_unknown_error_kind_treated_as_soft():
    """未知错误类型按软错误处理（不进入总熔断）。"""
    ledger = FailureLedger(max_total_failures=1)
    ledger.record_failure("tool_u", "SOMETHING_NEW")
    assert ledger.is_circuit_broken() is False
    assert SOFT_ERROR_KINDS and HARD_ERROR_KINDS  # 分层集合非空


# ---------- 2. 引擎层：事故场景直接回归 ----------

def test_incident_soft_failures_do_not_preempt_write():
    """事故回归：同工具名下 2 次软失败（TIMEOUT+SANDBOX）后，
    第 3 次调用（写文件）必须真正执行且可成功。"""
    tool = FlakyTool(fail_times=2, error_kind="SANDBOX")
    llm = FakeLLM(
        [
            _tool_call("run_tool", call_id="c1"),  # find 超时（失败 1）
            _tool_call("run_tool", call_id="c2"),  # find 越界（失败 2）
            _tool_call("run_tool", call_id="c3"),  # apply_patch 写文件（成功）
            LLMResponse(content="19 个幻灯片文件已全部写入完成"),
        ]
    )
    reg = ToolRegistry()
    reg.register(tool)
    engine = ReActEngine(llm, reg, max_iterations=6)

    result = engine.run(Step(id="s13", description="写入 19 个幻灯片文件"))

    assert result.success
    assert tool.calls == 3  # 写入调用没有像旧实现那样被预熔断跳过
    assert "已全部写入" in result.output


def test_unconverged_step_returns_failure():
    """迭代耗尽未收敛：返回 success=False（阶段总结保留在 output），
    不再出现'模型自述尚未完成却标 done'的假成功。"""
    tool = FlakyTool(fail_times=99)  # 永远失败
    llm = FakeLLM(
        [
            _tool_call("run_tool", call_id="c1"),
            LLMResponse(content="阶段性说明：尚未完成文件写入"),
        ]
    )
    reg = ToolRegistry()
    reg.register(tool)
    engine = ReActEngine(llm, reg, max_iterations=1)

    result = engine.run(Step(id="s13", description="写入文件"))

    assert not result.success
    assert "尚未完成文件写入" in result.output


def test_circuit_break_round_written_to_trace():
    """熔断回合写入 last_trace：UI 与 trace.jsonl 一致，可事后审计。"""
    # tool_missing 未注册 → 走 TOOL_UNAVAILABLE 硬错误分支
    llm = FakeLLM(
        [
            _tool_call("tool_missing", call_id="c1"),
            _tool_call("tool_missing", call_id="c2"),
            _tool_call("tool_missing", call_id="c3"),  # 第 3 次：应触发单工具熔断
            LLMResponse(content="被迫收尾"),
        ]
    )
    reg = ToolRegistry()
    engine = ReActEngine(llm, reg, max_iterations=4)

    engine.run(Step(id="s1", description="测试"))

    # 熔断回合在 trace 中可见
    breaker_rows = [
        t for t in engine.last_trace
        if "action" in t and "最大重试次数" in str(t.get("observation", ""))
    ]
    assert breaker_rows, "熔断回合未写入 last_trace"
    assert breaker_rows[0].get("ok") is False
