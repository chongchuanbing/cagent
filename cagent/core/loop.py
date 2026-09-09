"""AgentLoop：外层编排控制，串联 Plan-Executor 与 ReAct。"""
import traceback
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
from ..utils.logging import get_logger

logger = get_logger(__name__)


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

    def run(
        self,
        goal: str,
        recorder=None,
        session_id: Optional[str] = None,
        memory=None,
        prior_history: List[dict] = None,
        resume_plan: Optional[Plan] = None,
        resume_history: Optional[List[dict]] = None,
    ) -> str:
        """运行至计划完成或终止条件触发，返回最终答案。

        recorder 为 SessionRecorder 时，会把 meta / plan / 每步 trace 落盘；
        session_id 会附加到发出的事件上；
        memory 为 MemoryService 时，开始召回长期记忆注入上下文、结束提取沉淀；
        prior_history 为会话恢复时的历史上下文，注入到 planner 的 context 和首轮 history；
        resume_plan 为中断恢复时的计划对象；
        resume_history 为中断恢复时的历史上下文。
        """
        try:
            return self._run(
                goal,
                recorder=recorder,
                session_id=session_id,
                memory=memory,
                prior_history=prior_history,
                resume_plan=resume_plan,
                resume_history=resume_history,
            )
        except Exception as e:  # noqa: BLE001 —— 运行异常以事件暴露
            logger.exception(f"AgentLoop.run 执行失败: {type(e).__name__}: {e}")
            self._emit(EventType.ERROR, {"message": str(e)}, session_id=session_id)
            raise

    def _run(
        self,
        goal: str,
        recorder=None,
        session_id: Optional[str] = None,
        memory=None,
        prior_history: Optional[List[dict]] = None,
        resume_plan: Optional[Plan] = None,
        resume_history: Optional[List[dict]] = None,
    ) -> str:
        logger.info(f"_run: 开始执行目标 - {goal[:100]}")
        if recorder:
            recorder.record_meta(goal)
        # 度量采集：标记 turn 开始
        self._emit(EventType.TURN_STARTED, {"goal": goal}, session_id=session_id)
        # 长期记忆召回：作为上下文前缀注入每个 step（不受 history 窗口淘汰影响）
        memory_prefix: List[dict] = []
        if memory is not None:
            logger.debug("_run: 开始召回长期记忆")
            recalled = memory.recall(goal, session_id=session_id)
            if recalled:
                logger.info(f"_run: 召回 {len(recalled)} 条长期记忆")
                memory_prefix = [{"memory": [r.content for r in recalled]}]

        # 中断恢复 vs 正常多轮：两条路径
        if resume_plan is not None:
            # 中断恢复路径：使用传入的旧 plan 和 history
            plan = resume_plan
            history = resume_history or []
            done_count = sum(1 for s in plan.steps if s.status == StepStatus.DONE)
            logger.info(
                f"_run: 中断恢复模式，加载旧 plan（{len(plan.steps)} steps，"
                f"{done_count} done，{sum(1 for s in plan.steps if s.status == StepStatus.PENDING)} pending）"
            )
            logger.info(f"_run: 中断恢复模式，加载旧 history（{len(history)} 条）")
        else:
            # 正常多轮路径：生成新 plan
            prior_history = prior_history or []
            logger.info(f"_run: 开始生成计划，历史上下文 {len(prior_history)} 条")
            plan: Plan = self.planner.plan(goal, context=prior_history)
            logger.info(f"_run: 计划生成完成，共 {len(plan.steps)} 个步骤")
            self._emit(EventType.PLAN_CREATED, {"plan": plan.model_dump()}, session_id=session_id)
            if recorder:
                recorder.record_plan(plan)
            history: List[dict] = list(prior_history)

        executed = 0
        logger.info(f"_run: 开始执行主循环，共 {len(plan.steps)} 个步骤")
        try:
            while not self._should_terminate(plan):
                step = self.executor.next_step(plan)
                if step is None:
                    logger.info("_run: 无可执行步骤，退出循环")
                    break

                logger.info(f"_run: 开始执行步骤 {step.id} - {step.description[:50]}")
                self._emit(
                    EventType.STEP_STARTED,
                    {"step": {"id": step.id, "description": step.description}},
                    session_id=session_id, step_id=step.id,
                )
                result: StepResult = self.executor.execute_step(
                    step, history=memory_prefix + history, goal=goal
                )
                logger.info(f"_run: 步骤 {step.id} 执行完成，状态: {'成功' if result.success else '失败'}")
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
                history_entry = self._history_entry(step, result, trace)
                history.append(history_entry)
                history = self._apply_window(history)

                # 实时持久化：每步完成后立即保存 plan 状态和 history 条目
                # 这样中断后可以从断点恢复，而不是从头开始
                if recorder:
                    recorder.record_plan(plan)  # 保存最新的 step 状态
                    recorder.record_history_entry(history_entry)  # 追加本步 history

                executed += 1

                if executed >= self.max_steps:
                    logger.warning(f"_run: 达到最大步骤数限制 {self.max_steps}，强制退出")
                    break

                # 成功后自动增量调整未执行的 steps
                if result.success and plan.pending_steps():
                    adjusted = None
                    try:
                        logger.debug(f"_run: 步骤 {step.id} 成功，尝试调整计划")
                        adjusted = self.planner.adjust(plan, step, history)
                    except Exception as e:
                        logger.exception(f"Planner.adjust 失败: {type(e).__name__}: {e}")
                        pass  # adjust 失败不影响主流程
                    if adjusted is not None:
                        plan = adjusted
                        logger.info(f"_run: 计划调整完成，新版本 {plan.version}")
                        self._emit(
                            EventType.PLAN_ADJUSTED,
                            {"plan": plan.model_dump()},
                            session_id=session_id,
                        )
                        if recorder:
                            recorder.record_plan(plan)

                if self.planner.should_replan(plan, result):
                    logger.warning(f"_run: 步骤 {step.id} 失败，需要重新规划")
                    failed = next(s for s in plan.steps if s.id == result.step_id)
                    obs = Observation(content=result.error or result.output)
                    plan = self.planner.replan(plan, failed, obs)
                    self._emit(
                        EventType.REPLANNED, {"plan": plan.model_dump()},
                        session_id=session_id,
                    )
                    if recorder:
                        recorder.record_plan(plan)

            logger.info(f"_run: 主循环结束，共执行 {executed} 个步骤")
            if recorder:
                recorder.finish()
        except Exception:
            # 异常中断：标记 meta.status = "interrupted"，保留已执行步骤的状态，
            # 下次 resume 时 Agent.run() 可检测到并走中断恢复路径
            logger.exception("_run: 执行异常，标记会话为中断状态")
            if recorder:
                try:
                    recorder.finish(status="interrupted")
                except Exception:
                    logger.exception("_run: finish(interrupted) 失败")
            raise

        logger.debug("_run: 开始生成最终总结")
        answer = self._summarize(plan)
        logger.info(f"_run: 最终总结生成完成，长度 {len(answer)} 字符")
        self._emit(
            EventType.FINAL_ANSWER, {"answer": answer},
            session_id=session_id,
        )
        # run 结束：提取本次候选记忆并走分代流转（晋升 / 合并 / 淘汰）
        if memory is not None:
            try:
                memory.absorb(goal, history, answer, session_id=session_id)
            except Exception as e:  # noqa: BLE001 —— 记忆沉淀失败不影响主流程
                logger.exception(f"Memory.absorb 失败: {type(e).__name__}: {e}")

            # L6: 将失败账本中的环境事实沉淀到长期记忆
            if self.executor.react_engine._failure_ledger is not None:
                try:
                    memory.absorb_env_facts(
                        self.executor.react_engine._failure_ledger,
                        None,
                        session_id=session_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"Memory.absorb_env_facts 失败: {type(e).__name__}: {e}")

        # 度量采集：标记 turn 结束
        self._emit(EventType.TURN_FINISHED, {"status": "done"}, session_id=session_id)

        return answer

    def _should_terminate(self, plan: Plan) -> bool:
        """计划全部完成 / 全部终态则终止。"""
        return all(s.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)
                   for s in plan.steps)

    def _history_entry(self, step: Step, result: StepResult, trace: List[dict]) -> dict:
        """把单个 step 的执行结果压缩为可注入后续步骤上下文的记录。

        保留 step 的交付结论（output）和工具观察（截断后）。完整轨迹保留在
        trace.jsonl 用于审计/调试。
        """
        # 提取工具观察并截断
        observations = []
        for entry in trace:
            if "observation" in entry:
                tool_name = entry.get("action", {}).get("tool_name", "unknown")
                obs = entry["observation"]
                clipped = self._clip_observation(obs)
                observations.append(f"{tool_name}: {clipped}")
        
        output_parts = []
        if result.output:
            output_parts.append(result.output)
        if observations:
            output_parts.append("工具观察: " + "; ".join(observations))
        
        return {
            "step_id": step.id,
            "description": step.description,
            "success": result.success,
            "output": " | ".join(output_parts) if output_parts else (result.error or ""),
        }

    # ---------- history 窗口管理 ----------

    def _cfg_value(self, key: str, default):
        """从 ConfigProvider 取值（热加载生效）；无配置时用默认值。"""
        if self.config is not None:
            return getattr(self.config.get_config(), key, default)
        return default

    def _clip_observation(self, text: str) -> str:
        """按 observation_max_chars 截断单条工具观察，防止大结果撑爆上下文。

        读取类输出（shell_exec 行号化视图，以 `=== <path> | ` 开头）走更大阈值
        read_max_chars，且截断时**保留头部契约**（位置/总行数/续读指令不被吞）。
        结构化截断（shell_exec 非读取类，以 `[元信息]` 开头）走中等阈值
        observation_max_chars * 2，且截断时**保留元信息头和截断建议**。
        普通观察仍走 observation_max_chars（默认 500）。
        """
        # 读取类识别：=== <path> | ... === 头部契约
        stripped = text.lstrip()
        read_marker = "=== "
        if stripped.startswith(read_marker) and " | " in stripped[:80]:
            max_chars = self._cfg_value("read_max_chars", 30000)
            if max_chars <= 0 or len(text) <= max_chars:
                return text
            # 保留头部契约：找到 [read] 行作为正文分界
            marker = "\n[read] "
            idx = text.find(marker)
            if idx > 0:
                # 头部结束位置：[read] 行末尾
                nl_after = text.find("\n", idx + 1)
                if nl_after > 0:
                    header = text[: nl_after + 1]
                    body_budget = max_chars - len(header) - 50
                    if body_budget > 0:
                        body = text[
                            nl_after + 1 : nl_after + 1 + body_budget
                        ]
                        return (
                            header + body
                            + f"\n…（已截断，原 {len(text)} 字符，"
                            "请用 sed -n 'A,Bp' <file> 续读）"
                        )
            # 找不到 [read] 标记（不正常的视图），粗暴切
            return text[:max_chars] + f"…（已截断，原 {len(text)} 字符）"

        # 结构化截断识别：[元信息] 开头（非读取类命令的结构化截断）
        meta_marker = "[元信息]"
        if stripped.startswith(meta_marker):
            # 结构化截断走中等阈值：observation_max_chars * 2
            max_chars = self._cfg_value("observation_max_chars", 500) * 2
            if max_chars <= 0 or len(text) <= max_chars:
                return text
            # 找到元信息头结束位置（第一个 "---"）
            meta_end = text.find("\n---\n")
            if meta_end > 0:
                meta_header = text[: meta_end + 5]  # 包含 \n---\n
                # 找到截断建议开始位置（最后一个 "---"）
                footer_start = text.rfind("\n---\n")
                if footer_start > meta_end:
                    footer = text[footer_start:]
                    # 元信息头 + 正文（截断）+ 截断建议
                    body_budget = max_chars - len(meta_header) - len(footer) - 50
                    if body_budget > 0:
                        body = text[meta_end + 5 : footer_start]
                        if len(body) > body_budget:
                            body = body[:body_budget] + "\n...（正文截断）"
                        return meta_header + body + footer
                else:
                    # 没有截断建议，只有元信息头
                    body_budget = max_chars - len(meta_header) - 50
                    if body_budget > 0:
                        body = text[meta_end + 5 : meta_end + 5 + body_budget]
                        return meta_header + body + "\n...（已截断）"
            # 找不到元信息头结构，粗暴切
            return text[:max_chars] + f"…（已截断，原 {len(text)} 字符）"

        # 普通观察
        max_chars = self._cfg_value("observation_max_chars", 500)
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars] + f"…（已截断，原 {len(text)} 字符）"

    def _apply_window(self, history: List[dict]) -> List[dict]:
        """对 history 应用滑动窗口：保留最近 history_window 条完整条目。

        超窗条目按 history_summarize 配置处理：
        - True（默认）：与既有摘要一起压缩为一条滚动摘要，置顶保留关键信息；
        - False：直接丢弃（trace.jsonl 已完整落盘，不丢数据）。
        """
        window = self._cfg_value("history_window", 10)
        if window <= 0:
            return history
        entries = [h for h in history if "summary" not in h]
        old_summary = next((h["summary"] for h in history if "summary" in h), "")
        evicted: List[dict] = []
        while len(entries) > window:
            evicted.append(entries.pop(0))
        if not evicted:
            return history
        if not self._cfg_value("history_summarize", True):
            return entries
        summary = self._rollup_summary(old_summary, evicted)
        return ([{"summary": summary}] if summary else []) + entries

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
        except Exception as e:  # noqa: BLE001 —— 摘要失败不影响主流程
            logger.exception(f"LLM 摘要失败: {type(e).__name__}: {e}")
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
