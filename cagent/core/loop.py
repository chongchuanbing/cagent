"""AgentLoop：外层编排控制，串联 Plan-Executor 与 ReAct。"""
from typing import List, Optional

from ..schema.plan import Plan, Step, StepResult, StepStatus
from ..schema.action import Observation
from ..schema.message import Message, MessageRole
from ..llm.base import LLMClient
from ..prompts.summarize_prompt import (
    build_summarize_prompt,
    HISTORY_SUMMARIZE_SYSTEM_PROMPT,
    SUMMARIZE_SYSTEM_PROMPT,
)
from ..events import EventEmitter, EventType
from .planner import Planner
from .executor import Executor


class AgentLoop:
    """主控制循环：plan → 逐 step 执行（内部 ReAct）→ LLM 总结 → 必要时 replan。"""

    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        max_steps: int = 20,
        llm: Optional[LLMClient] = None,
        config=None,
        emitter: Optional[EventEmitter] = None,
    ):
        self.planner = planner
        self.executor = executor
        self.max_steps = max_steps
        self.llm = llm
        self.config = config
        self.emitter = emitter

    def run(self, goal: str, recorder=None, session_id: Optional[str] = None, memory=None, prior_history: List[dict] = None) -> str:
        """运行至计划完成或终止条件触发，返回最终答案。

        recorder 为 SessionRecorder 时，会把 meta / plan / 每步 trace 落盘；
        session_id 会附加到发出的事件上；
        memory 为 MemoryService 时，开始召回长期记忆注入上下文、结束提取沉淀；
        prior_history 为会话恢复时的历史上下文，注入到 planner 的 context 和首轮 history。
        """
        try:
            return self._run(goal, recorder=recorder, session_id=session_id, memory=memory, prior_history=prior_history)
        except Exception as e:  # noqa: BLE001 —— 运行异常以事件暴露
            self._emit(EventType.ERROR, {"message": str(e)}, session_id=session_id)
            raise

    def _run(self, goal: str, recorder=None, session_id: Optional[str] = None, memory=None, prior_history: List[dict] = None) -> str:
        if recorder:
            recorder.record_meta(goal)
        # 长期记忆召回：作为上下文前缀注入每个 step（不受 history 窗口淘汰影响）
        memory_prefix: List[dict] = []
        if memory is not None:
            recalled = memory.recall(goal, session_id=session_id)
            if recalled:
                memory_prefix = [{"memory": [r.content for r in recalled]}]
        # 会话恢复：注入上轮历史作为 planner 上下文和初始 history
        prior_history = prior_history or []
        plan: Plan = self.planner.plan(goal, context=prior_history)
        self._emit(EventType.PLAN_CREATED, {"plan": plan.model_dump()}, session_id=session_id)
        if recorder:
            recorder.record_plan(plan)
        executed = 0
        # 跨 step 共享上下文：每步完成后追加本步结论与工具观察（含 ask_user 的用户反馈）
        history: List[dict] = list(prior_history)

        while not self._should_terminate(plan):
            step = self.executor.next_step(plan)
            if step is None:
                break

            self._emit(
                EventType.STEP_STARTED,
                {"step": {"id": step.id, "description": step.description}},
                session_id=session_id, step_id=step.id,
            )
            result: StepResult = self.executor.execute_step(
                step, history=memory_prefix + history, goal=goal
            )
            trace = self.executor.react_engine.last_trace
            if recorder:
                for entry in trace:
                    recorder.record_trace(entry)
            self.executor.update(plan, result)
            self._emit(
                EventType.STEP_FINISHED,
                {
                    "step": {"id": step.id, "description": step.description},
                    "result": result.model_dump(),
                },
                session_id=session_id, step_id=step.id,
            )
            history.append(self._history_entry(step, result, trace))
            history = self._apply_window(history)
            executed += 1

            if executed >= self.max_steps:
                break

            # 成功后自动增量调整未执行的 steps
            if result.success and plan.pending_steps():
                adjusted = None
                try:
                    adjusted = self.planner.adjust(plan, step, history)
                except Exception:
                    pass  # adjust 失败不影响主流程
                if adjusted is not None:
                    plan = adjusted
                    self._emit(
                        EventType.PLAN_ADJUSTED,
                        {"plan": plan.model_dump()},
                        session_id=session_id,
                    )
                    if recorder:
                        recorder.record_plan(plan)

            if self.planner.should_replan(plan, result):
                failed = next(s for s in plan.steps if s.id == result.step_id)
                obs = Observation(content=result.error or result.output)
                plan = self.planner.replan(plan, failed, obs)
                self._emit(
                    EventType.REPLANNED, {"plan": plan.model_dump()},
                    session_id=session_id,
                )
                if recorder:
                    recorder.record_plan(plan)

        if recorder:
            recorder.finish()
        answer = self._summarize(plan)
        self._emit(
            EventType.FINAL_ANSWER, {"answer": answer},
            session_id=session_id,
        )
        # run 结束：提取本次候选记忆并走分代流转（晋升 / 合并 / 淘汰）
        if memory is not None:
            try:
                memory.absorb(goal, history, answer, session_id=session_id)
            except Exception:  # noqa: BLE001 —— 记忆沉淀失败不影响主流程
                pass
        return answer

    def _should_terminate(self, plan: Plan) -> bool:
        """计划全部完成 / 全部终态则终止。"""
        return all(s.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)
                   for s in plan.steps)

    def _history_entry(self, step: Step, result: StepResult, trace: List[dict]) -> dict:
        """把单个 step 的执行结果压缩为可注入后续步骤上下文的记录。

        observations 完整保留工具问答（ask_user 的用户回答就在这里），
        单条观察按 observation_max_chars 截断。
        """
        observations = [
            {
                "tool": e["action"].tool_name,
                "args": e["action"].args,
                "result": self._clip_observation(e["observation"]),
            }
            for e in trace if "action" in e
        ]
        return {
            "step_id": step.id,
            "description": step.description,
            "success": result.success,
            "output": result.output or result.error or "",
            "observations": observations,
        }

    # ---------- history 窗口管理 ----------

    def _cfg_value(self, key: str, default):
        """从 ConfigProvider 取值（热加载生效）；无配置时用默认值。"""
        if self.config is not None:
            return getattr(self.config.get_config(), key, default)
        return default

    def _clip_observation(self, text: str) -> str:
        """按 observation_max_chars 截断单条工具观察，防止大结果撑爆上下文。"""
        max_chars = self._cfg_value("observation_max_chars", 500)
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars] + f"…（已截断，原 {len(text)} 字符）"

    def _apply_window(self, history: List[dict]) -> List[dict]:
        """对 history 应用滑动窗口：保留最近 history_window 条完整条目。

        超窗条目按 history_summarize 配置处理：
        - True（默认）：与既有摘要一起压缩为一条滚动摘要，置顶保留关键信息；
        - False：直接丢弃（trace.jsonl 已完整落盘，不丢数据）。
        含 ask_user 用户反馈的条目最后淘汰（用户输入是最高价值信息）。
        """
        window = self._cfg_value("history_window", 10)
        if window <= 0:
            return history
        entries = [h for h in history if "summary" not in h]
        old_summary = next((h["summary"] for h in history if "summary" in h), "")
        evicted: List[dict] = []
        while len(entries) > window:
            # 优先淘汰最旧的非用户反馈条目；全是用户反馈时按最旧淘汰
            idx = next(
                (i for i, h in enumerate(entries) if not self._has_user_feedback(h)), 0
            )
            evicted.append(entries.pop(idx))
        if not evicted:
            return history
        if not self._cfg_value("history_summarize", True):
            return entries
        summary = self._rollup_summary(old_summary, evicted)
        return ([{"summary": summary}] if summary else []) + entries

    @staticmethod
    def _has_user_feedback(entry: dict) -> bool:
        return any(ob.get("tool") == "ask_user" for ob in entry.get("observations", []))

    def _rollup_summary(self, old_summary: str, evicted: List[dict]) -> str:
        """把被挤出窗口的条目（连同旧摘要）压缩为一条滚动摘要。

        有 LLM 时用模型压缩（提示词可用 prompts.history_summarize_system 覆盖）；
        无 LLM 或调用失败时退化为文本压缩（保留结论、丢弃过程细节）。
        """
        parts: List[str] = []
        if old_summary:
            parts.append(f"此前摘要：{old_summary}")
        for h in evicted:
            line = f"步骤[{h['step_id']}]（{'成功' if h.get('success') else '失败'}）{h.get('description', '')} → {h.get('output', '')}"
            obs = "；".join(
                f"{o['tool']}({o['args']})→{self._clip_observation(o['result'])}"
                for o in h.get("observations", [])
            )
            if obs:
                line += f"（{obs}）"
            parts.append(line)
        digest = "\n".join(parts)

        if self.llm is None:
            return digest
        system_prompt = HISTORY_SUMMARIZE_SYSTEM_PROMPT
        if self.config is not None:
            system_prompt = self.config.get_prompt(
                "history_summarize_system", default=HISTORY_SUMMARIZE_SYSTEM_PROMPT
            )
        try:
            resp = self.llm.complete([
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=f"待压缩的历史记录：\n{digest}"),
            ])
            return resp.content or digest
        except Exception:  # noqa: BLE001 —— 摘要失败不影响主流程
            return digest

    def _summarize(self, plan: Plan) -> str:
        """汇总各 step 结果，交由 LLM 精炼为最终输出。

        无 LLM 时退化为纯文本拼接。
        """
        # 无 LLM 可用时，退化为简单拼接
        if self.llm is None:
            lines = [f"# 目标: {plan.goal}"]
            for s in plan.steps:
                status = s.status.value
                out = s.result.output if s.result else ""
                lines.append(f"- [{status}] {s.description}: {out}")
            return "\n".join(lines)

        # 从配置取系统提示词（支持热加载覆盖），无配置则用内置默认
        if self.config is not None:
            system_prompt = self.config.get_prompt("summarize_system", default=SUMMARIZE_SYSTEM_PROMPT)
        else:
            system_prompt = SUMMARIZE_SYSTEM_PROMPT

        user_prompt = build_summarize_prompt(plan)
        resp = self.llm.complete([
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ])
        return resp.content

    def _emit(
        self,
        type: EventType,
        payload: dict,
        session_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> None:
        """发布事件（未配置 emitter 时静默跳过）。"""
        if self.emitter is not None:
            self.emitter.emit(type, payload=payload, session_id=session_id, step_id=step_id)
