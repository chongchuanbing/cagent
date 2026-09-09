"""Planner：Plan-Executor 的 Plan 侧，负责生成与修订计划。"""
import json
import re
from typing import List, Optional

from ..llm.base import LLMClient
from ..schema.plan import Plan, Step, StepStatus, StepResult
from ..schema.action import Observation
from ..schema.message import Message, MessageRole
from ..prompts.planner_prompt import (
    build_planner_prompt,
    build_replan_prompt,
    build_adjust_prompt,
    PLANNER_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
    ADJUST_SYSTEM_PROMPT,
)
from ..config.provider import ConfigProvider
from ..utils.logging import get_logger

logger = get_logger(__name__)


def _extract_json_block(text: str) -> dict:
    """从 LLM 文本中提取 JSON 对象（兼容 ```json 代码块与多余文本）。"""
    text = text.strip()
    # 优先用正则提取 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # 回退：用 raw_decode 精确提取第一个 JSON 对象
        logger.debug(f"JSON 解析失败，尝试 raw_decode: {e}")
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start != -1:
            try:
                obj, _ = decoder.raw_decode(text[start:])
                return obj
            except json.JSONDecodeError as e2:
                logger.debug(f"raw_decode 也失败: {e2}")
        raise


class Planner:
    """根据目标（与已有上下文）产出结构化 Plan；失败时修订计划。"""

    def __init__(self, llm: LLMClient, config: Optional[ConfigProvider] = None):
        self.llm = llm
        self.config = config

    def _build_plan(self, raw_text: str, goal: str) -> Plan:
        data = _extract_json_block(raw_text)
        
        # 类型检查：确保 data 是字典
        if not isinstance(data, dict):
            # 如果 data 是字符串，尝试解析
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                    if not isinstance(data, dict):
                        raise ValueError(f"计划 JSON 格式错误：期望字典，实际得到 {type(data).__name__}")
                except json.JSONDecodeError:
                    raise ValueError(f"计划 JSON 格式错误：无法解析字符串为 JSON")
            else:
                raise ValueError(f"计划 JSON 格式错误：期望字典，实际得到 {type(data).__name__}")
        
        steps_raw = data.get("steps", [])
        
        # 类型检查：确保 steps 是列表
        if not isinstance(steps_raw, list):
            raise ValueError(f"计划 JSON 格式错误：'steps' 字段应为列表，实际得到 {type(steps_raw).__name__}")
        
        steps: List[Step] = []
        used_ids: set = set()
        for idx, s in enumerate(steps_raw, start=1):
            # 类型检查：确保每个 step 是字典
            if not isinstance(s, dict):
                # 如果是字符串，尝试解析
                if isinstance(s, str):
                    try:
                        s = json.loads(s)
                        if not isinstance(s, dict):
                            raise ValueError(f"计划 JSON 格式错误：第 {idx} 个步骤应为字典，实际得到 {type(s).__name__}: {s}")
                    except json.JSONDecodeError:
                        raise ValueError(f"计划 JSON 格式错误：第 {idx} 个步骤无法解析为 JSON 对象")
                else:
                    raise ValueError(f"计划 JSON 格式错误：第 {idx} 个步骤应为字典，实际得到 {type(s).__name__}: {s}")
            
            sid = s.get("id") or f"s{idx}"
            if sid in used_ids:
                sid = f"{sid}_{idx}"
            used_ids.add(sid)
            steps.append(
                Step(
                    id=sid,
                    description=s.get("description", ""),
                    depends_on=s.get("depends_on", []) or [],
                    status=StepStatus.PENDING,
                )
            )
        return Plan(goal=goal, steps=steps)

    def plan(self, goal: str, context: Optional[List] = None) -> Plan:
        """生成初始 Plan（LLM → JSON → Plan）。"""
        logger.info(f"开始生成初始计划 goal={goal[:50]}... context_count={len(context or [])}")
        system = (
            self.config.get_prompt("planner_system", default=PLANNER_SYSTEM_PROMPT)
            if self.config
            else PLANNER_SYSTEM_PROMPT
        )
        resp = self.llm.complete(
            [
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=build_planner_prompt(goal, context or [])),
            ]
        )
        plan = self._build_plan(resp.content, goal)
        logger.info(f"初始计划生成完成 steps={len(plan.steps)}")
        return plan

    def replan(self, plan: Plan, failed_step: Step, obs: Observation) -> Plan:
        """结合失败观察让 LLM 重新规划。"""
        logger.info(f"开始重规划 failed_step={failed_step.id} error={obs.content[:100]}")
        system = (
            self.config.get_prompt("replan_system", default=REPLAN_SYSTEM_PROMPT)
            if self.config
            else REPLAN_SYSTEM_PROMPT
        )
        resp = self.llm.complete(
            [
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=build_replan_prompt(plan, failed_step, obs)),
            ]
        )
        try:
            new_plan = self._build_plan(resp.content, plan.goal)
            logger.info(f"重规划完成 new_steps={len(new_plan.steps)}")
            return new_plan
        except (json.JSONDecodeError, ValueError):
            # 解析失败则保留原计划但标记失败步骤，避免循环崩溃
            logger.warning("重规划 JSON 解析失败，保留原计划")
            return plan

    def should_replan(self, plan: Plan, step_result: StepResult) -> bool:
        """决定是否需要重规划（供 AgentLoop 调用）。"""
        return not step_result.success

    def adjust(
        self,
        plan: Plan,
        completed_step: Step,
        history: List[dict],
    ) -> Optional[Plan]:
        """每步成功后增量调整未执行的 steps。

        让 LLM 基于已完成步骤的结果评估是否需要增删改后续 PENDING steps。
        返回调整后的 Plan（version 递增）；无变更时返回 None。
        """
        # 无 PENDING steps 时不需要调整
        if not plan.pending_steps():
            logger.debug("adjust: 无 pending steps，跳过调整")
            return None

        logger.info(f"adjust: 开始评估 step {completed_step.id} 完成后的计划调整")
        system = (
            self.config.get_prompt("adjust_system", default=ADJUST_SYSTEM_PROMPT)
            if self.config
            else ADJUST_SYSTEM_PROMPT
        )
        try:
            resp = self.llm.complete([
                Message(role=MessageRole.SYSTEM, content=system),
                Message(role=MessageRole.USER, content=build_adjust_prompt(plan, completed_step, history)),
            ])
            data = _extract_json_block(resp.content)
        except (json.JSONDecodeError, ValueError):
            # LLM 输出解析失败，跳过调整
            logger.warning("adjust: LLM 输出解析失败，跳过调整")
            return None

        if not isinstance(data, dict):
            logger.warning("adjust: LLM 返回非 dict 类型")
            return None
        if data.get("action") == "no_change":
            logger.debug("adjust: 无需调整")
            return None

        add = data.get("add", [])
        remove = data.get("remove", [])
        modify = data.get("modify", [])
        # 防御性类型检查
        if not isinstance(add, list):
            add = []
        if not isinstance(remove, list):
            remove = []
        if not isinstance(modify, list):
            modify = []
        if not add and not remove and not modify:
            logger.debug("adjust: 无实际调整操作")
            return None

        old_version = plan.version
        changed = plan.apply_adjust(add=add, remove=remove, modify=modify)
        if changed:
            logger.info(f"adjust: 计划已调整，version {old_version} → {plan.version}")
            return plan
        logger.debug("adjust: 应用调整后无实际变更")
        return None
