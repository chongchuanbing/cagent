"""CLI 事件展示策略：把标准化 Agent 事件渲染为带颜色的终端输出。

- 工具调用 / 工具结果 / 思考 / 错误等使用不同颜色与前缀，一眼可区分；
- 非 TTY（管道、重定向、CI）自动降级为无颜色纯文本；
- 遵守 NO_COLOR 环境变量约定。
"""
import os
import sys
from typing import Callable, Optional

from cagent.events import AgentEvent, EventType

# ---------- ANSI 颜色 ----------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GRAY = "\033[90m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_MAGENTA = "\033[35m"


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class Display:
    """事件 → 终端行的渲染器。颜色可开关，便于测试与降级。"""

    def __init__(self, color: Optional[bool] = None, stream=None):
        self.color = _supports_color() if color is None else color
        self.stream = stream or sys.stdout

    # ---------- 内部工具 ----------
    def _paint(self, text: str, *codes: str) -> str:
        if not self.color:
            return text
        return "".join(codes) + text + _RESET

    def _write(self, text: str) -> None:
        self.stream.write(text + "\n")

    # ---------- 各事件渲染 ----------
    def render(self, event: AgentEvent) -> None:
        handler: Optional[Callable[[AgentEvent], None]] = {
            EventType.PLAN_CREATED: self._on_plan_created,
            EventType.STEP_STARTED: self._on_step_started,
            EventType.THOUGHT: self._on_thought,
            EventType.TOOL_CALL: self._on_tool_call,
            EventType.TOOL_RESULT: self._on_tool_result,
            EventType.STEP_FINISHED: self._on_step_finished,
            EventType.REPLANNED: self._on_replanned,
            EventType.USER_INPUT_REQUEST: self._on_user_input,
            EventType.MEMORY_RECALLED: self._on_memory_recalled,
            EventType.MEMORY_PROMOTED: self._on_memory_promoted,
            EventType.FINAL_ANSWER: self._on_final_answer,
            EventType.ERROR: self._on_error,
        }.get(event.type)
        if handler is not None:
            handler(event)

    def _on_plan_created(self, event: AgentEvent) -> None:
        plan = event.payload.get("plan", {})
        steps = plan.get("steps", [])
        self._write(
            self._paint(f"═══ 计划已生成（{len(steps)} 步）═══", _BOLD, _CYAN)
        )
        for i, s in enumerate(steps, 1):
            dep = f"（依赖: {', '.join(s['depends_on']) or '无'}）" if s.get("depends_on") else ""
            self._write(f"  {i}. [{s['id']}] {s['description']} {dep}")

    def _on_step_started(self, event: AgentEvent) -> None:
        step = event.payload.get("step", {})
        self._write(
            self._paint(f"▶ 步骤 [{step.get('id', '?')}] {step.get('description', '')}", _BOLD)
        )

    def _on_thought(self, event: AgentEvent) -> None:
        content = event.payload.get("content", "")
        for line in content.splitlines():
            self._write(self._paint(f"  ↳ 思考: {line}", _GRAY))

    def _on_tool_call(self, event: AgentEvent) -> None:
        name = event.payload.get("name", "?")
        args = event.payload.get("args", {})
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        self._write(
            self._paint(f"  → 调用工具: {name}({args_str})", _YELLOW, _BOLD)
        )

    def _on_tool_result(self, event: AgentEvent) -> None:
        name = event.payload.get("name", "?")
        content = event.payload.get("content", "")
        ok = event.payload.get("ok", True)
        mark = "结果" if ok else "失败"
        color = _GREEN if ok else _RED
        self._write(self._paint(f"  ↳ [{name}] {mark}: {content}", color))

    def _on_step_finished(self, event: AgentEvent) -> None:
        step = event.payload.get("step", {})
        result = event.payload.get("result", {})
        ok = result.get("success", False)
        mark = "完成" if ok else "失败"
        color = _GREEN if ok else _RED
        out = result.get("output") or result.get("error") or ""
        self._write(
            self._paint(f"  {'✓' if ok else '✗'} 步骤 [{step.get('id', '?')}] {mark}: {out}", color)
        )

    def _on_replanned(self, event: AgentEvent) -> None:
        plan = event.payload.get("plan", {})
        steps = plan.get("steps", [])
        self._write(
            self._paint(f"↻ 计划已修订（{len(steps)} 步）", _MAGENTA, _BOLD)
        )
        for i, s in enumerate(steps, 1):
            self._write(f"  {i}. [{s['id']}] {s['description']}")

    def _on_user_input(self, event: AgentEvent) -> None:
        question = event.payload.get("question", "")
        self._write(self._paint(f"? {question}", _CYAN, _BOLD))

    def _on_memory_recalled(self, event: AgentEvent) -> None:
        contents = event.payload.get("contents", [])
        self._write(
            self._paint(f"◆ 召回 {len(contents)} 条长期记忆", _MAGENTA)
        )
        for c in contents:
            self._write(self._paint(f"    · {c}", _MAGENTA))

    def _on_memory_promoted(self, event: AgentEvent) -> None:
        content = event.payload.get("content", "")
        pinned = event.payload.get("pinned", False)
        mark = "已记住（用户指定）" if pinned else "晋升为长期记忆"
        self._write(self._paint(f"◆ {mark}: {content}", _MAGENTA, _BOLD))

    def _on_final_answer(self, event: AgentEvent) -> None:
        answer = event.payload.get("answer", "")
        self._write(self._paint("═══ 最终答案 ═══", _BOLD, _GREEN))
        for line in answer.splitlines():
            self._write(self._paint(line, _GREEN))

    def _on_error(self, event: AgentEvent) -> None:
        msg = event.payload.get("message", "")
        self._write(self._paint(f"✗ 错误: {msg}", _RED, _BOLD))


def create_display_handler(
    color: Optional[bool] = None, stream=None
) -> Callable[[AgentEvent], None]:
    """创建事件订阅回调：收到事件即渲染到终端。"""
    display = Display(color=color, stream=stream)
    return display.render
