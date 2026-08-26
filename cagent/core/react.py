"""ReAct 引擎：单 step 内的 Thought → Action → Observation 循环。"""
import json
from typing import List, Optional

from ..llm.base import LLMClient
from ..schema.action import Action, Observation, Thought
from ..schema.plan import Step, StepResult
from ..schema.message import Message, MessageRole
from ..tools.base import Tool
from ..tools.registry import ToolRegistry
from ..prompts.react_prompt import (
    REACT_UNCONVERGED_PROMPT,
    build_react_system_prompt,
    build_react_user_prompt,
)
from ..config.provider import ConfigProvider
from ..events import EventEmitter, EventType


class ReActEngine:
    """在单个 Step 内部反复推理—调用工具—观察，直到收敛为该步结论。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        config: ConfigProvider = None,
        max_iterations: int = 5,
        emitter: EventEmitter = None,
    ):
        self.llm = llm
        self.tools = tools
        self.config = config
        self.max_iterations = max_iterations
        self.emitter = emitter
        # 最近一次运行的轨迹，供 storage / 调试使用
        self.last_trace: List[dict] = []

    def _select_tools(self, step: Step, goal: Optional[str]) -> List[Tool]:
        """按配置确定本 step 注入的工具集。

        - enabled_groups：分组裁剪（None = 全部）；
        - dynamic：开启时按「步骤描述 + 总目标」检索 Top-K（max_per_step）。
        """
        tcfg = None
        if self.config is not None:
            tcfg = self.config.get_config().tools
        groups = tcfg.enabled_groups if tcfg else None
        if tcfg and tcfg.dynamic:
            query = f"{step.description} {goal or ''}"
            return self.tools.select(query, k=tcfg.max_per_step, groups=groups)
        return self.tools.list(groups)

    def run(self, step: Step, history: Optional[List] = None, goal: Optional[str] = None) -> StepResult:
        """执行单个 step，返回 StepResult。

        history 为前序步骤的执行记录（含结论与工具观察），
        goal 为总目标，两者共同构成跨 step 的共享上下文。

        循环：
        1. 以 system(步骤+工具目录) + user(步骤目标) 调 LLM（带工具声明）
        2. 若 LLM 返回 tool_calls → 执行工具，把 Observation 作为 tool 消息回写，继续
        3. 若 LLM 不再返回 tool_calls（直接给结论）→ 视为收敛，返回成功
        4. 超过 max_iterations 未收敛 → 用 LLM 总结已有轨迹作为阶段性结果；兜底失败才返回失败
        """
        self.last_trace = []
        history = history or []
        tools = self._select_tools(step, goal)
        # 执行只认本 step 选中的工具集：被分组裁剪 / 未被选中的工具
        # 即使被模型幻觉调用，也按不可用处理
        available = {t.name: t for t in tools}
        tool_schemas = [t.schema() for t in tools]
        template = self.config.get_prompt("react_system") if self.config else None
        messages: List[Message] = [
            Message(
                role=MessageRole.SYSTEM,
                content=build_react_system_prompt(step, tools, template=template),
            ),
            Message(role=MessageRole.USER, content=build_react_user_prompt(step, history, goal=goal)),
        ]

        for _ in range(self.max_iterations):
            resp = self.llm.complete_with_tools(messages, tool_schemas)

            if resp.content:
                self.last_trace.append({"thought": resp.content})
                self._emit(EventType.THOUGHT, {"content": resp.content}, step_id=step.id)

            # 无工具调用 → 模型给出最终结论，视为收敛
            if not resp.tool_calls:
                self.last_trace.append({"final": resp.content})
                return StepResult(step_id=step.id, success=True, output=resp.content or "")

            # 把 assistant 的工具调用回写进上下文
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=resp.content or "",
                    tool_calls=resp.tool_calls,
                )
            )

            for tc in resp.tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = available.get(name)
                self._emit(EventType.TOOL_CALL, {"name": name, "args": args}, step_id=step.id)
                if tool is None:
                    obs_content = f"错误：工具 {name} 在本步骤不可用（未启用或未入选）"
                    ok = False
                else:
                    result = tool.run(**args)
                    obs_content = result.content
                    ok = result.ok
                self._emit(
                    EventType.TOOL_RESULT,
                    {"name": name, "content": obs_content, "ok": ok},
                    step_id=step.id,
                )
                obs = Observation(action_ref=name, content=obs_content, ok=ok)
                self.last_trace.append(
                    {"action": Action(tool_name=name, args=args), "observation": obs_content}
                )
                messages.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=obs_content,
                        tool_call_id=tc.get("id"),
                    )
                )

        # 迭代耗尽未收敛：用 LLM 对已有轨迹做一次总结，作为本步骤的尽力结果
        fallback = self._summarize_unconverged(messages)
        if fallback:
            self.last_trace.append({"final": fallback, "unconverged": True})
            return StepResult(step_id=step.id, success=True, output=fallback)

        return StepResult(
            step_id=step.id,
            success=False,
            output="",
            error=f"ReAct 在 {self.max_iterations} 次迭代内未收敛",
        )

    def _summarize_unconverged(self, messages: List[Message]) -> Optional[str]:
        """未收敛兜底：基于已累积的思考与观察让 LLM 给出阶段性结论。

        提示词可用 prompts.react_unconverged 覆盖；调用失败时返回 None，
        由上层退化为失败结果。
        """
        template = self.config.get_prompt("react_unconverged") if self.config else None
        prompt = template or REACT_UNCONVERGED_PROMPT
        try:
            resp = self.llm.complete(
                messages + [Message(role=MessageRole.USER, content=prompt)]
            )
            return (resp.content or "").strip() or None
        except Exception:  # noqa: BLE001 —— 兜底失败不影响主流程
            return None

    def _emit(self, type: EventType, payload: dict, step_id: Optional[str] = None) -> None:
        """发布事件（未配置 emitter 时静默跳过）。"""
        if self.emitter is not None:
            self.emitter.emit(type, payload=payload, step_id=step_id)
