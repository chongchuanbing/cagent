"""Doom loop detection: prevent infinite repetition of identical tool calls.

对标 OpenCode 的 doom loop 检测机制，通过滑动窗口追踪工具调用历史，
检测 LLM 是否陷入重复执行相同操作的模式。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import json

from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DoomLoopDetector:
    """Detect repetitive tool call patterns to prevent infinite loops.
    
    策略（对标 OpenCode）：
    - 滑动窗口记录最近 window_size 次工具调用
    - 连续 >= threshold 次相同调用（同工具 + 同参数）→ 触发告警
    - 告警方式：返回提示文本，框架注入到下一轮 Observation 中
    
    Attributes:
        window_size: 滑动窗口大小（默认 5）
        threshold: 连续相同调用次数阈值（默认 3）
    """
    window_size: int = 5
    threshold: int = 3
    _history: List[Tuple[str, str]] = field(default_factory=list)  # (tool_name, args_json)
    _alerted: bool = False
    
    def record(self, tool_name: str, args: dict) -> Optional[str]:
        """Record a tool call, return warning text if doom loop detected.
        
        Args:
            tool_name: 工具名称
            args: 工具参数（会被 JSON 序列化用于比较）
        
        Returns:
            告警文本（检测到死循环）或 None（无告警）
        """
        try:
            args_json = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            # 参数不可序列化时，用 str 兜底
            args_json = str(args)
        
        key = (tool_name, args_json)
        self._history.append(key)
        
        # 保持窗口大小
        if len(self._history) > self.window_size:
            self._history.pop(0)
        
        # 检查最近 threshold 次是否完全相同
        if len(self._history) >= self.threshold:
            recent = self._history[-self.threshold:]
            if all(k == recent[0] for k in recent):
                if not self._alerted:
                    self._alerted = True
                    logger.warning(f"死循环检测触发: {tool_name} 连续 {self.threshold} 次相同调用")
                    return (
                        f"⚠️ 死循环检测：工具 {tool_name} 已连续调用 {self.threshold} 次且参数完全相同。"
                        "请换一种方法或调整参数，不要重复相同的操作。"
                    )
                # 已经告警过，不重复
                return None
        
        # 参数变了 → 重置告警状态
        self._alerted = False
        return None
    
    def reset(self) -> None:
        """Reset detector state (called at the start of each ReAct step)."""
        self._history.clear()
        self._alerted = False
