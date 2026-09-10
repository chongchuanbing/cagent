"""验证配置系统：加载、环境变量展开、提示词覆盖与保存后热加载。"""
import json
import os
import tempfile
import time

from cagent.config import ConfigProvider


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_load_model_and_env_expansion(tmp_path):
    os.environ["MY_KEY"] = "secret-123"
    agent = tmp_path / "agent.yaml"
    _write(agent, "prompts:\n  react_system: 'STEP={step}'\n")
    models = tmp_path / "models.json"
    _write(
        models,
        json.dumps(
            {
                "default": "m1",
                "models": [
                    {
                        "id": "m1",
                        "name": "M1",
                        "vendor": "OpenAI",
                        "url": "https://api.openai.com/v1",
                        "apiKey": "${MY_KEY}",
                        "supportsToolCall": True,
                        "supportsImages": False,
                        "supportsReasoning": False,
                        "maxOutputTokens": 2048,
                    }
                ],
            }
        ),
    )
    p = ConfigProvider(str(agent), watch=False)
    mc = p.get_model_config()
    assert mc.model == "m1"
    assert mc.api_key == "secret-123"          # ${MY_KEY} 在 models.json 路径展开
    assert mc.max_tokens == 2048
    assert p.get_default_model() == "m1"
    assert p.get_model_names() == ["m1"]


def test_prompt_override_and_placeholders(tmp_path):
    path = tmp_path / "agent.yaml"
    _write(
        path,
        "prompts:\n"
        "  react_system: 'STEP={step} TOOLS={tools}'\n",
    )
    p = ConfigProvider(str(path), watch=False)
    out = p.get_prompt("react_system", step="计算", tools="calc")
    assert out == "STEP=计算 TOOLS=calc"
    # 未配置的 key 回退内置默认
    assert "规划器" in p.get_prompt("planner_system")


def test_hot_reload_after_save(tmp_path):
    agent = tmp_path / "agent.yaml"
    _write(agent, "prompts:\n  react_system: 'x'\n")
    models = tmp_path / "models.json"
    _write(models, json.dumps({"default": "a", "models": [{"id": "a"}]}))
    p = ConfigProvider(str(agent), watch=False)
    assert p.get_default_model() == "a"

    # 修改 models.json 并把 mtime 显式推后，触发重载
    _write(models, json.dumps({"default": "b", "models": [{"id": "b"}]}))
    future = time.time() + 10
    os.utime(models, (future, future))
    p._maybe_reload()
    assert p.get_default_model() == "b"
