"""会话恢复和交互式 chat 测试。"""
import json
import os
import tempfile

import pytest

from cagent.core import Agent, AgentLoop, Planner, ReActEngine, Executor
from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.tools import ToolRegistry, add
from cagent.storage import get_storage, SessionRecorder
from cagent.schema import Step


class FakeLLM(LLMClient):
    """按预设脚本返回 LLMResponse。"""

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


# ── SessionRecorder.load_history 测试 ──────────────────────


class TestSessionRecorderHistory:
    def test_load_history_empty(self):
        """无 trace 时返回空列表。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            recorder = SessionRecorder(storage, "test-sid")
            history = recorder.load_history()
            assert history == []

    def test_load_history_from_trace(self):
        """从 trace.jsonl 重建 history。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            recorder = SessionRecorder(storage, "test-sid")
            # 写入 meta
            recorder.record_meta("测试目标")
            # 写入 trace（模拟 ReActEngine.last_trace 格式）
            recorder.record_trace({"thought": "我需要计算"})
            recorder.record_trace({
                "action": {"tool_name": "add", "args": {"a": 3, "b": 5}},
                "observation": "8",
            })
            recorder.record_trace({"final": "3+5=8"})

            history = recorder.load_history()
            assert len(history) == 1
            entry = history[0]
            assert entry["description"] == "测试目标"
            assert entry["output"] == "3+5=8"
            assert len(entry["observations"]) == 1
            assert entry["observations"][0]["tool"] == "add"
            assert entry["observations"][0]["result"] == "8"

    def test_load_history_observation_truncation(self):
        """超长观察被截断。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            recorder = SessionRecorder(storage, "test-sid")
            recorder.record_meta("目标")
            long_obs = "x" * 1000
            recorder.record_trace({
                "action": {"tool_name": "long_tool", "args": {}},
                "observation": long_obs,
            })
            recorder.record_trace({"final": "done"})

            history = recorder.load_history(max_obs_chars=50)
            obs = history[0]["observations"][0]["result"]
            assert "截断" in obs
            assert len(obs) < 100

    def test_load_last_answer(self):
        """读取最后一条 final。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            recorder = SessionRecorder(storage, "test-sid")
            recorder.record_meta("目标")
            recorder.record_trace({"final": "第一个答案"})
            recorder.record_trace({"thought": "继续"})
            recorder.record_trace({"final": "最终答案"})

            assert recorder.load_last_answer() == "最终答案"

    def test_load_last_answer_empty(self):
        """无 trace 时返回空字符串。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            recorder = SessionRecorder(storage, "test-sid")
            assert recorder.load_last_answer() == ""


# ── Agent.run(resume=True) 测试 ─────────────────────────────


class TestAgentResume:
    def test_resume_injects_prior_history(self):
        """resume=True 时 prior_history 注入到 planner context 和初始 history。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)

            # 第一轮：正常执行，留下 trace
            plan_resp = LLMResponse(
                content=json.dumps({"steps": [{"id": "s1", "description": "打招呼", "depends_on": []}]})
            )
            react_final = LLMResponse(content="你好")
            summarize_resp = LLMResponse(content="最终结果：你好")
            llm1 = FakeLLM([plan_resp, react_final, summarize_resp])
            reg = ToolRegistry()
            reg.register(add)
            agent1 = Agent(llm=llm1, tools=reg, storage=storage)
            agent1.run("打个招呼", session_id="test-sid")

            # 第二轮：resume=True，应该带着上轮历史
            plan2_resp = LLMResponse(
                content=json.dumps({"steps": [{"id": "s1", "description": "基于上轮结果继续", "depends_on": []}]})
            )
            react2_final = LLMResponse(content="继续")
            summarize2_resp = LLMResponse(content="最终结果：继续你好")
            llm2 = FakeLLM([plan2_resp, react2_final, summarize2_resp])
            reg2 = ToolRegistry()
            reg2.register(add)
            agent2 = Agent(llm=llm2, tools=reg2, storage=storage)
            agent2.run("接着上次继续", session_id="test-sid", resume=True)

            # 验证 planner 的 context 包含上轮历史
            plan_call = llm2.calls[0]  # ("complete", messages)
            assert plan_call[0] == "complete"
            # messages 中应有上轮历史内容
            plan_messages = plan_call[1]
            user_msg = next(m.content for m in plan_messages if m.role.value == "user")
            assert "你好" in user_msg  # 上轮 final answer 在上下文中

    def test_resume_without_history(self):
        """resume=True 但无历史时，正常执行不报错。"""
        with tempfile.TemporaryDirectory() as d:
            storage = get_storage(d)
            plan_resp = LLMResponse(
                content=json.dumps({"steps": [{"id": "s1", "description": "执行", "depends_on": []}]})
            )
            react_final = LLMResponse(content="完成")
            summarize_resp = LLMResponse(content="最终结果：完成")
            llm = FakeLLM([plan_resp, react_final, summarize_resp])
            reg = ToolRegistry()
            reg.register(add)
            agent = Agent(llm=llm, tools=reg, storage=storage)
            # resume=True 但 session 无历史
            agent.run("新任务", session_id="no-history-sid", resume=True)


# ── AgentLoop prior_history 测试 ────────────────────────────


class TestLoopPriorHistory:
    def test_prior_history_injected_to_planner(self):
        """prior_history 传入 planner 的 context。"""
        plan_resp = LLMResponse(
            content=json.dumps({"steps": [{"id": "s1", "description": "步骤", "depends_on": []}]})
        )
        react_final = LLMResponse(content="结果")
        summarize_resp = LLMResponse(content="总结：结果")
        llm = FakeLLM([plan_resp, react_final, summarize_resp])

        prior = [{
            "step_id": "prior",
            "description": "上轮目标",
            "success": True,
            "output": "上轮结果：重要信息",
            "observations": [],
        }]

        reg = ToolRegistry()
        reg.register(add)
        planner = Planner(llm)
        executor = Executor(ReActEngine(llm, reg))
        loop = AgentLoop(planner, executor, llm=llm)

        loop.run("继续执行", prior_history=prior)

        # planner 的 complete 调用应该包含上轮历史
        plan_call = llm.calls[0]
        assert plan_call[0] == "complete"
        plan_messages = plan_call[1]
        user_msg = next(m.content for m in plan_messages if m.role.value == "user")
        assert "重要信息" in user_msg

    def test_prior_history_empty(self):
        """prior_history=None 时正常执行。"""
        plan_resp = LLMResponse(
            content=json.dumps({"steps": [{"id": "s1", "description": "步骤", "depends_on": []}]})
        )
        react_final = LLMResponse(content="结果")
        summarize_resp = LLMResponse(content="总结")
        llm = FakeLLM([plan_resp, react_final, summarize_resp])

        reg = ToolRegistry()
        reg.register(add)
        planner = Planner(llm)
        executor = Executor(ReActEngine(llm, reg))
        loop = AgentLoop(planner, executor, llm=llm)

        loop.run("新任务", prior_history=None)


# ── chat 模块导入测试 ──────────────────────────────────────


class TestChatModule:
    def test_chat_module_importable(self):
        """chat 模块可正常导入。"""
        from clients.cli.chat import cmd_chat
        assert callable(cmd_chat)

    def test_chat_subcommand_registered(self):
        """chat 子命令在 argparse 中注册。"""
        from clients.cli.main import build_parser
        parser = build_parser()
        # 解析 chat 命令
        args = parser.parse_args(["chat", "--session-id", "abc"])
        assert args.command == "chat"
        assert args.session_id == "abc"

    def test_run_subcommand_has_resume_flag(self):
        """run 子命令有 --resume flag。"""
        from clients.cli.main import build_parser
        parser = build_parser()
        args = parser.parse_args(["run", "测试", "--resume"])
        assert args.resume is True
        args2 = parser.parse_args(["run", "测试"])
        assert args2.resume is False
