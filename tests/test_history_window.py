"""history 窗口的 token 预算裁剪：maxInputTokens 透传 + AgentLoop._apply_window。"""
import json
import types

from cagent.config import ConfigProvider
from cagent.core.loop import AgentLoop, estimate_tokens
from cagent.llm.registry import ModelRouter


def _fake_llm(max_input_tokens):
    """构造一个仅带 config.max_input_tokens 的伪 LLMClient。"""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(max_input_tokens=max_input_tokens)
    )


def _make_history(n, size=200):
    return [
        {
            "step_id": f"s{i}",
            "description": "step desc",
            "success": True,
            "output": "X" * size,
        }
        for i in range(n)
    ]


# ── token 估算器 ──

def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0          # 纯英文 ~4 字符/token
    assert estimate_tokens("你好世界") >= 4             # 中文 ~1 token/字


# ── _apply_window：token 预算裁剪 ──

def test_apply_window_token_budget(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text(
        "history_window: 50\nhistory_summarize: false\nhistory_token_budget_ratio: 0.5\n"
    )
    cfg = ConfigProvider(str(agent), watch=False)
    loop = AgentLoop(planner=object(), executor=object(), llm=_fake_llm(100), config=cfg)
    # 每条约 57 token；budget = 100 * 0.5 = 50，单条已超预算，但至少保留 1 条
    out = loop._apply_window(_make_history(5, size=200))
    assert len(out) == 1
    assert out[0]["step_id"] == "s4"                   # 保留最新一条


def test_apply_window_no_budget_keeps_all(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text("history_window: 50\nhistory_summarize: false\n")
    cfg = ConfigProvider(str(agent), watch=False)
    loop = AgentLoop(planner=object(), executor=object(), llm=_fake_llm(None), config=cfg)
    out = loop._apply_window(_make_history(5, size=200))
    # 无 token 预算 + 计数窗口大 → 全部保留
    assert len(out) == 5


def test_apply_window_count_and_token_coexist(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text(
        "history_window: 2\nhistory_summarize: false\nhistory_token_budget_ratio: 1.0\n"
    )
    cfg = ConfigProvider(str(agent), watch=False)
    loop = AgentLoop(planner=object(), executor=object(), llm=_fake_llm(1000), config=cfg)
    # 计数窗口限 2；token 预算 1000 足够 → 取更保守的 2 条
    out = loop._apply_window(_make_history(5, size=200))
    assert len(out) == 2
    assert [h["step_id"] for h in out] == ["s3", "s4"]


def test_apply_window_disabled_ratio_skips_budget(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text(
        "history_window: 50\nhistory_summarize: false\nhistory_token_budget_ratio: 0\n"
    )
    cfg = ConfigProvider(str(agent), watch=False)
    loop = AgentLoop(planner=object(), executor=object(), llm=_fake_llm(100), config=cfg)
    # ratio=0 关闭 token 预算 → 不裁剪
    out = loop._apply_window(_make_history(5, size=200))
    assert len(out) == 5


# ── max_input_tokens 透传 ──

def test_provider_passes_max_input_tokens(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text("prompts:\n  react_system: 'x'\n")
    models = tmp_path / "models.json"
    models.write_text(json.dumps({
        "default": "m1",
        "models": [{"id": "m1", "maxInputTokens": 32768}],
    }))
    p = ConfigProvider(str(agent), watch=False)
    assert p.get_model_config("m1").max_input_tokens == 32768


def test_router_passes_max_input_tokens(tmp_path):
    agent = tmp_path / "agent.yaml"
    agent.write_text("prompts:\n  react_system: 'x'\n")
    models = tmp_path / "models.json"
    models.write_text(json.dumps({
        "default": "m1",
        "models": [{"id": "m1", "maxInputTokens": 8192}],
    }))
    p = ConfigProvider(str(agent), watch=False)
    captured = {}

    def factory(lc):
        captured["cfg"] = lc
        return "client"

    ModelRouter.from_config(p, factory).get("m1")
    assert captured["cfg"].max_input_tokens == 8192
