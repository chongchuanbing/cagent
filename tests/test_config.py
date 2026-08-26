"""验证配置系统：加载、环境变量展开、提示词覆盖与保存后热加载。"""
import os
import tempfile
import time

from cagent.config import ConfigProvider


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_load_model_and_env_expansion(tmp_path):
    os.environ["MY_KEY"] = "secret-123"
    path = tmp_path / "agent.yaml"
    _write(
        path,
        "model:\n"
        "  model: gpt-4o\n"
        "  api_key: ${MY_KEY}\n"
        "  temperature: 0.2\n",
    )
    p = ConfigProvider(str(path), watch=False)
    mc = p.get_model_config()
    assert mc.model == "gpt-4o"
    assert mc.api_key == "secret-123"
    assert mc.temperature == 0.2


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
    path = tmp_path / "agent.yaml"
    _write(path, "model:\n  model: gpt-4o\n")
    p = ConfigProvider(str(path), watch=False)
    assert p.get_model_config().model == "gpt-4o"

    # 修改配置并把 mtime 显式推后，触发重载
    _write(path, "model:\n  model: gpt-4o-mini\n")
    future = time.time() + 10
    os.utime(path, (future, future))
    p._maybe_reload()
    assert p.get_model_config().model == "gpt-4o-mini"
