"""验证长期记忆持久化、迭代次数配置、CLI 入口。"""
import json
import tempfile

from cagent.config import ConfigProvider
from cagent.memory import LongTermMemory
from cagent.schema.message import Message, MessageRole
from cagent.storage import get_storage


def test_long_term_memory_persists_and_retrieves():
    storage = get_storage(tempfile.mkdtemp())
    mem = LongTermMemory(storage, namespace="facts")

    mem.add(Message(role=MessageRole.USER, content="北京的气候四季分明"))
    mem.add(Message(role=MessageRole.USER, content="Python 是一种动态类型语言"))

    # 跨实例（新 session）应能读回并召回相关项
    mem2 = LongTermMemory(storage, namespace="facts")
    hits = mem2.retrieve("Python 语言特性")
    assert hits, "应召回至少一条相关记忆"
    assert "Python" in hits[0].content


def test_iteration_config_from_yaml(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(
        "model:\n  model: gpt-4o\n"
        "max_steps: 7\n"
        "react_max_iterations: 3\n",
        encoding="utf-8",
    )
    p = ConfigProvider(str(path), watch=False)
    assert p.get_max_steps() == 7
    assert p.get_react_max_iterations() == 3
    # 完整配置可序列化
    assert isinstance(p.get_config().model_dump(), dict)


def test_cli_config_set_writes_yaml(tmp_path):
    import clients.cli.main as cli

    path = tmp_path / "agent.yaml"
    path.write_text("model:\n  model: gpt-4o\n  temperature: 0.0\n", encoding="utf-8")
    rc = cli.cmd_config_set(
        __ns(path, "model.temperature", "0.9")
    )
    assert rc == 0
    data = json.loads(json.dumps(_load_yaml(path)))
    assert data["model"]["temperature"] == 0.9


# 小工具：构造一个带 config 的 argparse.Namespace 给 cmd_* 用
def __ns(path, key, value):
    import argparse

    ns = argparse.Namespace()
    ns.config = str(path)
    ns.key = key
    ns.value = value
    return ns


def _load_yaml(path):
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_cli_help_and_parser():
    import clients.cli.main as cli

    parser = cli.build_parser()
    # run 子命令
    args = parser.parse_args(["run", "hello"])
    assert args.command == "run" and args.goal == "hello"
    # config set 子命令
    args = parser.parse_args(["config", "set", "max_steps", "12"])
    assert args.key == "max_steps" and args.value == "12"
    # sessions list 子命令
    args = parser.parse_args(["sessions", "list"])
    assert args.sess_command == "list"


def test_agent_uses_config_iterations(tmp_path):
    from cagent.core import Agent
    from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
    from cagent.tools import ToolRegistry

    path = tmp_path / "agent.yaml"
    path.write_text("model:\n  model: gpt-4o\nmax_steps: 3\nreact_max_iterations: 2\n", encoding="utf-8")
    cfg = ConfigProvider(str(path), watch=False)

    class FakeLLM(LLMClient):
        def __init__(self):
            super().__init__(LLMConfig())

        def complete(self, messages):
            return LLMResponse(content="{}")

        def complete_with_tools(self, messages, tools):
            return LLMResponse(content="x")

    agent = Agent(tools=ToolRegistry(), config=cfg, llm=FakeLLM())
    assert agent._effective_max_steps() == 3
    assert agent._effective_react_max_iterations() == 2
