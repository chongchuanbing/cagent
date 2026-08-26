"""LLM 抽象层。"""
from .base import LLMClient, LLMConfig, LLMResponse
from .openai import OpenAIClient

__all__ = ["LLMClient", "LLMConfig", "LLMResponse", "OpenAIClient"]
