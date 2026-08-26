"""长期记忆（分代晋升）回归测试：晋升阈值 / 同会话去重 / 冲突覆盖 / TTL /
标签校验 / remember 快速晋升 / 端到端召回注入。"""
import json
import tempfile

from cagent.llm.base import LLMClient, LLMConfig, LLMResponse
from cagent.memory import (
    LongTermMemory,
    MemoryRecord,
    MemoryService,
    MemoryStatus,
    TenuringManager,
)
from cagent.storage import get_storage


def _mem_cfg(tmpdir=None, **memory_kwargs) -> str:
    """写临时 agent.yaml（memory 段可覆盖），返回路径。"""
    import os

    import yaml

    tmpdir = tmpdir or tempfile.mkdtemp()
    path = os.path.join(tmpdir, "agent.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"memory": memory_kwargs}, f)
    return path


def _provider(**memory_kwargs):
    from cagent.config.provider import ConfigProvider

    return ConfigProvider(path=_mem_cfg(**memory_kwargs), watch=False)


def _record(content: str, **kwargs) -> MemoryRecord:
    return MemoryRecord(content=content, **kwargs)


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


# ---------- TenuringManager：分代流转 ----------

def test_promotion_after_threshold_sessions():
    """同一记忆跨 N 个不同会话出现后晋升（N=tenuring_threshold）。"""
    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=3))

    content = "用户偏好简洁的中文回答"
    r1 = tm.absorb([_record(content)], session_id="s1")
    assert not r1 and store.list_all(MemoryStatus.CANDIDATE)[0].age == 1

    r2 = tm.absorb([_record(content)], session_id="s2")
    assert not r2  # age=2 未达阈值
    assert store.list_all(MemoryStatus.CANDIDATE)[0].age == 2

    r3 = tm.absorb([_record(content)], session_id="s3")
    assert len(r3) == 1  # 第三次出现 → 晋升
    assert not store.list_all(MemoryStatus.CANDIDATE)
    tenured = store.list_all(MemoryStatus.TENURED)
    assert tenured[0].content == content and tenured[0].age == 3


def test_same_session_repeated_no_age_increment():
    """同一会话内重复提取不累计 age（防止单次长会话自我晋升）。"""
    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=2))

    content = "北京是中国的首都"
    tm.absorb([_record(content)], session_id="s1")
    tm.absorb([_record(content)], session_id="s1")  # 同会话再次出现
    cand = store.list_all(MemoryStatus.CANDIDATE)
    assert cand[0].age == 1
    assert not store.list_all(MemoryStatus.TENURED)


def test_pinned_direct_promotion():
    """用户显式指定（pinned）直通老年代。"""
    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=5))

    promoted = tm.absorb(
        [_record("用户喜欢深色主题", pinned=True)], session_id="s1"
    )
    assert len(promoted) == 1
    assert store.list_all(MemoryStatus.TENURED)[0].pinned is True


def test_conflict_new_overwrites_old():
    """新记忆与老年代冲突：新内容覆盖旧内容，保留 id 与 age。"""
    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=2))

    tm.absorb([_record("用户使用 Python 3.9")], session_id="s1")
    tm.absorb([_record("用户使用 Python 3.9")], session_id="s2")  # 晋升

    old = store.list_all(MemoryStatus.TENURED)[0]
    old_id = old.id

    # 新事实：同主题不同内容（相似度高于阈值）
    tm.absorb([_record("用户使用 Python 3.13")], session_id="s3")
    tenured = store.list_all(MemoryStatus.TENURED)
    assert len(tenured) == 1
    assert tenured[0].id == old_id            # 保留身份
    assert "3.13" in tenured[0].content       # 内容已更新


def test_candidate_ttl_eviction():
    """候选超过 TTL 未复发则淘汰。"""
    from datetime import datetime, timedelta

    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=3, candidate_ttl_days=7))

    tm.absorb([_record("一条陈旧的候选记忆")], session_id="s1")
    # 把候选的 updated_at 改到 10 天前
    cand = store.list_all(MemoryStatus.CANDIDATE)[0]
    cand.updated_at = datetime.now() - timedelta(days=10)
    store.upsert(cand)

    # 触发一次新的 absorb（先做新生代清理）
    tm.absorb([_record("完全无关的另一条记忆")], session_id="s2")
    contents = [r.content for r in store.list_all()]
    assert "一条陈旧的候选记忆" not in contents


def test_memory_promoted_event_emitted():
    """晋升时发布 MEMORY_PROMOTED 事件。"""
    from cagent.events import EventEmitter, EventType

    emitter = EventEmitter()
    events = []
    emitter.subscribe(events.append)

    store = LongTermMemory(get_storage(tempfile.mkdtemp()))
    tm = TenuringManager(store, config=_provider(tenuring_threshold=1), emitter=emitter)
    tm.absorb([_record("会被立刻晋升的记忆")], session_id="s1")

    promoted_events = [e for e in events if e.type == EventType.MEMORY_PROMOTED]
    assert promoted_events and "会被立刻晋升的记忆" in promoted_events[0].payload["content"]


# ---------- Extractor：提取与标签校验 ----------

def test_extractor_parses_and_validates_explicit_tags():
    """explicit 模式：枚举外的标签取值被过滤。"""
    from cagent.memory import MemoryExtractor

    llm = FakeLLM([
        LLMResponse(content=json.dumps([
            {"content": "用户偏好简洁回答", "tags": {"domain": "work", "sensitivity": "secret", "extra": "x"}, "pinned": False},
        ]))
    ])
    extractor = MemoryExtractor(llm=llm, config=_provider(
        tags={"mode": "explicit", "categories": {
            "domain": {"values": ["work", "life"]},
            "sensitivity": {"values": ["public", "private"]},
        }}
    ))
    records = extractor.extract("测试目标", [], "最终答案")
    assert len(records) == 1
    tags = records[0].tags
    assert tags.get("domain") == "work"        # 合法取值保留
    assert "sensitivity" not in tags            # 枚举外取值被丢弃
    assert "extra" not in tags                  # explicit 模式不允许自由标签


def test_extractor_implicit_tags_kept():
    """implicit 模式：自由生成的标签原样保留。"""
    from cagent.memory import MemoryExtractor

    llm = FakeLLM([
        LLMResponse(content="```json\n[{\"content\": \"用户在上海工作\", \"tags\": {\"城市\": \"上海\"}, \"pinned\": true}]\n```")
    ])
    extractor = MemoryExtractor(llm=llm, config=_provider(tags={"mode": "implicit"}))
    records = extractor.extract("目标", [], "答案")
    assert records[0].tags == {"城市": "上海"}
    assert records[0].pinned is True


def test_extractor_without_llm_returns_empty():
    from cagent.memory import MemoryExtractor

    extractor = MemoryExtractor(llm=None, config=_provider())
    assert extractor.extract("目标", [], "答案") == []


# ---------- MemoryService / remember 工具 ----------

def test_remember_tool_direct_promotion():
    """remember 工具：显式指定直通老年代并写入存储。"""
    from cagent.tools.builtin import RememberTool

    storage = get_storage(tempfile.mkdtemp())
    svc = MemoryService(storage, config=_provider())
    tool = RememberTool(svc)

    result = tool.run(content="记住我喜欢简洁的 Markdown 格式回答", tags={"主题": "偏好"})
    assert result.ok
    tenured = svc.store.list_all(MemoryStatus.TENURED)
    assert tenured and tenured[0].pinned and tenured[0].tags["主题"] == "偏好"


def test_service_recall_only_tenured_by_default():
    """召回默认只查老年代（候选不注入上下文）。"""
    storage = get_storage(tempfile.mkdtemp())
    svc = MemoryService(storage, config=_provider())

    svc.tenuring.absorb([_record("Python 是动态类型语言")], session_id="s1")
    assert svc.recall("Python 语言特性") == []  # 还是候选

    svc.tenuring.pin("Python 是动态类型语言")
    hits = svc.recall("Python 语言特性")
    assert hits and "Python" in hits[0].content


# ---------- Agent 端到端：跨会话晋升 + 召回注入 ----------

def test_agent_memory_lifecycle_across_sessions():
    """三个会话：提取→候选→晋升→召回注入 step 上下文。"""
    from cagent.config.provider import ConfigProvider
    from cagent.core import Agent
    from cagent.tools import ToolRegistry

    plan_resp = LLMResponse(content=json.dumps(
        {"steps": [{"id": "s1", "description": "执行任务", "depends_on": []}]}
    ))
    extraction = LLMResponse(content=json.dumps(
        [{"content": "用户偏好简洁的中文回答", "tags": {"主题": "偏好"}, "pinned": False}]
    ))

    storage = get_storage(tempfile.mkdtemp())
    cfg = ConfigProvider(
        path=_mem_cfg(tenuring_threshold=2, candidate_ttl_days=0), watch=False
    )

    def make_agent(scripted):
        llm = FakeLLM(scripted)
        reg = ToolRegistry()
        return Agent(llm=llm, tools=reg, storage=storage, config=cfg)

    # 会话 1：提取 → 候选
    a1 = make_agent([plan_resp, LLMResponse(content="结论"), LLMResponse(content="总结1"), extraction])
    a1.run("生成回答", session_id="sess-1")
    assert len(a1.memory.store.list_all(MemoryStatus.CANDIDATE)) == 1

    # 会话 2：同一记忆再现 → 晋升
    a2 = make_agent([plan_resp, LLMResponse(content="结论"), LLMResponse(content="总结2"), extraction])
    a2.run("生成回答", session_id="sess-2")
    tenured = a2.memory.store.list_all(MemoryStatus.TENURED)
    assert tenured and tenured[0].content == "用户偏好简洁的中文回答"

    # 会话 3：召回注入 step 上下文
    llm3 = FakeLLM([plan_resp, LLMResponse(content="结论"), LLMResponse(content="总结3"),
                    LLMResponse(content="[]")])
    a3 = Agent(llm=llm3, tools=ToolRegistry(), storage=storage, config=cfg)
    a3.run("生成回答", session_id="sess-3")
    react_calls = [c for c in llm3.calls if c[0] == "complete_with_tools"]
    user_prompt = next(m.content for m in react_calls[0][1] if m.role.value == "user")
    assert "长期记忆" in user_prompt
    assert "用户偏好简洁的中文回答" in user_prompt


# ---------- CLI 管理命令 ----------

def _cli_ns(path, **kwargs):
    import argparse

    ns = argparse.Namespace()
    ns.config = str(path)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_cli_memory_management_commands():
    import os

    import clients.cli.main as cli

    tmpdir = tempfile.mkdtemp()
    cfg_path = _mem_cfg(tmpdir)
    data_dir = os.path.join(tmpdir, ".data")
    store = LongTermMemory(get_storage(data_dir))
    tm = TenuringManager(store, config=_provider())
    tm.absorb([_record("用户使用 macOS")], session_id="s1")
    cand = store.list_all(MemoryStatus.CANDIDATE)[0]

    # list
    assert cli.cmd_memory_list(_cli_ns(cfg_path, status=None, data_dir=data_dir)) == 0
    # promote
    assert cli.cmd_memory_promote(_cli_ns(cfg_path, id=cand.id, data_dir=data_dir)) == 0
    assert store.get(cand.id).status == MemoryStatus.TENURED
    # tag
    assert cli.cmd_memory_tag(_cli_ns(cfg_path, id=cand.id, data_dir=data_dir, tags=["domain=work"])) == 0
    assert store.get(cand.id).tags["domain"] == "work"
    # forget
    assert cli.cmd_memory_forget(_cli_ns(cfg_path, id=cand.id, data_dir=data_dir)) == 0
    assert store.get(cand.id) is None
