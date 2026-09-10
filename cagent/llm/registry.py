"""模型路由：持有多个 LLMClient，按名字懒构建 + 缓存。"""
from typing import Callable, List, Optional

from .base import LLMClient, LLMConfig
from ..config.schema import ModelsFile


class ModelRouter:
    """按名字路由到已配置的模型 client（懒构建 + 缓存）。

    - 所有 client 经 `llm_factory` 构造（默认 OpenAIClient）；
    - `get(name=None)`：name 为空取默认模型；
    - `validate_react_target(name)`：启动期校验 ReAct 目标必须支持工具调用。
    """

    def __init__(
        self,
        models_file: ModelsFile,
        llm_factory: Callable[[LLMConfig], LLMClient],
        data_dir: str = ".data",
    ):
        self._mf = models_file
        self._factory = llm_factory
        self._data_dir = data_dir
        self._cache: dict[str, LLMClient] = {}

    @classmethod
    def from_config(cls, config, llm_factory, data_dir: Optional[str] = None) -> "ModelRouter":
        """从 ConfigProvider 构造（读取 models.json）。"""
        return cls(config.get_models_file(), llm_factory, data_dir or config.data_dir)

    @property
    def default(self) -> str:
        return self._mf.resolve_default()

    @property
    def names(self) -> List[str]:
        return [m.model for m in self._mf.models]

    def get(self, name: Optional[str] = None) -> LLMClient:
        name = name or self.default
        if name not in self.names:
            raise ValueError(f"未知模型 {name!r}，可用：{self.names}")
        if name not in self._cache:
            mc = next(m for m in self._mf.models if m.model == name)
            cfg = LLMConfig(
                model=mc.model,
                api_key=mc.api_key,
                base_url=mc.base_url,
                temperature=mc.temperature,
                max_tokens=mc.max_tokens,
                data_dir=self._data_dir,
                max_input_tokens=mc.max_input_tokens,
                tool_calling=mc.tool_calling,
                vision=mc.vision,
                reasoning_enabled=mc.reasoning.enabled,
                reasoning_effort=mc.reasoning.effort,
                reasoning_budget_tokens=mc.reasoning.budget_tokens,
                reasoning_extra_body=mc.reasoning.extra_body,
                vendor=mc.vendor,
            )
            self._cache[name] = self._factory(cfg)
        return self._cache[name]

    def validate_react_target(self, name: Optional[str] = None) -> None:
        """启动期校验：ReAct 角色目标必须 tool_calling=true。"""
        name = name or self.default
        if name not in self.names:
            raise ValueError(f"未知模型 {name!r}，可用：{self.names}")
        mc = next(m for m in self._mf.models if m.model == name)
        if not mc.tool_calling:
            raise ValueError(
                f"模型 {mc.model!r} 未开启 tool_calling（supportsToolCall=false），"
                f"无法用于 ReAct 执行（需要工具调用能力）"
            )
