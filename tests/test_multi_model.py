"""多模型配置与运行时路由测试。"""
import json

import pytest

from cagent.config import ConfigProvider
from cagent.config.schema import ModelConfig, ModelsFile, ReasoningConfig
from cagent.llm.base import LLMConfig
from cagent.llm.reasoning import build_reasoning_extra_body, effective_temperature
from cagent.llm.registry import ModelRouter
from cagent.schema.message import ImageRef, Message, MessageRole
from cagent.llm.openai import OpenAIClient


def _write_models(tmp_path, default, models):
    """在 tmp_path 写出 models.json 并返回 (agent.yaml 路径, models.json 路径)。"""
    agent = tmp_path / "agent.yaml"
    agent.write_text("prompts:\n  react_system: 'x'\n")
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps({"default": default, "models": models}))
    return str(agent), str(models_path)


# ── schema：UI camelCase → 内部 snake_case 映射 ──

def test_from_ui_dict_mapping():
    mc = ModelConfig.from_ui_dict(
        {
            "id": "glm",
            "name": "GLM",
            "vendor": "Custom",
            "url": "https://x/v1",
            "apiKey": "${K}",
            "supportsToolCall": True,
            "supportsImages": True,
            "supportsReasoning": True,
            "reasoning": {"effort": "high", "budgetTokens": 8000},
            "maxInputTokens": 100,
            "maxOutputTokens": 200,
        }
    )
    assert mc.model == "glm"
    assert mc.name == "GLM"
    assert mc.vendor == "Custom"
    assert mc.base_url == "https://x/v1"
    assert mc.api_key == "${K}"
    assert mc.tool_calling is True
    assert mc.vision is True
    assert mc.reasoning.enabled is True
    assert mc.reasoning.effort == "high"
    assert mc.reasoning.budget_tokens == 8000
    assert mc.max_input_tokens == 100
    assert mc.max_output_tokens == 200


def test_from_ui_dict_missing_id():
    with pytest.raises(ValueError):
        ModelConfig.from_ui_dict({"name": "x"})


def test_use_custom_protocol_ignored():
    # useCustomProtocol 字段被忽略，不影响解析
    mc = ModelConfig.from_ui_dict({"id": "x", "useCustomProtocol": True})
    assert mc.model == "x"


# ── ModelsFile.resolve_default ──

def test_models_file_resolve_default():
    mf = ModelsFile(default="a", models=[ModelConfig(model="a"), ModelConfig(model="b")])
    assert mf.resolve_default() == "a"
    # default 缺失 → 取首项
    mf2 = ModelsFile(models=[ModelConfig(model="x"), ModelConfig(model="y")])
    assert mf2.resolve_default() == "x"
    # 空 → 报错
    with pytest.raises(ValueError):
        ModelsFile().resolve_default()


# ── ConfigProvider 加载 / 取模型 ──

def test_configprovider_load_models(tmp_path):
    agent, _ = _write_models(
        tmp_path,
        "m1",
        [
            {
                "id": "m1",
                "name": "M1",
                "vendor": "OpenAI",
                "url": "https://api.openai.com/v1",
                "apiKey": "k",
                "supportsToolCall": True,
                "supportsImages": False,
                "supportsReasoning": True,
                "reasoning": {"effort": "low"},
                "maxOutputTokens": 4096,
            },
            {
                "id": "m2",
                "vendor": "Qwen",
                "url": "https://q/v1",
                "apiKey": "k2",
                "supportsToolCall": False,
                "supportsImages": True,
                "supportsReasoning": False,
            },
        ],
    )
    p = ConfigProvider(agent, watch=False)
    assert p.get_model_names() == ["m1", "m2"]
    assert p.get_default_model() == "m1"
    lc = p.get_model_config("m2")
    assert lc.model == "m2"
    assert lc.vision is True
    assert lc.tool_calling is False


# ── ModelRouter：路由 / 缓存 / 校验 ──

def test_router_routing_and_cache(tmp_path):
    agent, _ = _write_models(
        tmp_path,
        "a",
        [
            {"id": "a", "supportsToolCall": True},
            {"id": "b", "supportsToolCall": False},
        ],
    )
    p = ConfigProvider(agent, watch=False)
    router = ModelRouter.from_config(p, lambda lc: ("client", lc.model))

    assert router.get("a")[1] == "a"
    assert router.get("b")[1] == "b"
    # 默认
    assert router.get()[1] == "a"
    # 懒缓存：同一 name 返回同一对象
    assert router.get("a") is router.get("a")
    # 校验：无 tool_calling 的模型不能用于 ReAct
    with pytest.raises(ValueError):
        router.validate_react_target("b")
    # 未知模型
    with pytest.raises(ValueError):
        router.get("zzz")


# ── ReasoningAdapter ──

def test_reasoning_adapter():
    assert build_reasoning_extra_body(
        LLMConfig(model="o", reasoning_enabled=True, reasoning_effort="high", vendor="openai")
    ) == {"reasoning_effort": "high"}
    assert build_reasoning_extra_body(
        LLMConfig(model="q", reasoning_enabled=True, vendor="Qwen")
    ) == {"enable_thinking": True}
    assert build_reasoning_extra_body(LLMConfig(model="x")) == {}
    assert effective_temperature(LLMConfig(reasoning_enabled=True, temperature=0.9)) == 0.0
    assert effective_temperature(LLMConfig(temperature=0.9)) == 0.9


# ── vision 序列化 ──

def test_vision_serialization():
    cfg = LLMConfig(model="v", base_url="http://x", api_key="k", vision=True)
    client = OpenAIClient(cfg)
    msgs = [
        Message(
            role=MessageRole.USER,
            content="图",
            images=[ImageRef(url="http://i/1.png", detail="low")],
        )
    ]
    out = client._to_openai_messages(msgs)
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0] == {"type": "text", "text": "图"}
    assert out[0]["content"][1]["type"] == "image_url"


def test_vision_dropped_when_unsupported(caplog):
    cfg = LLMConfig(model="n", base_url="http://x", api_key="k", vision=False)
    client = OpenAIClient(cfg)
    msgs = [
        Message(role=MessageRole.USER, content="图", images=[ImageRef(url="http://i/1.png")])
    ]
    out = client._to_openai_messages(msgs)
    assert out[0]["content"] == "图"
