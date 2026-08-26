"""计划（Plan）与步骤（Step）定义，对应 Plan-Executor 范式。"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field
from datetime import datetime


class StepStatus(str, Enum):
    """Step 状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepResult(BaseModel):
    """单个 Step 的执行结果，由 ReActEngine 产出并回写。"""

    step_id: str
    success: bool
    output: str = ""
    error: Optional[str] = None


class Step(BaseModel):
    """计划中的一个可执行步骤。"""

    id: str
    description: str
    # 依赖的 step id 列表；只有被依赖项完成后才可被调度
    depends_on: List[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: Optional[StepResult] = None


class Plan(BaseModel):
    """整体计划，由 Planner 生成、Executor 维护状态。

    支持动态调整：每步执行后可增量增删改未执行的 steps（version 递增）。
    """

    goal: str
    steps: List[Step] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    version: int = 1

    def pending_steps(self) -> List[Step]:
        """返回所有未执行的步骤（PENDING 状态）。"""
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    def apply_adjust(self, add: List[dict], remove: List[str], modify: List[dict]) -> bool:
        """应用增量调整，返回是否有实际变更。

        - add：追加新 step 到末尾
        - remove：移除指定 PENDING step（已完成的不可移除）
        - modify：修改 PENDING step 的 description/depends_on
        """
        changed = False
        # remove：只移除 PENDING 状态的
        if remove:
            before = len(self.steps)
            self.steps = [
                s for s in self.steps
                if s.id not in remove or s.status != StepStatus.PENDING
            ]
            if len(self.steps) != before:
                changed = True

        # modify：只改 PENDING 状态的
        if modify:
            for m in modify:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id", "")
                for s in self.steps:
                    if s.id == mid and s.status == StepStatus.PENDING:
                        if "description" in m:
                            s.description = m["description"]
                            changed = True
                        if "depends_on" in m:
                            s.depends_on = m["depends_on"]
                            changed = True

        # add：追加到末尾
        if add:
            for a in add:
                if not isinstance(a, dict):
                    continue
                step = Step(
                    id=a.get("id", f"s{len(self.steps)+1}"),
                    description=a.get("description", ""),
                    depends_on=a.get("depends_on", []),
                )
                self.steps.append(step)
                changed = True

        if changed:
            self.version += 1
        return changed
