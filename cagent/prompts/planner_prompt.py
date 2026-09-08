"""Planner 提示词模板。"""
from typing import List

from ..schema.plan import Plan, Step, StepStatus
from ..schema.action import Observation

PLANNER_SYSTEM_PROMPT = """你是一个任务规划器。请将用户目标分解为有序、可独立执行的步骤。
仅输出 JSON，不要包含任何额外解释或 markdown 代码块标记，格式如下：
{
  "steps": [
    {"id": "s1", "description": "步骤描述", "depends_on": []},
    {"id": "s2", "description": "依赖 s1 的后续步骤", "depends_on": ["s1"]}
  ]
}
规则：
- id 简洁且全局唯一；depends_on 列出该步骤所依赖的前置步骤 id（无依赖则为空数组）。
- 步骤粒度适中：一个步骤对应一次 ReAct 执行单元。
- 不要编造无法完成的步骤。
"""

REPLAN_SYSTEM_PROMPT = """你是一个任务规划器。请根据下方失败信息修订计划。
仅输出 JSON，格式与初始计划一致（steps: [{id, description, depends_on}]）。
可重试失败步骤、跳过或重排步骤；保持 id 稳定以便对照。
"""

ADJUST_SYSTEM_PROMPT = """你是一个任务规划器。一个步骤已成功执行，请评估是否需要调整未执行的步骤。

基于刚完成步骤的结果和观察，判断后续步骤是否需要：
- 新增步骤（发现了需要处理的新情况）
- 移除步骤（某些步骤已不再需要）
- 修改步骤（描述或依赖需要调整）

规则：
- 已完成（done）和正在执行（running）的步骤不可移除或修改。
- 新增步骤的 id 不能与已有步骤冲突。
- 若无需调整，输出 {"action": "no_change"}。

仅输出 JSON：
{
  "action": "adjust",
  "add": [{"id": "s4", "description": "新增步骤描述", "depends_on": ["s2"]}],
  "remove": ["s3"],
  "modify": [{"id": "s2", "description": "修改后的描述", "depends_on": ["s1"]}]
}
"""


def build_planner_prompt(goal: str, context: List) -> str:
    """构造让 LLM 产出 Plan 的用户提示。"""
    ctx = "\n".join(str(c) for c in context) if context else "（无）"
    return f"目标：{goal}\n已知上下文：\n{ctx}\n请输出计划 JSON。"


def build_replan_prompt(plan: Plan, failed_step: Step, obs: Observation) -> str:
    """构造重规划提示：给出原计划、失败步骤与观察，要求输出修订后的计划 JSON。"""
    steps_desc = "\n".join(f"- {s.id}: {s.description} [{s.status.value}]" for s in plan.steps)
    return (
        f"原目标：{plan.goal}\n"
        f"当前计划：\n{steps_desc}\n"
        f"失败步骤：{failed_step.id} - {failed_step.description}\n"
        f"失败观察：{obs.content}\n"
        f"请修订计划（可重试、跳过或重排步骤），输出新的计划 JSON。"
    )


def build_adjust_prompt(
    plan: Plan,
    completed_step: Step,
    history: List[dict],
) -> str:
    """构造增量调整提示：给出当前 plan 状态 + 本步结果，要求输出调整 JSON。

    只传入已完成步骤的交付结论，中间过程不进上下文。
    """
    steps_desc = "\n".join(
        f"- {s.id}: {s.description} [{s.status.value}]"
        + (f" → {s.result.output[:200]}" if s.result and s.result.output else "")
        for s in plan.steps
    )
    # 已完成步骤的结论摘要
    step_outputs = []
    for h in history:
        if "step_id" in h:
            mark = "成功" if h.get("success", True) else "失败"
            step_outputs.append(f"  · [{mark}] {h.get('output', '')[:150]}")
    outputs_text = "\n".join(step_outputs[-5:]) if step_outputs else "（无）"

    return (
        f"当前目标：{plan.goal}\n"
        f"当前计划状态：\n{steps_desc}\n\n"
        f"刚完成的步骤：{completed_step.id} - {completed_step.description}\n"
        f"步骤结果：{completed_step.result.output[:300] if completed_step.result else '（无）'}\n"
        f"已完成步骤结论：\n{outputs_text}\n\n"
        f"请评估是否需要调整未执行（pending）的步骤。"
    )
