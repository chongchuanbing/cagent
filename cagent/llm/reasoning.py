"""思考模式适配器：按 vendor 把 reasoning 配置翻译为厂商原生参数。"""
from typing import Dict

from .base import LLMConfig


def build_reasoning_extra_body(cfg: LLMConfig) -> Dict:
    """返回需透传给 API 的 reasoning 参数（走 OpenAI extra_body）。

    - 未开启 reasoning → 返回空 dict；
    - openai：reasoning_effort（+ 可选 max_completion_tokens）；
    - 其它（qwen / deepseek / glm / custom）：enable_thinking（可被 extra_body 覆盖）。
    """
    if not cfg.reasoning_enabled:
        return {}
    body: Dict = dict(cfg.reasoning_extra_body or {})
    vendor = (cfg.vendor or "openai").lower()
    if vendor == "openai":
        if cfg.reasoning_effort:
            body["reasoning_effort"] = cfg.reasoning_effort
        if cfg.reasoning_budget_tokens:
            body["max_completion_tokens"] = cfg.reasoning_budget_tokens
    else:
        body.setdefault("enable_thinking", True)
    return body


def effective_temperature(cfg: LLMConfig) -> float:
    """思考模型强制 temperature=0（o-series / Qwen-thinking 等多禁非零温度）。"""
    if cfg.reasoning_enabled:
        return 0.0
    return cfg.temperature
