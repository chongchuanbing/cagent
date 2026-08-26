"""验证事件机制：发射/订阅/SSE 序列化、核心引擎埋点、CLI 渲染、ask_user 工具。"""
import io
import json
import tempfile

from cagent.events import AgentEvent, EventEmitter, EventType, to_sse
from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.tools import ToolRegistry, add, FeedbackTool
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


# ---------- EventEmitter / SSE ----------

def test_emitter_emit_and_subscribe():
    emitter = EventEmitter()
    received = []
    emitter.subscribe(lambda e: received.append(e))
    e1 = emitter.emit(EventType.THOUGHT, payload={"content": "思考中"})
    e2 = emitter.emit(EventType.TOOL_CALL, payload={"name": "add", "args": {"a": 1, "b": 2}})

    assert len(received) == 2
    assert e1.seq == 1 and e2.seq == 2          # 序号自动递增
    assert received[0].type == EventType.THOUGHT
    assert received[1].payload["name"] == "add"


def test_to_sse_format():
    emitter = EventEmitter()
    event = emitter.emit(EventType.TOOL_CALL, payload={"name": "x"}, session_id="s1", step_id="s2")
    sse = to_sse(event)
    assert sse.startswith("event: tool_call\n")
    assert "\n\n" in sse
    data = sse.split("data: ", 1)[1].strip()
    obj = json.loads(data)
    assert obj["type"] == "tool_call"
    assert obj["session_id"] == "s1" and obj["step_id"] == "s2"
    assert obj["seq"] == 1


def test_emitter_iter_sse_stream():
    emitter = EventEmitter()
    out = []

    # 模拟 Web 端挂流：消费者先进入迭代
    def consume():
        for chunk in emitter.iter_sse():
            out.append(chunk)
            if len(out) >= 2:
                break

    import threading

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    import time

    time.sleep(0.05)
    emitter.emit(EventType.STEP_STARTED, payload={"step": {"id": "s1", "description": "x"}})
    emitter.emit(EventType.FINAL_ANSWER, payload={"answer": "ok"})
    t.join(timeout=1)

    assert len(out) == 2
    assert out[0].startswith("event: step_started\n")
    assert out[1].startswith("event: final_answer\n")


# ---------- 核心引擎埋点 ----------

def test_react_emits_thought_and_tool_events():
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

    emitter = EventEmitter()
    received = []
    emitter.subscribe(lambda e: received.append(e))

    reg = ToolRegistry()
    reg.register(add)
    engine = ReActEngine(llm, reg, emitter=emitter)
    engine.run(Step(id="s1", description="计算 3+5"))

    types = [e.type for e in received]
    assert types == [EventType.THOUGHT, EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.THOUGHT]
    assert received[1].payload["name"] == "add"
    assert received[1].step_id == "s1"
    assert received[2].payload["content"] == "8"
    assert received[2].payload["ok"] is True


def test_loop_emits_plan_step_and_final_events():
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

    emitter = EventEmitter()
    received = []
    emitter.subscribe(lambda e: received.append(e))

    reg = ToolRegistry()
    reg.register(add)
    agent = Agent(llm=llm, tools=reg, storage=get_storage(tempfile.mkdtemp()), emitter=emitter, max_steps=10)
    agent.run("测试")

    types = [e.type for e in received]
    assert EventType.PLAN_CREATED in types
    assert EventType.STEP_STARTED in types
    assert EventType.STEP_FINISHED in types
    assert EventType.FINAL_ANSWER in types
    final = next(e for e in received if e.type == EventType.FINAL_ANSWER)
    assert "2" in final.payload["answer"]
    # session_id 已附加到事件
    assert all(e.session_id for e in received)


# ---------- CLI 展示策略 ----------

def test_display_renders_events_with_colors():
    from clients.cli.display import Display

    buf = io.StringIO()
    d = Display(color=True, stream=buf)
    d.render(AgentEvent(type=EventType.PLAN_CREATED, payload={"plan": {"goal": "g", "steps": [
        {"id": "s1", "description": "步骤1", "depends_on": []},
    ]}}))
    d.render(AgentEvent(type=EventType.TOOL_CALL, payload={"name": "add", "args": {"a": 1, "b": 2}}))
    d.render(AgentEvent(type=EventType.TOOL_RESULT, payload={"name": "add", "content": "3", "ok": True}))
    d.render(AgentEvent(type=EventType.ERROR, payload={"message": "boom"}))
    d.render(AgentEvent(type=EventType.FINAL_ANSWER, payload={"answer": "答案是 3"}))

    text = buf.getvalue()
    assert "\033[" in text                        # 有 ANSI 颜色
    assert "调用工具: add" in text
    assert "[add] 结果: 3" in text
    assert "错误: boom" in text
    assert "最终答案" in text and "答案是 3" in text


def test_display_no_color_when_disabled():
    from clients.cli.display import Display

    buf = io.StringIO()
    d = Display(color=False, stream=buf)
    d.render(AgentEvent(type=EventType.TOOL_CALL, payload={"name": "add", "args": {}}))
    assert "\033[" not in buf.getvalue()
    assert "调用工具: add" in buf.getvalue()


# ---------- ask_user 反馈工具 ----------

def test_feedback_tool_returns_user_answer():
    emitter = EventEmitter()
    received = []
    emitter.subscribe(lambda e: received.append(e))

    tool = FeedbackTool(emitter=emitter, input_fn=lambda q: "北京")
    result = tool.run("你在哪个城市？")

    assert result.ok and result.content == "北京"
    assert received[0].type == EventType.USER_INPUT_REQUEST
    assert received[0].payload["question"] == "你在哪个城市？"


def test_feedback_tool_empty_input_fails():
    tool = FeedbackTool(input_fn=lambda q: "   ")
    result = tool.run("请提供补充信息")
    assert not result.ok
    assert "空" in (result.error or "")
