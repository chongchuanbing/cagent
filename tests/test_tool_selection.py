"""工具分组 / 动态选择 / 目录化 prompt 的回归测试。"""
import json
import os
import tempfile

import pytest

from cagent.config.provider import ConfigProvider
from cagent.llm.base import LLMClient, LLMConfig
from cagent.prompts.react_prompt import (
    TOOL_DESC_MAX_CHARS,
    build_react_system_prompt,
    format_tool_catalog,
)
from cagent.schema.plan import Step, StepStatus
from cagent.tools.base import Tool, ToolResult
from cagent.tools.registry import ToolRegistry


class _Dummy(Tool):
    def __init__(self, name, description, group="default"):
        self.name = name
        self.description = description
        self.group = group

    def run(self, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content=f"{self.name} called")


class _ScriptedLLM(LLMClient):
    """按脚本返回带/不带 tool_calls 的响应。"""

    def __init__(self, scripted):
        super().__init__(LLMConfig())
        self.scripted = list(scripted)
        self.calls = []

    def complete(self, messages):
        raise AssertionError("unexpected complete()")

    def complete_with_tools(self, messages, tools):
        self.calls.append((messages, tools))
        nxt = self.scripted.pop(0)
        return nxt() if callable(nxt) else nxt


def _resp(content="", tool_calls=None):
    from cagent.llm.base import LLMResponse

    return LLMResponse(content=content, tool_calls=tool_calls or [])


def _tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_Dummy("calculator", "数学计算器，做加减乘除运算", group="math"))
    reg.register(_Dummy("ask_user", "向用户收集补充信息", group="interaction"))
    reg.register(_Dummy("remember", "把用户指定的事实写入长期记忆", group="memory"))
    return reg


# ---------- 目录渲染 ----------

def test_tool_catalog_with_group_prefix_and_truncation():
    long_desc = "描述" * 200
    reg = _tools()
    reg.register(_Dummy("big", long_desc))
    catalog = format_tool_catalog(reg.list())
    assert "[math] calculator: 数学计算器" in catalog
    assert "[interaction] ask_user: " in catalog
    assert "\n- big: " in catalog  # default 组不带前缀
    assert "big" in catalog and "…" in catalog
    # 截断后不超过 max + 省略号
    line = next(l for l in catalog.splitlines() if "big" in l)
    assert len(line) <= len("- [default] big: ") + TOOL_DESC_MAX_CHARS + 10


def test_build_react_system_prompt_uses_catalog():
    step = Step(id="s1", description="算一下 1+2", depends_on=[], status=StepStatus.PENDING)
    reg = _tools()
    prompt = build_react_system_prompt(step, reg.list())
    assert "算一下 1+2" in prompt
    assert "[math] calculator" in prompt
    assert "parameters" not in prompt  # 完整 schema 不进 system prompt


# ---------- 分组过滤 ----------

def test_enabled_groups_filter_keeps_default():
    reg = _tools()
    reg.register(_Dummy("misc", "未分组的通用工具"))  # default 组
    names = [t.name for t in reg.list(groups=["math"])]
    assert set(names) == {"calculator", "misc"}  # default 组始终启用


def test_registry_register_group_override():
    reg = ToolRegistry()
    t = _Dummy("x", "desc", group="a")
    reg.register(t, group="b")
    assert reg.groups() == {"b": ["x"]}


# ---------- 动态 Top-K 选择 ----------

def test_select_topk_by_keyword_overlap():
    reg = _tools()
    picked = reg.select("需要做数学计算，求 3 乘 4", k=1)
    assert [t.name for t in picked] == ["calculator"]


def test_select_fallback_to_all_when_no_match():
    reg = _tools()
    picked = reg.select("zzz qqq", k=1)
    assert len(picked) == 3  # 无命中回退全量


def test_select_noop_when_pool_small():
    reg = _tools()
    picked = reg.select("数学计算", k=10)
    assert len(picked) == 3  # 池子小于 k 时直接全量


# ---------- ReActEngine 集成 ----------

def _cfg(content: str) -> ConfigProvider:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return ConfigProvider(path, watch=False)


def test_react_dynamic_selection_limits_tools():
    """dynamic 开启时，本 step 的 tools 参数只含 Top-K 命中工具。"""
    from cagent.core.react import ReActEngine

    cfg = _cfg("tools:\n  dynamic: true\n  max_per_step: 1\n")
    llm = _ScriptedLLM([_resp(content="结论")])
    engine = ReActEngine(llm, _tools(), config=cfg, max_iterations=1)
    step = Step(id="s1", description="做数学计算", depends_on=[], status=StepStatus.PENDING)
    result = engine.run(step)
    assert result.success
    _, tools = llm.calls[0]
    assert [t["function"]["name"] for t in tools] == ["calculator"]


def test_react_tool_outside_selection_unavailable():
    """被裁剪/未入选的工具即使被模型幻觉调用，也按不可用处理。"""
    from cagent.core.react import ReActEngine

    cfg = _cfg("tools:\n  enabled_groups: [math]\n")
    llm = _ScriptedLLM([
        _resp(tool_calls=[{"id": "1", "function": {"name": "ask_user", "arguments": json.dumps({"question": "?"})}}]),
        _resp(content="结论"),
    ])
    engine = ReActEngine(llm, _tools(), config=cfg, max_iterations=2)
    step = Step(id="s1", description="执行", depends_on=[], status=StepStatus.PENDING)
    engine.run(step)
    unavailable = [e for e in engine.last_trace if "action" in e and "不可用" in e["observation"]]
    assert unavailable, "被裁剪的工具调用应返回不可用提示"


def test_react_default_config_all_tools():
    """默认配置（无 tools 段）行为不变：全量注入。"""
    from cagent.core.react import ReActEngine

    cfg = _cfg("")
    llm = _ScriptedLLM([_resp(content="ok")])
    engine = ReActEngine(llm, _tools(), config=cfg, max_iterations=1)
    step = Step(id="s1", description="任意", depends_on=[], status=StepStatus.PENDING)
    engine.run(step)
    _, tools = llm.calls[0]
    assert len(tools) == 3


# ---------- 配置 ----------

def test_tools_config_none_normalization_and_fields():
    cfg = _cfg("tools:\n# 仅注释\n")
    assert cfg.get_config().tools.dynamic is False
    assert cfg.get_config().tools.enabled_groups is None
    assert cfg.get_config().tools.max_per_step == 8
