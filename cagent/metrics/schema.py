"""度量数据模型。

分层：
  ToolCallRecord  → 单次工具调用
  StepMetrics     → 单个 Plan Step 的执行度量
  TurnMetrics     → 一次 run() 调用的完整度量
  ToolAggregation → 按工具名聚合的统计
  SessionMetrics  → 整个会话的聚合度量（跨 turn）
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ── 工具来源分类 ──────────────────────────────────────────

# 合法取值：
#   builtin  — 框架内置（ask_user / remember / tool_guide 等）
#   plugin   — 插件包（OperationTree execute.mode=executor）
#   mcp      — MCP 工具（execute.mode=mcp）
#   shell    — shell 命令（execute.mode=shell）
#   skill    — Skill 相关（skill_guide / run_skill / read_skill_file）

class ToolCallRecord(BaseModel):
    """单次工具调用的度量记录。"""

    tool_name: str                              # 代理工具名（run_tool / ask_user 等）
    real_tool_name: Optional[str] = None        # 真实工具名（run_tool 代理时填实际 path）
    source: str = "builtin"                     # 工具来源分类
    args: Dict = Field(default_factory=dict)    # 调用参数（脱敏后）
    ok: bool = True                             # 是否成功
    duration_ms: int = 0                        # 执行耗时（毫秒）
    observation_length: int = 0                 # 返回内容长度（字符数）
    error_kind: Optional[str] = None            # 失败类型
    step_id: str = ""                           # 所属 step
    seq: int = 0                                # 全局事件序号


class StepMetrics(BaseModel):
    """单个 Plan Step 的执行度量。"""

    step_id: str
    description: str = ""
    status: str = "pending"                     # done / failed / skipped
    react_iterations: int = 0                   # ReAct 循环次数
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    llm_calls: int = 0                          # LLM 调用次数（= thought 次数）
    duration_ms: int = 0
    doom_loop_detected: bool = False


class ToolAggregation(BaseModel):
    """单工具的聚合统计。"""

    tool_name: str
    source: str = "builtin"
    call_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    avg_duration_ms: float = 0.0
    total_duration_ms: int = 0


class TurnMetrics(BaseModel):
    """一次 run() 调用的完整度量。"""

    session_id: str = ""
    turn_id: int = 0
    goal: str = ""
    status: str = "running"                     # running / done / failed / max_steps
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    # Plan 维度
    total_steps: int = 0
    successful_steps: int = 0
    failed_steps: int = 0
    replan_count: int = 0
    adjust_count: int = 0
    # 汇总
    total_tool_calls: int = 0
    total_llm_calls: int = 0
    duration_ms: int = 0
    # 明细
    steps: List[StepMetrics] = Field(default_factory=list)
    # 工具聚合（按工具名）
    tool_summary: Dict[str, ToolAggregation] = Field(default_factory=dict)
    # 来源聚合（按 source 分类）
    source_summary: Dict[str, int] = Field(default_factory=dict)

    def finalize(self) -> None:
        """run 结束时调用：计算 duration、聚合统计。"""
        now = datetime.now(timezone.utc)
        self.finished_at = now
        self.duration_ms = int((now - self.started_at).total_seconds() * 1000)
        # 聚合 step 统计
        for step in self.steps:
            self.total_steps += 1
            if step.status == "done":
                self.successful_steps += 1
            elif step.status == "failed":
                self.failed_steps += 1
            self.total_llm_calls += step.llm_calls
            for tc in step.tool_calls:
                self.total_tool_calls += 1
                # 工具名聚合
                key = tc.real_tool_name or tc.tool_name
                if key not in self.tool_summary:
                    self.tool_summary[key] = ToolAggregation(
                        tool_name=key, source=tc.source
                    )
                agg = self.tool_summary[key]
                agg.call_count += 1
                if tc.ok:
                    agg.success_count += 1
                else:
                    agg.fail_count += 1
                agg.total_duration_ms += tc.duration_ms
                # 来源聚合
                self.source_summary[tc.source] = self.source_summary.get(tc.source, 0) + 1
        # 计算平均耗时
        for agg in self.tool_summary.values():
            if agg.call_count > 0:
                agg.avg_duration_ms = agg.total_duration_ms / agg.call_count


class SessionMetrics(BaseModel):
    """整个会话的聚合度量（跨 turn）。"""

    session_id: str
    total_turns: int = 0
    total_duration_ms: int = 0
    total_tool_calls: int = 0
    total_llm_calls: int = 0
    turns: List[TurnMetrics] = Field(default_factory=list)
    tool_usage_ranking: List[ToolAggregation] = Field(default_factory=list)

    @classmethod
    def from_turns(cls, session_id: str, turns: List[TurnMetrics]) -> "SessionMetrics":
        """从 turn 列表聚合出会话度量。"""
        total_duration = sum(t.duration_ms for t in turns)
        total_tools = sum(t.total_tool_calls for t in turns)
        total_llm = sum(t.total_llm_calls for t in turns)
        # 跨 turn 工具聚合
        merged: Dict[str, ToolAggregation] = {}
        for t in turns:
            for key, agg in t.tool_summary.items():
                if key not in merged:
                    merged[key] = ToolAggregation(tool_name=key, source=agg.source)
                m = merged[key]
                m.call_count += agg.call_count
                m.success_count += agg.success_count
                m.fail_count += agg.fail_count
                m.total_duration_ms += agg.total_duration_ms
        for agg in merged.values():
            if agg.call_count > 0:
                agg.avg_duration_ms = agg.total_duration_ms / agg.call_count
        ranking = sorted(merged.values(), key=lambda a: a.call_count, reverse=True)
        return cls(
            session_id=session_id,
            total_turns=len(turns),
            total_duration_ms=total_duration,
            total_tool_calls=total_tools,
            total_llm_calls=total_llm,
            turns=turns,
            tool_usage_ranking=ranking,
        )
