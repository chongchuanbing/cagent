"""度量采集器：订阅 EventEmitter，实时聚合执行度量。"""
from datetime import datetime
from typing import Dict, List, Optional

from ..events.schema import AgentEvent, EventType
from .schema import StepMetrics, ToolCallRecord, TurnMetrics
from ..utils.logging import get_logger

logger = get_logger("metrics.collector")


class MetricsCollector:
    """订阅 EventEmitter，实时聚合执行度量。
    
    作为 EventHandler 注册到 EventEmitter，监听事件流自动聚合统计。
    零侵入核心循环，所有统计逻辑在此处完成。
    """
    
    def __init__(self):
        self._current_turn: Optional[TurnMetrics] = None
        self._current_step: Optional[StepMetrics] = None
        self._step_start_time: Optional[datetime] = None  # 当前 step 开始时间
        self._pending_tool_calls: List[Dict] = []  # 存储未完成的工具调用 {step_id, seq, start_time}
    
    def handle_event(self, event: AgentEvent) -> None:
        """事件处理入口（注册为 EventHandler）。"""
        handler = {
            EventType.TURN_STARTED: self._on_turn_started,
            EventType.TURN_FINISHED: self._on_turn_finished,
            EventType.STEP_STARTED: self._on_step_started,
            EventType.STEP_FINISHED: self._on_step_finished,
            EventType.THOUGHT: self._on_thought,
            EventType.TOOL_CALL: self._on_tool_call,
            EventType.TOOL_RESULT: self._on_tool_result,
            EventType.REAL_TOOL_EXEC: self._on_real_tool_exec,
            EventType.REPLANNED: self._on_replanned,
            EventType.PLAN_ADJUSTED: self._on_plan_adjusted,
            EventType.DOOM_LOOP_DETECTED: self._on_doom_loop_detected,
        }.get(event.type)
        
        if handler:
            handler(event)
    
    def get_current_turn(self) -> Optional[TurnMetrics]:
        """获取当前 turn 的度量（用于落盘）。"""
        return self._current_turn
    
    # ── 事件处理器 ────────────────────────────────────────
    
    def _on_turn_started(self, event: AgentEvent) -> None:
        """一次 run() 调用开始。"""
        goal = event.payload.get("goal", "")
        self._current_turn = TurnMetrics(
            session_id=event.session_id or "",
            goal=goal,
            started_at=event.ts,
        )
    
    def _on_turn_finished(self, event: AgentEvent) -> None:
        """一次 run() 调用结束。"""
        if self._current_turn:
            status = event.payload.get("status", "done")
            self._current_turn.status = status
            self._current_turn.finalize()
        # 注意：不落盘，由调用方（Agent.run）从 collector 取出并保存
        # 不清空 _current_turn，保留供调用方读取
    
    def _on_step_started(self, event: AgentEvent) -> None:
        """Step 开始执行。"""
        if not self._current_turn:
            return
        step_id = event.step_id or ""
        description = event.payload.get("step", {}).get("description", "")
        self._current_step = StepMetrics(
            step_id=step_id,
            description=description,
            status="running",
        )
        self._step_start_time = event.ts  # 记录 step 开始时间
    
    def _on_step_finished(self, event: AgentEvent) -> None:
        """Step 执行结束。"""
        if not self._current_turn or not self._current_step:
            return
        result = event.payload.get("result", {})
        self._current_step.status = "done" if result.get("success") else "failed"
        # 计算 step 耗时
        if self._step_start_time:
            self._current_step.duration_ms = int(
                (event.ts - self._step_start_time).total_seconds() * 1000
            )
        self._current_turn.steps.append(self._current_step)
        self._current_step = None
        self._step_start_time = None  # 清空 step 开始时间
    
    def _on_thought(self, event: AgentEvent) -> None:
        """LLM 推理过程。"""
        if not self._current_step:
            return
        self._current_step.llm_calls += 1
    
    def _on_tool_call(self, event: AgentEvent) -> None:
        """工具调用开始（代理层）。"""
        if not self._current_step:
            return
        name = event.payload.get("name", "")
        args = event.payload.get("args", {})
        # 创建 ToolCallRecord
        record = ToolCallRecord(
            tool_name=name,
            args=args,
            ok=True,  # 暂定，等 TOOL_RESULT 更新
            duration_ms=0,
            observation_length=0,
            step_id=self._current_step.step_id,
            seq=event.seq,
        )
        self._current_step.tool_calls.append(record)
        # 记录到待匹配列表（用于 TOOL_RESULT 匹配）
        self._pending_tool_calls.append({
            "tool_name": name,
            "step_id": self._current_step.step_id,
            "seq": event.seq,
            "start_time": event.ts,
        })
    
    def _on_tool_result(self, event: AgentEvent) -> None:
        """工具调用结束（代理层）。"""
        name = event.payload.get("name", "")
        content = event.payload.get("content", "")
        ok = event.payload.get("ok", True)
        
        # 从待匹配列表中查找（按工具名）
        matched_idx = None
        for i, pending in enumerate(self._pending_tool_calls):
            if pending["tool_name"] == name:
                matched_idx = i
                break
        
        if matched_idx is None:
            logger.warning(f"[MetricsCollector] TOOL_RESULT {name}: 未找到匹配的 TOOL_CALL")
            return
        
        # 计算耗时并更新记录
        pending = self._pending_tool_calls.pop(matched_idx)
        duration_ms = int((event.ts - pending["start_time"]).total_seconds() * 1000)
        seq = pending["seq"]
        
        # 在当前 step 中查找对应的 ToolCallRecord
        if self._current_step:
            for tc in self._current_step.tool_calls:
                if tc.seq == seq:
                    tc.duration_ms = duration_ms
                    tc.observation_length = len(content)
                    tc.ok = ok
                    break
        # 在已完成的 steps 中查找（不应该发生，但作为兜底）
        elif self._current_turn:
            for step in self._current_turn.steps:
                for tc in step.tool_calls:
                    if tc.seq == seq:
                        tc.duration_ms = duration_ms
                        tc.observation_length = len(content)
                        tc.ok = ok
                        break
    
    def _on_real_tool_exec(self, event: AgentEvent) -> None:
        """真实工具执行（run_tool 代理内部）。
        
        时序：TOOL_CALL → REAL_TOOL_EXEC → TOOL_RESULT
        REAL_TOOL_EXEC 在 TOOL_CALL 之后，所以最后一个 ToolCallRecord 已存在。
        """
        if not self._current_step or not self._current_step.tool_calls:
            return
        real_name = event.payload.get("real_tool_name", "")
        source = event.payload.get("source", "plugin")
        # 更新最后一个 ToolCallRecord（REAL_TOOL_EXEC 紧跟 TOOL_CALL 之后）
        last_tc = self._current_step.tool_calls[-1]
        last_tc.real_tool_name = real_name
        last_tc.source = source
    
    def _on_replanned(self, event: AgentEvent) -> None:
        """计划被修订。"""
        if self._current_turn:
            self._current_turn.replan_count += 1
    
    def _on_plan_adjusted(self, event: AgentEvent) -> None:
        """计划被增量调整。"""
        if self._current_turn:
            self._current_turn.adjust_count += 1
    
    def _on_doom_loop_detected(self, event: AgentEvent) -> None:
        """死循环检测。"""
        if self._current_step:
            self._current_step.doom_loop_detected = True
