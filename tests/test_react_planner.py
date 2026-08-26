"""验证 Planner / ReActEngine 的 LLM 解析与工具执行（使用 FakeLLM，无需真实 API）。"""
import json
import tempfile

from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.tools import ToolRegistry, add
from cagent.core import Planner, ReActEngine, Agent
from cagent.storage import get_storage
from cagent.schema import Step


class FakeLLM(LLMClient):
    """按预设脚本返回 LLMResponse，记录调用。"""

    def __init__(self, scripted):
        super().__init__(LLMConfig())
        self.scripted = list(scripted)
        self.calls = []

    def _next(self) -> LLMResponse:
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


def test_planner_parses_json():
    llm = FakeLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {
                        "steps": [
                            {"id": "s1", "description": "先算 3+5", "depends_on": []},
                            {"id": "s2", "description": "再解释", "depends_on": ["s1"]},
                        ]
                    }
                )
            )
        ]
    )
    planner = Planner(llm)
    plan = planner.plan("计算并解释 3+5")
    assert len(plan.steps) == 2
    assert plan.steps[1].depends_on == ["s1"]


def test_react_calls_tool_then_finishes():
    tool_call = LLMResponse(
        content="我需要计算结果",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "add", "arguments": json.dumps({"a": 3, "b": 5})},
            }
        ],
    )
    final = LLMResponse(content="3 + 5 = 8")
    llm = FakeLLM([tool_call, final])

    reg = ToolRegistry()
    reg.register(add)
    engine = ReActEngine(llm, reg)
    result = engine.run(Step(id="s1", description="计算 3+5"))

    assert result.success
    assert "8" in result.output
    # 工具调用 + 最终结论 = 两次 complete_with_tools
    assert len([c for c in llm.calls if c[0] == "complete_with_tools"]) == 2
    # 轨迹应记录 action 与 observation
    actions = [t for t in engine.last_trace if "action" in t]
    assert actions and actions[0]["observation"] == "8"


def test_agent_loop_with_fake_llm():
    plan_resp = LLMResponse(
        content=json.dumps({"steps": [{"id": "s1", "description": "打招呼", "depends_on": []}]})
    )
    react_tool = LLMResponse(
        content="调用工具",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 1})},
            }
        ],
    )
    react_final = LLMResponse(content="完成：2")
    summarize_resp = LLMResponse(content="最终结果：1+1=2")
    llm = FakeLLM([plan_resp, react_tool, react_final, summarize_resp])

    reg = ToolRegistry()
    reg.register(add)
    storage = get_storage(tempfile.mkdtemp())
    agent = Agent(llm=llm, tools=reg, storage=storage, max_steps=10)
    out = agent.run("测试")

    assert "2" in out
    # summarize 由 LLM 生成，不再是纯拼接
    assert "最终结果" in out
    # 会话应已落盘 meta/plan/trace
    keys = storage.list_keys("sessions/")
    assert any(k.endswith("meta.json") for k in keys)
    assert any(k.endswith("plan.json") for k in keys)
    assert any(k.endswith("trace.jsonl") for k in keys)


def test_summarize_uses_config_prompt():
    """验证 summarize 提示词可被配置覆盖。"""
    from cagent.config.provider import ConfigProvider
    from cagent.prompts import SUMMARIZE_SYSTEM_PROMPT

    import tempfile, os, yaml as _yaml

    # 写一个临时配置，覆盖 summarize_system
    tmpdir = tempfile.mkdtemp()
    cfg_path = os.path.join(tmpdir, "agent.yaml")
    with open(cfg_path, "w") as f:
        _yaml.safe_dump({"prompts": {"summarize_system": "自定义总结提示"}}, f)

    provider = ConfigProvider(path=cfg_path, watch=False)
    prompt = provider.get_prompt("summarize_system", default=SUMMARIZE_SYSTEM_PROMPT)
    assert prompt == "自定义总结提示"
    provider.stop()


def test_summarize_fallback_without_llm():
    """无 LLM 时 _summarize 退化为纯文本拼接。"""
    from cagent.core.loop import AgentLoop
    from cagent.schema.plan import Plan, Step, StepStatus, StepResult

    plan = Plan(goal="测试", steps=[
        Step(id="s1", description="步骤1", depends_on=[],
             status=StepStatus.DONE, result=StepResult(step_id="s1", success=True, output="ok")),
    ])
    loop = AgentLoop(planner=None, executor=None, llm=None)
    result = loop._summarize(plan)
    assert "测试" in result and "ok" in result


def test_step2_context_contains_user_feedback():
    """跨 step 上下文串联：step1 的 ask_user 反馈必须进入 step2 的提示上下文。"""
    from cagent.tools.builtin import FeedbackTool

    plan_resp = LLMResponse(
        content=json.dumps(
            {
                "steps": [
                    {"id": "s1", "description": "向用户收集偏好", "depends_on": []},
                    {"id": "s2", "description": "按偏好产出结果", "depends_on": ["s1"]},
                ]
            }
        )
    )
    ask_call = LLMResponse(
        content="需要问用户偏好",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "arguments": json.dumps({"question": "你偏好什么回答风格？"}),
                },
            }
        ],
    )
    step1_final = LLMResponse(content="用户偏好：简洁的中文回答")
    adjust_no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
    step2_final = LLMResponse(content="已按用户偏好输出简洁中文结果")
    summarize_resp = LLMResponse(content="任务完成")
    llm = FakeLLM([plan_resp, ask_call, step1_final, adjust_no_change, step2_final, summarize_resp])

    reg = ToolRegistry()
    reg.register(add)
    reg.register(FeedbackTool(input_fn=lambda q: "简洁的中文回答"))
    storage = get_storage(tempfile.mkdtemp())
    agent = Agent(llm=llm, tools=reg, storage=storage, max_steps=10)
    agent.run("生成回答")

    # step2 的 complete_with_tools 上下文必须包含 step1 结论与 ask_user 的用户反馈
    react_calls = [c for c in llm.calls if c[0] == "complete_with_tools"]
    assert len(react_calls) == 3  # s1 两次（工具调用+收敛）+ s2 一次
    step2_messages = react_calls[2][1]
    user_prompt = next(m.content for m in step2_messages if m.role.value == "user")
    assert "简洁的中文回答" in user_prompt  # ask_user 观察记录（用户反馈原文）
    assert "用户偏好：简洁的中文回答" in user_prompt  # step1 结论
    assert "总目标" in user_prompt  # 总目标也进入上下文


# ---------- history 窗口化 ----------

def _write_cfg(tmpdir: str, **kwargs) -> str:
    """写临时 agent.yaml，返回路径。"""
    import os

    import yaml as _yaml

    path = os.path.join(tmpdir, "agent.yaml")
    with open(path, "w", encoding="utf-8") as f:
        _yaml.safe_dump(kwargs, f)
    return path


def _three_step_plan() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "steps": [
                    {"id": "s1", "description": "第一步", "depends_on": []},
                    {"id": "s2", "description": "第二步", "depends_on": ["s1"]},
                    {"id": "s3", "description": "第三步", "depends_on": ["s2"]},
                ]
            }
        )
    )


def test_react_unconverged_falls_back_to_summary():
    """迭代耗尽未收敛：LLM 总结已有轨迹作为本步骤结果，而非直接失败。"""
    tool_call = LLMResponse(
        content="我需要计算结果",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "add", "arguments": json.dumps({"a": 3, "b": 5})},
            }
        ],
    )
    fallback_summary = LLMResponse(content="阶段性结论：已得到中间结果 8，尚需进一步验证")
    llm = FakeLLM([tool_call, fallback_summary])

    reg = ToolRegistry()
    reg.register(add)
    engine = ReActEngine(llm, reg, max_iterations=1)
    result = engine.run(Step(id="s1", description="计算 3+5"))

    # 总结成为 step 执行结果，且标记成功（避免触发 replan 循环）
    assert result.success
    assert "阶段性结论" in result.output
    # 轨迹记录了未收敛标记
    finals = [t for t in engine.last_trace if "final" in t]
    assert finals and finals[0].get("unconverged") is True


def test_react_unconverged_fallback_failure():
    """兜底总结也失败（LLM 报错）时，才返回失败结果。"""

    class BrokenLLM(FakeLLM):
        def complete(self, messages):
            raise RuntimeError("LLM 不可用")

    tool_call = LLMResponse(
        content="继续调工具",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 2})},
            }
        ],
    )
    llm = BrokenLLM([tool_call])
    reg = ToolRegistry()
    reg.register(add)
    engine = ReActEngine(llm, reg, max_iterations=1)
    result = engine.run(Step(id="s1", description="计算"))

    assert not result.success
    assert "未收敛" in (result.error or "")


def test_history_window_sliding():
    """history_window=1 且不摘要：后续步骤只看到最近 1 条完整条目。"""
    from cagent.config.provider import ConfigProvider

    _no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
    llm = FakeLLM(
        [
            _three_step_plan(),
            LLMResponse(content="第一步结论AAA"),
            _no_change,  # adjust after s1
            LLMResponse(content="第二步结论BBB"),
            _no_change,  # adjust after s2
            LLMResponse(content="第三步结论CCC"),
            LLMResponse(content="最终总结"),
        ]
    )
    reg = ToolRegistry()
    reg.register(add)
    storage = get_storage(tempfile.mkdtemp())
    cfg_path = _write_cfg(tempfile.mkdtemp(), history_window=1, history_summarize=False)
    cfg = ConfigProvider(path=cfg_path, watch=False)
    agent = Agent(llm=llm, tools=reg, storage=storage, config=cfg, max_steps=10)
    agent.run("三步任务")

    react_calls = [c for c in llm.calls if c[0] == "complete_with_tools"]
    assert len(react_calls) == 3
    step3_prompt = next(
        m.content for m in react_calls[2][1] if m.role.value == "user"
    )
    assert "第二步结论BBB" in step3_prompt  # 窗口内最近 1 条
    assert "第一步结论AAA" not in step3_prompt  # 窗口外被淘汰
    assert "更早步骤摘要" not in step3_prompt  # 未开启滚动摘要


def test_history_window_rolling_summary():
    """history_summarize=true：被挤出条目经 LLM 压缩为滚动摘要置顶。"""
    from cagent.config.provider import ConfigProvider

    _no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
    llm = FakeLLM(
        [
            _three_step_plan(),
            LLMResponse(content="第一步结论AAA"),
            _no_change,  # adjust after s1
            LLMResponse(content="第二步结论BBB"),
            LLMResponse(content="压缩摘要：第一二步已完成"),  # 滚动摘要（s1 被挤出）
            _no_change,  # adjust after s2
            LLMResponse(content="第三步结论CCC"),
            LLMResponse(content="压缩摘要：第一二三步已完成"),  # 滚动摘要（s2 被挤出）
            LLMResponse(content="最终总结"),
        ]
    )
    reg = ToolRegistry()
    reg.register(add)
    storage = get_storage(tempfile.mkdtemp())
    cfg_path = _write_cfg(tempfile.mkdtemp(), history_window=1, history_summarize=True)
    cfg = ConfigProvider(path=cfg_path, watch=False)
    agent = Agent(llm=llm, tools=reg, storage=storage, config=cfg, max_steps=10)
    agent.run("三步任务")

    react_calls = [c for c in llm.calls if c[0] == "complete_with_tools"]
    step3_prompt = next(
        m.content for m in react_calls[2][1] if m.role.value == "user"
    )
    assert "更早步骤摘要" in step3_prompt
    assert "压缩摘要：第一二步已完成" in step3_prompt
    assert "第二步结论BBB" in step3_prompt  # 最近 1 条完整保留


def test_observation_truncated_by_config():
    """observation_max_chars：超长工具观察被截断后才进入后续步骤上下文。"""
    from cagent.config.provider import ConfigProvider
    from cagent.tools.base import Tool, ToolResult

    class LongTool(Tool):
        name = "long_tool"
        description = "返回超长结果"

        def run(self) -> ToolResult:
            return ToolResult(ok=True, content="x" * 200)

        def schema(self) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": self.description,
                    "parameters": {"type": "object", "properties": {}},
                },
            }

    _no_change = LLMResponse(content=json.dumps({"action": "no_change"}))
    llm = FakeLLM(
        [
            LLMResponse(
                content=json.dumps(
                    {
                        "steps": [
                            {"id": "s1", "description": "调用长工具", "depends_on": []},
                            {"id": "s2", "description": "汇总", "depends_on": ["s1"]},
                        ]
                    }
                )
            ),
            LLMResponse(
                content="调用工具",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "long_tool", "arguments": "{}"},
                    }
                ],
            ),
            LLMResponse(content="第一步完成"),
            _no_change,  # adjust after s1
            LLMResponse(content="第二步完成"),
            LLMResponse(content="最终总结"),
        ]
    )
    reg = ToolRegistry()
    reg.register(LongTool())
    storage = get_storage(tempfile.mkdtemp())
    cfg_path = _write_cfg(tempfile.mkdtemp(), observation_max_chars=20)
    cfg = ConfigProvider(path=cfg_path, watch=False)
    agent = Agent(llm=llm, tools=reg, storage=storage, config=cfg, max_steps=10)
    agent.run("两步任务")

    react_calls = [c for c in llm.calls if c[0] == "complete_with_tools"]
    step2_prompt = next(
        m.content for m in react_calls[2][1] if m.role.value == "user"
    )
    assert "已截断" in step2_prompt
    assert "x" * 200 not in step2_prompt  # 全文不得进入上下文
