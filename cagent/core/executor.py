"""Executor：Plan-Executor 的 Executor 侧，按依赖调度 Step。"""
from typing import List, Optional

from ..schema.plan import Plan, Step, StepResult, StepStatus
from .react import ReActEngine


class Executor:
    """管理 step 调度、状态与结果回写。"""

    def __init__(self, react_engine: ReActEngine):
        self.react_engine = react_engine

    def next_step(self, plan: Plan) -> Optional[Step]:
        """选出下一个可执行 step（依赖均已 DONE）。"""
        done = {s.id for s in plan.steps if s.status == StepStatus.DONE}
        for step in plan.steps:
            if step.status != StepStatus.PENDING:
                continue
            if all(dep in done for dep in step.depends_on):
                return step
        return None

    def execute_step(self, step: Step, history: Optional[List] = None, goal: Optional[str] = None) -> StepResult:
        """把 step 交给 ReActEngine 执行并拿回结果。

        history / goal 构成跨 step 的共享上下文，由 AgentLoop 维护并传入。
        """
        step.status = StepStatus.RUNNING
        result = self.react_engine.run(step, history=history, goal=goal)
        step.status = StepStatus.DONE if result.success else StepStatus.FAILED
        return result

    def update(self, plan: Plan, step_result: StepResult) -> None:
        """将结果回写到对应 step（状态由 execute_step 单点管理）。"""
        for step in plan.steps:
            if step.id == step_result.step_id:
                step.result = step_result
                return
