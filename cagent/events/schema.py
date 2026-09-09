"""Agent 运行事件的数据契约。

所有事件统一为 `AgentEvent`（pydantic 模型），可按 SSE 标准序列化，
供 CLI / Web / 桌面端订阅展示。事件类型见 `EventType`。
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Agent 运行过程中的标准化事件类型。"""

    PLAN_CREATED = "plan_created"        # 计划生成
    STEP_STARTED = "step_started"        # 步骤开始
    THOUGHT = "thought"                  # LLM 推理过程
    TOOL_CALL = "tool_call"              # 调用工具
    TOOL_RESULT = "tool_result"          # 工具返回
    STEP_FINISHED = "step_finished"      # 步骤结束（成功/失败）
    REPLANNED = "replanned"              # 计划被修订（失败后全量重规划）
    PLAN_ADJUSTED = "plan_adjusted"      # 计划被增量调整（成功后增删改未执行步骤）
    USER_INPUT_REQUEST = "user_input_request"  # 请求用户补充信息
    MEMORY_RECALLED = "memory_recalled"        # 长期记忆召回注入上下文
    MEMORY_PROMOTED = "memory_promoted"        # 记忆晋升为长期记忆
    WORKSPACE_WRITE = "workspace_write"        # 会话向空间（workspace://）写入文件（审计）
    FINAL_ANSWER = "final_answer"        # 最终答案
    ERROR = "error"                      # 运行错误
    DOOM_LOOP_DETECTED = "doom_loop_detected"  # 死循环检测触发
    TURN_STARTED = "turn_started"        # 一次 run() 调用开始
    TURN_FINISHED = "turn_finished"      # 一次 run() 调用结束
    REAL_TOOL_EXEC = "real_tool_exec"    # 真实工具执行（run_tool 代理内部，区分 builtin/plugin/mcp/shell）


class AgentEvent(BaseModel):
    """一条标准化 Agent 事件。

    通用字段：
    - type：事件类型（同时作为 SSE 的 event 字段）
    - seq：全局递增序号，保证顺序可重放
    - ts：事件发生时间
    - session_id / step_id：事件归属上下文
    - payload：类型相关的负载（不同事件结构不同）
    """

    type: EventType
    seq: int = 0
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = None
    step_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
