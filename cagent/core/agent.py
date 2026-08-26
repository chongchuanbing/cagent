"""Agent：对外唯一入口，依赖注入组装所有组件。"""
import uuid
from typing import Callable, Optional

from ..llm.base import LLMClient, LLMConfig
from ..tools.registry import ToolRegistry
from ..storage import get_storage, SessionRecorder
from ..config.provider import ConfigProvider
from ..events import EventEmitter
from ..memory import MemoryService
from ..utils import sanitize_text
from ..utils.config import Config as RuntimeConfig
from .planner import Planner
from .react import ReActEngine
from .executor import Executor
from .loop import AgentLoop


def _default_llm_factory(llm_config: LLMConfig) -> LLMClient:
    """默认 LLM 工厂：按配置构造 OpenAI 兼容客户端。"""
    from ..llm.openai import OpenAIClient

    return OpenAIClient(llm_config)


class Agent:
    """用户调用入口。

    - 传入 `config` 时，每次 run 都按当前配置重建流水线：模型（api_key/model 等）
      与提示词「保存后立即生效」，无需重启。
    - 传入 `storage` 时，每次 run 自动创建 session 并落盘 meta/plan/trace。
    - 也可显式注入 planner/react/executor/loop 以完全自定义。
    """

    def __init__(
        self,
        tools: ToolRegistry,
        llm: Optional[LLMClient] = None,
        config: Optional[ConfigProvider] = None,
        storage=None,
        planner: Optional[Planner] = None,
        react_engine: Optional[ReActEngine] = None,
        executor: Optional[Executor] = None,
        loop: Optional[AgentLoop] = None,
        max_steps: Optional[int] = None,
        react_max_iterations: Optional[int] = None,
        llm_factory: Callable[[LLMConfig], LLMClient] = _default_llm_factory,
        emitter: Optional[EventEmitter] = None,
        memory: Optional[MemoryService] = None,
    ):
        self.tools = tools
        self._llm = llm
        self.config = config
        self.storage = storage or get_storage(self.config.data_dir if self.config else RuntimeConfig().data_dir)
        self._llm_factory = llm_factory
        self.emitter = emitter
        # 长期记忆：显式注入优先；否则按配置构建（memory.enabled 控制开关，
        # 每次调用实时读取，支持热加载）
        self.memory = memory if memory is not None else self._maybe_build_memory()

        self._loop = loop
        self._planner = planner
        self._react_engine = react_engine
        self._executor = executor
        # None 表示交由 config 决定（保存即生效）；显式传入则覆盖
        self._max_steps = max_steps
        self._react_max_iterations = react_max_iterations

    def _resolve_llm(self) -> LLMClient:
        if self._llm is not None:
            return self._llm
        if self.config is None:
            raise ValueError("必须提供 llm 或 config")
        return self._llm_factory(self.config.get_model_config())

    def _effective_max_steps(self) -> int:
        if self._max_steps is not None:
            return self._max_steps
        if self.config is not None:
            return self.config.get_max_steps()
        return 20

    def _effective_react_max_iterations(self) -> int:
        if self._react_max_iterations is not None:
            return self._react_max_iterations
        if self.config is not None:
            return self.config.get_react_max_iterations()
        return 5

    def _build_loop(self) -> AgentLoop:
        if self._loop is not None:
            return self._loop
        llm = self._resolve_llm()
        react = self._react_engine or ReActEngine(
            llm, self.tools, config=self.config,
            max_iterations=self._effective_react_max_iterations(),
            emitter=self.emitter,
        )
        planner = self._planner or Planner(llm, config=self.config)
        executor = self._executor or Executor(react)
        return AgentLoop(
            planner, executor,
            max_steps=self._effective_max_steps(),
            llm=llm, config=self.config, emitter=self.emitter,
        )

    def _maybe_build_memory(self) -> Optional[MemoryService]:
        """配置存在且 memory.enabled 时构建长期记忆服务；否则返回 None。"""
        if self.config is None:
            return None
        try:
            if not self.config.get_config().memory.enabled:
                return None
        except Exception:  # noqa: BLE001 —— 配置异常时不阻塞 Agent 构造
            return None
        return MemoryService(
            self.storage, config=self.config, emitter=self.emitter,
            llm_factory=self._resolve_llm,
        )

    def run(
        self,
        goal: str,
        session_id: Optional[str] = None,
        resume: bool = False,
    ) -> str:
        """执行目标任务，返回最终答案；过程落盘到 sessions/<session_id>/。

        - resume=True 时，从该 session 的 trace.jsonl 重建历史上下文，
          让新 goal 带着上轮的结论与工具观察继续执行。
        """
        # 终端在非 UTF-8 环境下粘贴的内容会带 \udcXX 代理字符，
        # 不清洗的话落盘/JSON 序列化会报 surrogates not allowed
        goal = sanitize_text(goal)
        loop = self._build_loop()
        sid = session_id or uuid.uuid4().hex
        if self.emitter is not None:
            self.emitter.default_session_id = sid
        # remember 工具若已注册但未绑定记忆服务，在此绑定（支持 CLI 构造后注册的场景）
        if self.memory is not None:
            from ..tools.builtin.remember import RememberTool

            rt = self.tools.get("remember")
            if isinstance(rt, RememberTool) and rt._memory is None:
                rt.set_memory(self.memory)
        recorder = SessionRecorder(self.storage, sid)

        # 恢复历史上下文
        prior_history = None
        if resume:
            prior_history = recorder.load_history()

        return loop.run(
            goal, recorder=recorder, session_id=sid,
            memory=self.memory, prior_history=prior_history,
        )
