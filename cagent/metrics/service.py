"""度量查询与聚合服务。

提供：
  - 单次 turn 详情查询
  - 整个会话聚合摘要
  - 人类可读的执行报告格式化
"""
from typing import List, Optional

from .backend import MetricsBackend
from .schema import SessionMetrics, TurnMetrics


class MetricsService:
    """度量查询与聚合服务。"""

    def __init__(self, backend: MetricsBackend):
        self._backend = backend

    def get_turn_metrics(self, session_id: str, turn_id: int) -> Optional[TurnMetrics]:
        """获取指定 turn 的详细度量。"""
        turns = self._backend.load_turns(session_id)
        for t in turns:
            if t.turn_id == turn_id:
                return t
        return None

    def get_session_summary(self, session_id: str) -> SessionMetrics:
        """获取整个会话的聚合摘要。"""
        turns = self._backend.load_turns(session_id)
        return SessionMetrics.from_turns(session_id, turns)

    def format_report(self, session_id: str) -> str:
        """输出人类可读的执行报告。"""
        turns = self._backend.load_turns(session_id)
        if not turns:
            return f"Session {session_id} 无度量数据"

        summary = SessionMetrics.from_turns(session_id, turns)
        lines: List[str] = []

        # 会话概览
        lines.append(f"═══════════════════════════════════════")
        lines.append(f"Session: {session_id}")
        lines.append(f"═══════════════════════════════════════")
        lines.append(
            f"Turns: {summary.total_turns} | "
            f"Total: {summary.total_duration_ms / 1000:.1f}s | "
            f"Tools: {summary.total_tool_calls} | "
            f"LLM calls: {summary.total_llm_calls}"
        )
        lines.append("───────────────────────────────────────")

        # 每个 turn 的详情
        for t in turns:
            goal_short = (t.goal[:40] + "...") if len(t.goal) > 40 else t.goal
            status_emoji = "✓" if t.status == "done" else "✗"
            lines.append(
                f"Turn {t.turn_id}: {goal_short} → {status_emoji} ({t.duration_ms / 1000:.1f}s)"
            )
            lines.append(
                f"  Steps: {t.successful_steps}/{t.total_steps} ✓ | "
                f"Failed: {t.failed_steps} | "
                f"Tools: {t.total_tool_calls} | "
                f"LLM: {t.total_llm_calls} calls | "
                f"Replan: {t.replan_count}"
            )

            # 工具明细
            if t.tool_summary:
                lines.append("  Tool breakdown:")
                for key, agg in sorted(
                    t.tool_summary.items(), key=lambda x: x[1].call_count, reverse=True
                ):
                    status_str = f"{agg.success_count}✓ {agg.fail_count}✗" if agg.fail_count > 0 else f"{agg.success_count}✓"
                    lines.append(
                        f"    {agg.tool_name}: {agg.call_count} calls ({status_str}) avg {agg.avg_duration_ms:.0f}ms"
                    )

            # Step 详情
            if t.steps:
                lines.append("  Steps detail:")
                for step in t.steps:
                    step_emoji = "✓" if step.status == "done" else "✗"
                    lines.append(
                        f"    {step.step_id} {step_emoji} {step.description[:50]} "
                        f"({step.react_iterations} iterations, {len(step.tool_calls)} tools)"
                    )

            lines.append("───────────────────────────────────────")

        # 工具使用排行
        if summary.tool_usage_ranking:
            lines.append("Tool Usage Ranking:")
            for agg in summary.tool_usage_ranking[:10]:
                status_str = f"{agg.success_count}✓ {agg.fail_count}✗" if agg.fail_count > 0 else f"{agg.success_count}✓"
                lines.append(
                    f"  {agg.tool_name} [{agg.source}]: {agg.call_count} calls ({status_str}) avg {agg.avg_duration_ms:.0f}ms"
                )

        # 来源聚合
        lines.append("───────────────────────────────────────")
        lines.append("Source breakdown:")
        all_sources: dict = {}
        for t in turns:
            for src, count in t.source_summary.items():
                all_sources[src] = all_sources.get(src, 0) + count
        for src, count in sorted(all_sources.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {src}: {count} calls")

        return "\n".join(lines)
