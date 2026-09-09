"""度量采集器：订阅 EventEmitter，实时聚合执行度量。"""
from datetime import datetime
from typing import Dict, List, Optional

from ..events.schema import AgentEvent, EventType
from .schema import StepMetrics, ToolCallRecord, TurnMetrics


class MetricsCollector:
    """订阅 EventEmitter，实时聚合执行度量。
    
    作为 EventHandler 注册到 EventEmitter，监听事件流自动聚合统计。
    零侵入核心循环，所有统计逻辑在此处完成。
    """
    
    def __init__(self):
        self._current_turn: Optional[TurnMetrics] = None
        self._current_step: Optional[StepMetrics] = None
        self._tool_call_timestamps: Dict[str, datetime] = {}  # key: "step_id:seq"
    
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
    
    def _on_step_finished(self, event: AgentEvent) -> None:
        """Step 执行结束。"""
        if not self._current_turn or not self._current_step:
            return
        result = event.payload.get("result", {})
        self._current_step.status = "done" if result.get("success") else "failed"
        # 计算 step 耗时（从第一个 tool_call 的时间戳推算）
        if self._current_step.tool_calls:
            # 找到第一个 tool_call 的时间戳
            first_seq = self._current_step.tool_calls[0].seq
            if first_seq > 0:
                key = f"{self._current_step.step_id}:{first_seq}"
                first_tc_time = self._tool_call_timestamps.get(key)
                if first_tc_time:
                    self._current_step.duration_ms = int(
                        (event.ts - first_tc_time).total_seconds() * 1000
                    )
        self._current_turn.steps.append(self._current_step)
        self._current_step = None
    
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
        # 记录时间戳（用于计算耗时）
        key = f"{self._current_step.step_id}:{event.seq}"
        self._tool_call_timestamps[key] = event.ts
        # 创建 ToolCallRecord（等 TOOL_RESULT 回填结果）
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
    
    def _on_tool_result(self, event: AgentEvent) -> None:
        """工具调用结束（代理层）。"""
        if not self._current_step:
            return
        name = event.payload.get("name", "")
        content = event.payload.get("content", "")
        ok = event.payload.get("ok", True)
        # 找到对应的 TOOL_CALL 记录（按 step_id + seq）
        key = f"{self._current_step.step_id}:{event.seq}"
        start_time = self._tool_call_timestamps.pop(key, None)
        duration_ms = 0
        if start_time:
            duration_ms = int((event.ts - start_time).total_seconds() * 1000)
        # 回填到 ToolCallRecord
        for tc in reversed(self._current_step.tool_calls):
            if tc.seq == event.seq:
                tc.ok = ok
                tc.duration_ms = duration_ms
                tc.observation_length = len(content)
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
