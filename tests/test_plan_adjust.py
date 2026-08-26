"""动态 Plan 调整测试：apply_adjust + planner.adjust + AgentLoop 集成。"""
import json
import tempfile

import pytest

from cagent.schema.plan import Plan, Step, StepStatus, StepResult
from cagent.core.planner import Planner
from cagent.core.executor import Executor
from cagent.core.react import ReActEngine
from cagent.core.loop import AgentLoop
from cagent.events import EventType
from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.tools import ToolRegistry, add
from cagent.storage import get_storage


class FakeLLM(LLMClient):
    def __init__(self, scripted):
        super().__init__(LLMConfig())
        self.scripted = list(scripted)
        self.calls = []

    def _next(self):
        if not self.scripted:
            raise AssertionError("FakeLLM: 脚本响应已用尽")
        nxt = self.scripted.pop(0)
        return nxt() if callable(nxt) else nxt

    def complete(self, messages):
        self.calls.append(("complete", messages))
        return self._next()

    def complete_with_tools(self, messages, tools):
        self.calls.append(("complete_with_tools", messages, tools))
        return self._next()


# ── Plan.apply_adjust 测试 ────────────────────────────────


class TestApplyAdjust:
    def _make_plan(self, steps_config=None):
        steps_config = steps_config or []
        steps = [
            Step(id=c["id"], description=c["desc"], depends_on=c.get("depends_on", []))
            for c in steps_config
        ]
        return Plan(goal="测试", steps=steps)

    def test_add_step(self):
        plan = self._make_plan([
            {"id": "s1", "desc": "第一步", "depends_on": []},
            {"id": "s2", "desc": "第二步", "depends_on": ["s1"]},
        ])
        changed = plan.apply_adjust(
            add=[{"id": "s3", "description": "新增的第三步", "depends_on": ["s2"]}],
            remove=[], modify=[],
        )
        assert changed
        assert plan.version == 2
        assert len(plan.steps) == 3
        assert plan.steps[2].id == "s3"
        assert plan.steps[2].description == "新增的第三步"

    def test_remove_pending_step(self):
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "待删除", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        changed = plan.apply_adjust(add=[], remove=["s2"], modify=[])
        assert changed
        assert len(plan.steps) == 1
        assert plan.steps[0].id == "s1"

    def test_remove_done_step_not_allowed(self):
        """已完成的 step 不可移除。"""
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "待执行", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        changed = plan.apply_adjust(add=[], remove=["s1"], modify=[])
        assert not changed  # s1 是 DONE，不移除
        assert len(plan.steps) == 2

    def test_modify_pending_step(self):
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "原描述", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        changed = plan.apply_adjust(
            add=[], remove=[],
            modify=[{"id": "s2", "description": "新描述", "depends_on": []}],
        )
        assert changed
        assert plan.steps[1].description == "新描述"
        assert plan.steps[1].depends_on == []

    def test_modify_done_step_not_allowed(self):
        """已完成的 step 不可修改。"""
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "待执行", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        changed = plan.apply_adjust(
            add=[], remove=[],
            modify=[{"id": "s1", "description": "改已完成"}],
        )
        assert not changed

    def test_no_change_returns_false(self):
        plan = self._make_plan([{"id": "s1", "desc": "步骤", "depends_on": []}])
        changed = plan.apply_adjust(add=[], remove=[], modify=[])
        assert not changed
        assert plan.version == 1

    def test_combined_add_remove_modify(self):
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "待删除", "depends_on": ["s1"]},
            {"id": "s3", "desc": "待修改", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        changed = plan.apply_adjust(
            add=[{"id": "s4", "description": "新增", "depends_on": ["s3"]}],
            remove=["s2"],
            modify=[{"id": "s3", "description": "修改后的描述"}],
        )
        assert changed
        assert plan.version == 2
        ids = [s.id for s in plan.steps]
        assert "s1" in ids
        assert "s2" not in ids  # 已移除
        assert "s3" in ids
        assert "s4" in ids
        s3 = next(s for s in plan.steps if s.id == "s3")
        assert s3.description == "修改后的描述"

    def test_pending_steps(self):
        plan = self._make_plan([
            {"id": "s1", "desc": "已完成", "depends_on": []},
            {"id": "s2", "desc": "待执行", "depends_on": ["s1"]},
        ])
        plan.steps[0].status = StepStatus.DONE
        pending = plan.pending_steps()
        assert len(pending) == 1
        assert pending[0].id == "s2"


# ── Planner.adjust 测试 ────────────────────────────────────


class TestPlannerAdjust:
    def test_adjust_add_step(self):
        """adjust 返回新增步骤。"""
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "第一步", "depends_on": []},
                {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
            ]
        }))
        adjust_resp = LLMResponse(content=json.dumps({
            "action": "adjust",
            "add": [{"id": "s3", "description": "新增的第三步", "depends_on": ["s2"]}],
            "remove": [],
            "modify": [],
        }))
        llm = FakeLLM([plan_resp, adjust_resp])

        planner = Planner(llm)
        plan = planner.plan("测试")
        # 模拟 s1 完成
        plan.steps[0].status = StepStatus.DONE
        plan.steps[0].result = StepResult(step_id="s1", success=True, output="第一步完成")
        history = [{"step_id": "s1", "description": "第一步", "success": True, "output": "第一步完成", "observations": []}]

        result = planner.adjust(plan, plan.steps[0], history)
        assert result is not None
        assert result.version == 2
        assert len(result.steps) == 3
        assert result.steps[2].id == "s3"

    def test_adjust_no_change(self):
        """adjust 返回无变更。"""
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "第一步", "depends_on": []},
                {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
            ]
        }))
        s1_result = LLMResponse(content="完成")
        no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
        llm = FakeLLM([plan_resp, s1_result, no_change])

        planner = Planner(llm)
        plan = planner.plan("测试")
        plan.steps[0].status = StepStatus.DONE
        plan.steps[0].result = StepResult(step_id="s1", success=True, output="完成")
        history = [{"step_id": "s1", "description": "第一步", "success": True, "output": "完成", "observations": []}]

        result = planner.adjust(plan, plan.steps[0], history)
        assert result is None

    def test_adjust_no_pending_returns_none(self):
        """无 PENDING steps 时不调 LLM。"""
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [{"id": "s1", "description": "唯一步骤", "depends_on": []}]
        }))
        llm = FakeLLM([plan_resp])
        planner = Planner(llm)
        plan = planner.plan("测试")
        plan.steps[0].status = StepStatus.DONE
        history = []
        result = planner.adjust(plan, plan.steps[0], history)
        assert result is None
        # 不应调 LLM（adjust 调用数为 0）
        assert len(llm.calls) == 1  # 只有 plan 调用

    def test_adjust_remove_step(self):
        """adjust 移除步骤。"""
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "已完成", "depends_on": []},
                {"id": "s2", "description": "待删除", "depends_on": ["s1"]},
                {"id": "s3", "description": "保留", "depends_on": ["s1"]},
            ]
        }))
        adjust_resp = LLMResponse(content=json.dumps({
            "action": "adjust",
            "add": [],
            "remove": ["s2"],
            "modify": [],
        }))
        llm = FakeLLM([plan_resp, adjust_resp])
        planner = Planner(llm)
        plan = planner.plan("测试")
        plan.steps[0].status = StepStatus.DONE
        plan.steps[0].result = StepResult(step_id="s1", success=True, output="完成")
        history = []
        result = planner.adjust(plan, plan.steps[0], history)
        assert result is not None
        assert len(result.steps) == 2
        ids = [s.id for s in result.steps]
        assert "s2" not in ids
        assert "s3" in ids

    def test_adjust_parse_failure_returns_none(self):
        """LLM 输出解析失败时返回 None。"""
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "第一步", "depends_on": []},
                {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
            ]
        }))
        bad_adjust = LLMResponse(content="这不是 JSON")
        llm = FakeLLM([plan_resp, bad_adjust])
        planner = Planner(llm)
        plan = planner.plan("测试")
        plan.steps[0].status = StepStatus.DONE
        plan.steps[0].result = StepResult(step_id="s1", success=True, output="完成")
        result = planner.adjust(plan, plan.steps[0], [])
        assert result is None


# ── AgentLoop 集成 adjust 测试 ─────────────────────────────


class TestAgentLoopAdjust:
    def test_loop_emits_plan_adjusted_event(self):
        """AgentLoop 每步成功后调 adjust，有变更时 emit PLAN_ADJUSTED 事件。"""
        from cagent.events import EventEmitter
        from cagent.schema import Step

        _no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "第一步", "depends_on": []},
                {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
            ]
        }))
        s1_final = LLMResponse(content="第一步完成")
        # adjust 返回新增 s3
        adjust_resp = LLMResponse(content=json.dumps({
            "action": "adjust",
            "add": [{"id": "s3", "description": "新增步骤", "depends_on": ["s2"]}],
            "remove": [],
            "modify": [],
        }))
        s2_final = LLMResponse(content="第二步完成")
        # s2 完成后 adjust（s3 是 PENDING）
        adjust2_resp = LLMResponse(content=json.dumps({"action": "no_change"}))
        s3_final = LLMResponse(content="第三步完成")
        # s3 完成后无 PENDING，不调 adjust
        summarize_resp = LLMResponse(content="最终结果")

        llm = FakeLLM([
            plan_resp, s1_final, adjust_resp,
            s2_final, adjust2_resp,
            s3_final,
            summarize_resp,
        ])

        emitter = EventEmitter()
        events = []
        emitter.subscribe(lambda e: events.append(e))

        reg = ToolRegistry()
        reg.register(add)
        planner = Planner(llm)
        executor = Executor(ReActEngine(llm, reg))
        loop = AgentLoop(planner, executor, llm=llm, emitter=emitter)
        storage = get_storage(tempfile.mkdtemp())
        loop.run("测试", recorder=None)

        # 应该有 PLAN_ADJUSTED 事件
        adjusted_events = [e for e in events if e.type == EventType.PLAN_ADJUSTED]
        assert len(adjusted_events) >= 1
        # 第一个 adjust 增加了 s3
        plan_data = adjusted_events[0].payload["plan"]
        step_ids = [s["id"] for s in plan_data["steps"]]
        assert "s3" in step_ids

    def test_loop_adjust_failure_does_not_crash(self):
        """adjust LLM 调用失败时不影响主流程。"""
        _no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
        plan_resp = LLMResponse(content=json.dumps({
            "steps": [
                {"id": "s1", "description": "第一步", "depends_on": []},
                {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
            ]
        }))
        s1_final = LLMResponse(content="第一步完成")
        # adjust 的 LLM 响应是坏 JSON，但 adjust 会 catch 异常返回 None
        bad_adjust = LLMResponse(content="坏 JSON")
        s2_final = LLMResponse(content="第二步完成")
        summarize_resp = LLMResponse(content="最终结果")

        llm = FakeLLM([plan_resp, s1_final, bad_adjust, s2_final, summarize_resp])
        reg = ToolRegistry()
        reg.register(add)
        planner = Planner(llm)
        executor = Executor(ReActEngine(llm, reg))
        loop = AgentLoop(planner, executor, llm=llm)

        # 不应抛异常
        result = loop.run("测试")
        assert "最终结果" in result
