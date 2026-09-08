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
from ..plugins.capability import CapabilityProbe
from .failure_ledger import FailureLedger


class ReActEngine:
    """在单个 Step 内部反复推理—调用工具—观察，直到收敛为该步结论。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        config: ConfigProvider = None,
        max_iterations: int = 5,
        emitter: EventEmitter = None,
        capability: Optional[CapabilityProbe] = None,
        failure_ledger: Optional[FailureLedger] = None,
        path_space=None,
    ):
        self.llm = llm
        self.tools = tools
        self.config = config
        self.max_iterations = max_iterations
        self.emitter = emitter
        self.capability = capability
        self.path_space = path_space
        # 最近一次运行的轨迹，供 storage / 调试使用
        self.last_trace: List[dict] = []
        # L5: 失败账本（跨 step 持续记录，在 Agent 级别共享）
        self._failure_ledger = failure_ledger or FailureLedger()

    def _select_tools(self, step: Step, goal: Optional[str]) -> List[Tool]:
        """按配置确定本 step 注入的工具集。

        - enabled_groups：分组裁剪（None = 全部）；
        - dynamic：开启时按「步骤描述 + 总目标」检索 Top-K（max_per_step）；
        - capability：L3 工具面裁剪，基于环境探测结果过滤不可用工具。
        """
        tcfg = None
        if self.config is not None:
            tcfg = self.config.get_config().tools
        groups = tcfg.enabled_groups if tcfg else None
        if tcfg and tcfg.dynamic:
            query = f"{step.description} {goal or ''}"
            tools = self.tools.select(query, k=tcfg.max_per_step, groups=groups)
        else:
            tools = self.tools.list(groups)

        # L3: 基于环境探测结果裁剪不可用工具
        if self.capability is not None:
            tools = self._filter_by_capability(tools)

        return tools

    def _filter_by_capability(self, tools: List[Tool]) -> List[Tool]:
        """L3: 过滤掉当前环境不可用的工具。

        工具若声明了 requires，检查其依赖的 CLI 是否可用。
        """
        filtered = []
        for tool in tools:
            requires = getattr(tool, "requires", None)
            if requires is None:
                filtered.append(tool)
                continue

            # requires 格式: {"cli": "fd", "fallback": "find"}
            cli_name = requires.get("cli")
            if cli_name and self.capability.is_available(cli_name):
                filtered.append(tool)
            elif cli_name is None:
                # 无 cli 依赖声明，保留
                filtered.append(tool)
            else:
                # CLI 不可用，跳过
                continue

        return filtered

    def run(self, step: Step, history: Optional[List] = None, goal: Optional[str] = None) -> StepResult:
        """执行单个 step，返回 StepResult。

        history 为前序步骤的执行记录（含结论与工具观察），
        goal 为总目标，两者共同构成跨 step 的共享上下文。

        循环：
        1. 以 system(步骤+工具目录+环境事实) + user(步骤目标) 调 LLM（带工具声明）
        2. 若 LLM 返回 tool_calls → 执行工具，把 Observation 作为 tool 消息回写，继续
        3. 若 LLM 不再返回 tool_calls（直接给结论）→ 视为收敛，返回成功
        4. 超过 max_iterations 未收敛 → 用 LLM 总结已有轨迹作为阶段性说明，
           但返回 success=False（未收敛 ≠ 完成），由上层标 FAILED 并触发 replan
        """
        self.last_trace = []
        # L5: 失败账本在 Agent 级别共享、run 开头 reset 一次（agent.py 调用）。
        # 跨 step 累计按设计：账本按 (工具, 错误类型) 分层——软错误（SANDBOX/TIMEOUT 等）
        # 只记数提示、不进总熔断，避免早前步骤的用法类失败连坐后续步骤的工具通道。
        history = history or []
        tools = self._select_tools(step, goal)
        # 执行只认本 step 选中的工具集：被分组裁剪 / 未被选中的工具
        # 即使被模型幻觉调用，也按不可用处理
        available = {t.name: t for t in tools}
        tool_schemas = [t.schema() for t in tools]
        template = self.config.get_prompt("react_system") if self.config else None

        # L3: 构建环境事实字符串，注入 system prompt
        env_facts = self.capability.to_env_facts() if self.capability else None
        # 路径速查卡：告知 LLM 可引用的逻辑 scheme，禁止猜绝对路径
        path_card = self.path_space.path_card() if self.path_space is not None else None

        messages: List[Message] = [
            Message(
                role=MessageRole.SYSTEM,
                content=build_react_system_prompt(
                    step, tools, template=template, env_facts=env_facts, path_card=path_card
                ),
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

                # L5: 检查是否应该继续重试该工具
                if self._failure_ledger and not self._failure_ledger.should_retry(name):
                    obs_content = f"工具 {name} 已达到最大重试次数（{self._failure_ledger.max_retries_per_tool}），停止重试。\n建议：{self._failure_ledger.get_retry_hint(name)}"
                    ok = False
                    self._emit(EventType.TOOL_RESULT, {"name": name, "content": obs_content, "ok": ok}, step_id=step.id)
                    # 熔断回合同样落 trace，保证 UI 与 trace.jsonl 一致（可审计）
                    self.last_trace.append(
                        {"action": {"tool_name": name, "args": args}, "observation": obs_content, "ok": False}
                    )
                    messages.append(
                        Message(
                            role=MessageRole.TOOL,
                            content=obs_content,
                            tool_call_id=tc.get("id"),
                        )
                    )
                    continue

                # L5: 检查是否应该触发熔断
                if self._failure_ledger and self._failure_ledger.is_circuit_broken():
                    obs_content = f"已达到总失败次数上限，触发熔断。\n{self._failure_ledger.get_circuit_break_hint()}"
                    ok = False
                    self._emit(EventType.TOOL_RESULT, {"name": name, "content": obs_content, "ok": ok}, step_id=step.id)
                    self.last_trace.append(
                        {"action": {"tool_name": name, "args": args}, "observation": obs_content, "ok": False}
                    )
                    messages.append(
                        Message(
                            role=MessageRole.TOOL,
                            content=obs_content,
                            tool_call_id=tc.get("id"),
                        )
                    )
                    # 触发收敛，提前结束：熔断说明当前策略已不可行，
                    # 兜底总结只是阶段性说明，不代表步骤成功——返回失败触发上层 replan
                    fallback = self._summarize_unconverged(messages)
                    if fallback:
                        self.last_trace.append({"final": fallback, "unconverged": True})
                        return StepResult(step_id=step.id, success=False, output=fallback, error="触发熔断，当前策略不可行")
                    return StepResult(step_id=step.id, success=False, output="", error="触发熔断")

                tool = available.get(name)
                self._emit(EventType.TOOL_CALL, {"name": name, "args": args}, step_id=step.id)
                if tool is None:
                    obs_content = f"错误：工具 {name} 在本步骤不可用（未启用或未入选）"
                    ok = False
                    error_kind = "TOOL_UNAVAILABLE"
                else:
                    result = tool.run(**args)
                    ok = result.ok
                    error_kind = getattr(result, "error_kind", None)
                    if not ok:
                        # 失败详情回传：content（附加上下文）+ error（原因）+ hint（可执行建议）。
                        # 若只回传空 content，模型看不到真实失败原因，只能盲目换猜测重试。
                        parts = []
                        if result.content:
                            parts.append(result.content)
                        if getattr(result, "error", None):
                            parts.append(f"错误：{result.error}")
                        if getattr(result, "hint", None):
                            parts.append(f"建议：{result.hint}")
                        obs_content = "\n".join(parts) or "（工具执行失败，未返回错误信息）"
                    else:
                        obs_content = result.content

                # L5: 记录失败
                if not ok and self._failure_ledger:
                    self._failure_ledger.record_failure(name, error_kind)
                    # 在观察结果中附加提示
                    if not obs_content.endswith("\n"):
                        obs_content += "\n"
                    obs_content += f"💡 {self._failure_ledger.get_retry_hint(name)}"

                self._emit(
                    EventType.TOOL_RESULT,
                    {"name": name, "content": obs_content, "ok": ok},
                    step_id=step.id,
                )
                obs = Observation(action_ref=name, content=obs_content, ok=ok)
                self.last_trace.append(
                    # action 存纯 dict：trace.jsonl 经 json.dumps(default=str) 落盘，
                    # pydantic 对象会被序列化成字符串，导致 resume 重建历史时无法 .get
                    {
                        "action": {"tool_name": name, "args": args},
                        "observation": obs_content,
                    }
                )
                messages.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=obs_content,
                        tool_call_id=tc.get("id"),
                    )
                )

        # 迭代耗尽未收敛：用 LLM 对已有轨迹做一次总结，作为阶段性说明。
        # 注意：总结只是"做到哪了"的说明，不代表步骤完成——返回 success=False，
        # 由上层标 FAILED 并触发 replan。否则会出现"模型自述尚未完成却标 done"的假成功。
        fallback = self._summarize_unconverged(messages)
        if fallback:
            self.last_trace.append({"final": fallback, "unconverged": True})
            return StepResult(
                step_id=step.id,
                success=False,
                output=fallback,
                error=f"ReAct 在 {self.max_iterations} 次迭代内未收敛（阶段性总结见 output）",
            )

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
