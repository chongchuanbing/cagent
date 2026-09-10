"""配置提供器：文件加载 + 保存后立即生效（热加载）。

设计要点：
- 默认从 `config/agent.yaml` 读取端上配置；支持 yaml / json。
- 模型配置独立在 `config/models.json`（UI 落盘格式，cagent 只读），
  与 agent.yaml 同目录；UI 修改后下一次 run 自动生效。
- 守护线程每秒轮询 agent.yaml + models.json 的 mtime，任一变化即重新加载。
- 组件在每次调用时通过 get_model_config()/get_prompt() 取值，因此变更对
  下一次 run 立即生效，无需重启。
"""
import json
import os
import threading
import time
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .schema import AgentConfig, MemoryConfig, ModelConfig, ModelsFile, PathSpaceConfig
from ..llm.base import LLMConfig
from ..prompts import (
    HISTORY_SUMMARIZE_SYSTEM_PROMPT,
    MEMORY_EXTRACT_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    REACT_UNCONVERGED_PROMPT,
    REPLAN_SYSTEM_PROMPT,
    REACT_SYSTEM_TEMPLATE,
    SUMMARIZE_SYSTEM_PROMPT,
)


# 内置默认提示词，未被配置覆盖时使用
DEFAULT_PROMPTS: Dict[str, str] = {
    "planner_system": PLANNER_SYSTEM_PROMPT,
    "replan_system": REPLAN_SYSTEM_PROMPT,
    "react_system": REACT_SYSTEM_TEMPLATE,
    "react_unconverged": REACT_UNCONVERGED_PROMPT,
    "history_summarize_system": HISTORY_SUMMARIZE_SYSTEM_PROMPT,
    "memory_extract": MEMORY_EXTRACT_SYSTEM_PROMPT,
    "summarize_system": SUMMARIZE_SYSTEM_PROMPT,
}


class ConfigProvider:
    """读取并热加载 `agent.yaml` + `models.json`，对外提供模型与提示词。"""

    def __init__(
        self,
        path: str = "config/agent.yaml",
        watch: bool = True,
        data_dir: Optional[str] = None,
        models_path: Optional[str] = None,
    ):
        self.path = path
        # 模型配置文件（UI 落盘格式，cagent 只读），默认与 agent.yaml 同目录
        self._models_path = models_path or os.path.join(
            os.path.dirname(os.path.abspath(path)), "models.json"
        )
        # 显式传入的 data_dir 优先级最高（覆盖配置里的 paths.data_dir）
        self._data_dir_override = data_dir
        self._lock = threading.RLock()
        self._cfg = AgentConfig()
        self._cfg_models = ModelsFile()
        self._mtime = -1
        self._models_mtime = -1
        self._stop = False
        self._load()  # 内部按 paths.data_dir 解析出 self.data_dir
        if watch:
            self._thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._thread.start()

    # ---------- 加载 / 热加载 ----------
    @staticmethod
    def _expand_env(value):
        """递归展开字符串中的 ${ENV_VAR} 为环境变量值。"""
        if isinstance(value, str):
            import re

            return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), value)
        if isinstance(value, dict):
            return {k: ConfigProvider._expand_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ConfigProvider._expand_env(v) for v in value]
        return value

    def _load(self) -> None:
        with self._lock:
            # —— agent.yaml ——
            data: dict = {}
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    if yaml is not None and self.path.endswith((".yaml", ".yml")):
                        data = yaml.safe_load(f) or {}
                    else:
                        data = json.load(f)
            data = self._expand_env(data)
            # 允许 yaml 中 `prompts:` / `memory:` / `tools:` 仅注释而解析为 None 的情况
            if data.get("prompts") is None:
                data["prompts"] = {}
            if data.get("memory") is None:
                data["memory"] = {}
            if data.get("tools") is None:
                data["tools"] = {}
            self._cfg = AgentConfig(**data)
            # —— models.json（UI 落盘，cagent 只读）——
            self._cfg_models = self._load_models_file()
            # data_dir 唯一来源：显式覆盖 > 配置 paths.data_dir（热加载后即生效）
            self.data_dir = self._data_dir_override or self._cfg.paths.data_dir
            self._mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else -1
            self._models_mtime = (
                os.path.getmtime(self._models_path) if os.path.exists(self._models_path) else -1
            )

    def _load_models_file(self) -> "ModelsFile":
        """加载并解析 models.json（若缺失则返回空 ModelsFile）。"""
        if not os.path.exists(self._models_path):
            return ModelsFile()
        with open(self._models_path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        raw = self._expand_env(raw)
        models = [ModelConfig.from_ui_dict(m) for m in raw.get("models", [])]
        return ModelsFile(default=raw.get("default"), models=models)

    def _maybe_reload(self) -> None:
        try:
            a_mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else -1
        except OSError:
            a_mtime = -1
        try:
            m_mtime = (
                os.path.getmtime(self._models_path) if os.path.exists(self._models_path) else -1
            )
        except OSError:
            m_mtime = -1
        if a_mtime != self._mtime or m_mtime != self._models_mtime:
            self._load()

    def _watch_loop(self) -> None:
        while not self._stop:
            time.sleep(1)
            try:
                self._maybe_reload()
            except Exception:  # pragma: no cover
                pass

    # ---------- 取值接口 ----------
    def get_config(self) -> AgentConfig:
        """返回当前完整配置（调用前先核对 mtime，确保热加载生效）。"""
        self._maybe_reload()
        return self._cfg

    def get_max_steps(self) -> int:
        self._maybe_reload()
        return self._cfg.max_steps

    def get_react_max_iterations(self) -> int:
        self._maybe_reload()
        return self._cfg.react_max_iterations

    def get_memory_config(self) -> "MemoryConfig":
        self._maybe_reload()
        return self._cfg.memory

    def get_model_config(self, name: Optional[str] = None) -> LLMConfig:
        """返回模型 LLMConfig（默认模型，或指定 name）。

        每次调用都核对文件，确保热加载生效。
        """
        self._maybe_reload()
        default_id = self._cfg_models.resolve_default()
        return self._to_llm_config(name or default_id)

    def _to_llm_config(self, model_id: str) -> LLMConfig:
        mc = next((m for m in self._cfg_models.models if m.model == model_id), None)
        if mc is None:
            raise ValueError(f"模型 {model_id!r} 未配置，可用：{self.get_model_names()}")
        return LLMConfig(
            model=mc.model,
            api_key=mc.api_key,
            base_url=mc.base_url,
            temperature=mc.temperature,
            max_tokens=mc.max_tokens,
            data_dir=self.data_dir,
            max_input_tokens=mc.max_input_tokens,
            tool_calling=mc.tool_calling,
            vision=mc.vision,
            reasoning_enabled=mc.reasoning.enabled,
            reasoning_effort=mc.reasoning.effort,
            reasoning_budget_tokens=mc.reasoning.budget_tokens,
            reasoning_extra_body=mc.reasoning.extra_body,
            vendor=mc.vendor,
        )

    def get_models_file(self) -> "ModelsFile":
        """返回当前 models.json 解析结果（调用前核对 mtime，确保热加载生效）。"""
        self._maybe_reload()
        return self._cfg_models

    def get_model_names(self) -> List[str]:
        """返回所有已配置模型 id。"""
        self._maybe_reload()
        return [m.model for m in self._cfg_models.models]

    def get_default_model(self) -> str:
        """返回默认模型 id（调用前核对 mtime）。"""
        self._maybe_reload()
        return self._cfg_models.resolve_default()

    def build_path_space(
        self,
        base_dir: Optional[str] = None,
        plugins_dir: Optional[str] = None,
        config_dir: Optional[str] = None,
    ):
        """按 `paths` 配置构造 PathSpace —— 框架内唯一推荐的路径构造入口。

        base_dir / plugins_dir / config_dir 为端上显式覆盖（如 CLI 由 __file__ 推出
        插件目录），未传时取配置里的 paths.* 。
        """
        from pathlib import Path

        from ..runtime.paths import Mount, PathSpace

        self._maybe_reload()
        p: PathSpaceConfig = self._cfg.paths
        base = Path(base_dir or p.base_dir or os.getcwd())
        space = PathSpace.build_default(
            base,
            work_dir=p.work_dir,
            data_dir=self.data_dir,
            plugins_dir=plugins_dir or p.plugins_dir,
            config_dir=config_dir or p.config_dir,
            allow_paths=tuple(p.allow_paths),
            allow_symlink_targets=tuple(p.allow_symlink_targets),
        )
        # data:// 是否对模型可见由配置决定（默认隐藏，避免模型误把 .data 当可写工作区）
        dm = space.mounts.get("data")
        if dm is not None and dm.expose_to_llm != p.expose_data_to_llm:
            space = space.with_mount(
                Mount(
                    dm.name,
                    dm.physical,
                    expose_to_llm=p.expose_data_to_llm,
                    sandbox=dm.sandbox,
                    modes=dm.modes,
                )
            )
        # 注：skills://<name>/... 由 read_skill_file 经 SkillDefinition.resolve_file 承接，
        # 不在 PathSpace 注册挂载点，避免与「mount 名即 scheme」的解析规则冲突；
        # 路径速查卡里 skills:// 作为框架级逻辑约定直接说明。
        return space

    def get_prompt(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        """返回提示词模板；带 kwargs 时按占位符渲染。

        优先级：配置文件 prompts[key] > 调用方 default > 内置默认。
        """
        self._maybe_reload()
        tmpl = self._cfg.prompts.get(key)
        if tmpl is None:
            tmpl = default if default is not None else DEFAULT_PROMPTS.get(key, "")
        if kwargs:
            try:
                return tmpl.format(**kwargs)
            except (KeyError, IndexError):
                return tmpl
        return tmpl

    def stop(self) -> None:
        """停止后台监听线程（进程退出时可选调用）。"""
        self._stop = True
