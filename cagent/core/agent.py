"""Agent：对外唯一入口，依赖注入组装所有组件。"""
import os
import uuid
from typing import Callable, Optional

from ..llm.base import LLMClient, LLMConfig
from ..tools.registry import ToolRegistry
from ..storage import get_storage, SessionRecorder
from ..config.provider import ConfigProvider
from ..config.schema import PathSpaceConfig
from ..events import EventEmitter
from ..memory import MemoryService
from ..utils import sanitize_text
from ..utils.logging import get_logger, setup_session_logger, teardown_session_logger
from ..plugins.capability import CapabilityProbe
from .failure_ledger import FailureLedger
from .planner import Planner
from .react import ReActEngine
from .executor import Executor
from .loop import AgentLoop
from ..metrics import MetricsCollector, JsonlMetricsBackend

logger = get_logger("agent")


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
        capability: Optional[CapabilityProbe] = None,
        failure_ledger: Optional[FailureLedger] = None,
        path_space=None,
    ):
        self.tools = tools
        self._llm = llm
        self.config = config
        # path_space 必须先于 storage 就位：storage 注入它做统一越界校验
        self.path_space = path_space
        self.storage = storage or get_storage(
            self.config.data_dir if self.config else PathSpaceConfig().data_dir,
            path_space=self.path_space,
        )
        self._llm_factory = llm_factory
        self.emitter = emitter
        # 长期记忆：显式注入优先；否则按配置构建（memory.enabled 控制开关，
        # 每次调用实时读取，支持热加载）
        self.memory = memory if memory is not None else self._maybe_build_memory()

        # L3 环境探测：显式注入优先；否则自动构建（缓存到 .data/env.json）
        self.capability = capability if capability is not None else self._build_capability_probe()

        # L5 失败账本：显式注入优先；否则自动构建
        self.failure_ledger = failure_ledger if failure_ledger is not None else FailureLedger()

        # 度量采集：显式注入优先；否则自动构建
        self.metrics_collector = MetricsCollector()
        
        # 订阅 MetricsCollector 到 emitter
        if self.emitter is not None:
            self.emitter.subscribe(self.metrics_collector.handle_event)

        self._loop = loop
        self._planner = planner
        self._react_engine = react_engine
        self._executor = executor
        # None 表示交由 config 决定（保存即生效）；显式传入则覆盖
        self._max_steps = max_steps
        self._react_max_iterations = react_max_iterations

    def _build_capability_probe(self) -> CapabilityProbe:
        """L3: 构建环境探测探针，缓存到 <data_dir>/env.json。"""
        data_dir = self.config.data_dir if self.config is not None else PathSpaceConfig().data_dir
        cache_path = os.path.join(data_dir, "env.json")
        return CapabilityProbe(cache_path=cache_path)

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

    def _resolve_path_space(self):
        """PathSpace：显式注入优先；否则按 config.paths 构建（每次 run 一个冻结实例）。"""
        if self.path_space is not None:
            return self.path_space
        if self.config is not None:
            return self.config.build_path_space()
        return None

    def _prepare_session_scope(self, sid: str, space_dir: Optional[str] = None):
        """按会话派生 PathSpace 与路径作用域（会话路径隔离）。

        - scratch = <data>/sessions/<sid>/scratch，本会话唯一默认可写区
        - 空间根：run 参数 space_dir 优先，其次配置 paths.space_dir；
          均无则无空间模式（workspace:// 即 scratch）
        返回 (session_space, scope)；无法派生（无 PathSpace / storage 无根）时 (None, None)。
        """
        from pathlib import Path

        from ..runtime.session_scope import SessionScope
        from ..storage import SessionRecorder

        base_space = self._resolve_path_space()
        storage_root = getattr(self.storage, "root", None)
        if base_space is None or storage_root is None:
            return None, None

        scratch = Path(os.path.join(storage_root, "sessions", sid, "scratch"))
        scratch.mkdir(parents=True, exist_ok=True)

        raw_space = space_dir
        if raw_space is None and self.config is not None:
            try:
                raw_space = self.config.get_config().paths.space_dir
            except Exception:  # noqa: BLE001 —— 配置异常时退回无空间模式
                raw_space = None
        space_root = None
        if raw_space:
            p = Path(os.path.expanduser(str(raw_space)))
            if not p.is_absolute():
                p = base_space.base_dir / p
            space_root = Path(os.path.realpath(str(p)))

        session_space = base_space.for_session(scratch, space_root)

        recorder = SessionRecorder(self.storage, sid)

        def _record_write(rel_path: str, op: str) -> None:
            recorder.record_workspace_write(rel_path, op)
            if self.emitter is not None:
                from ..events.schema import EventType

                self.emitter.emit(
                    EventType.WORKSPACE_WRITE,
                    payload={"path": rel_path, "op": op},
                    session_id=sid,
                )

        scope = SessionScope(
            scratch=scratch,
            space_root=space_root,
            path_space=session_space,
            record_write=_record_write,
        )
        return session_space, scope

    def _build_loop(self, path_space=None) -> AgentLoop:
        if self._loop is not None:
            return self._loop
        llm = self._resolve_llm()
        react = self._react_engine or ReActEngine(
            llm, self.tools, config=self.config,
            max_iterations=self._effective_react_max_iterations(),
            emitter=self.emitter,
            capability=self.capability,
            failure_ledger=self.failure_ledger,
            path_space=path_space if path_space is not None else self._resolve_path_space(),
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
        space_dir: Optional[str] = None,
    ) -> str:
        """执行目标任务，返回最终答案；过程落盘到 sessions/<session_id>/。

        - resume=True 时，从该 session 的 trace.jsonl 重建历史上下文，
          让新 goal 带着上轮的结论与工具观察继续执行。
        - space_dir 覆盖配置 paths.space_dir（空间模式：workspace:// 挂该目录，
          结构化工具写入需显式 scheme 并登记审计；临时文件始终落会话目录）。
        """
        # 终端在非 UTF-8 环境下粘贴的内容会带 \udcXX 代理字符，
        # 不清洗的话落盘/JSON 序列化会报 surrogates not allowed
        goal = sanitize_text(goal)
        # L5: 每次 run 重置失败账本，避免跨 run 累积误判
        self.failure_ledger.reset()
        sid = session_id or uuid.uuid4().hex
        # 会话路径作用域：scratch 目录创建 + session:// / workspace:// 派生挂载
        session_space, scope = self._prepare_session_scope(sid, space_dir)
        loop = self._build_loop(path_space=session_space)
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

        # 执行任务
        if scope is not None:
            from contextlib import ExitStack

            from ..runtime.session_scope import enter_scope

            with ExitStack() as stack:
                stack.enter_context(enter_scope(scope))
                result = loop.run(
                    goal, recorder=recorder, session_id=sid,
                    memory=self.memory, prior_history=prior_history,
                )
        else:
            result = loop.run(
                goal, recorder=recorder, session_id=sid,
                memory=self.memory, prior_history=prior_history,
            )
        
        # 保存度量数据
        turn_metrics = self.metrics_collector.get_current_turn()
        if turn_metrics is not None:
            recorder.record_metrics(turn_metrics)
        
        return result
