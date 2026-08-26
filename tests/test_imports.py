"""冒烟测试：验证各模块可正常导入与基础构造。"""
from cagent.core import Agent, AgentLoop, Planner, Executor, ReActEngine
from cagent.llm import OpenAIClient, LLMConfig
from cagent.tools import ToolRegistry, add
from cagent.schema import Plan, Step, Message, MessageRole


def test_imports():
    assert all([Agent, AgentLoop, Planner, Executor, ReActEngine])
    assert Message(role=MessageRole.USER, content="hi")


def test_agent_assembly():
    llm = OpenAIClient(LLMConfig(model="gpt-4o", api_key="x"))
    registry = ToolRegistry()
    registry.register(add)
    agent = Agent(llm=llm, tools=registry)
    assert agent is not None
