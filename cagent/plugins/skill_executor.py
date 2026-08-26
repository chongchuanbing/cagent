"""技能披露与执行工具：SkillGuideTool + RunSkillTool。

渐进式披露三层：
  L1: skill_guide() → 列出所有技能的 name + description
  L2: skill_guide(skill_name) → 返回完整 SKILL.md body
  L3: run_skill(skill_name, params) → 注入指令到上下文，agent 自主编排
"""
from typing import Dict, Optional

from ..tools.base import Tool, ToolResult
from .skill_loader import SkillDefinition


class SkillGuideTool(Tool):
    """查询技能列表和技能说明（渐进式披露入口）。"""

    name = "skill_guide"
    description = (
        "查询可用技能及其说明。"
        "不传 skill_name 时列出所有技能；传入 skill_name 返回该技能的完整指令。"
        "先调本工具查看技能指令，再调 run_skill 执行。"
    )
    group = "default"

    def __init__(self, skills: Dict[str, SkillDefinition]):
        self._skills = skills

    def set_skills(self, skills: dict) -> None:
        self._skills = skills

    def run(self, skill_name: str = "") -> ToolResult:
        if not skill_name or not skill_name.strip():
            if not self._skills:
                return ToolResult(ok=True, content="（暂无已加载的技能）")
            lines = ["可用技能："]
            for name, skill in self._skills.items():
                lines.append(f"  - {name}: {skill.description}")
            lines.append("\n调用 skill_guide(skill_name='技能名') 查看完整指令。")
            return ToolResult(ok=True, content="\n".join(lines))

        skill = self._skills.get(skill_name)
        if skill is None:
            return ToolResult(
                ok=False,
                content="",
                error=f"技能 '{skill_name}' 不存在。调用 skill_guide() 查看可用技能列表。",
            )
        # L2: 返回完整 SKILL.md body
        content = skill.get_guide_text()
        if not content:
            return ToolResult(
                ok=True,
                content=f"技能 '{skill_name}' 无详细指令。描述：{skill.description}",
            )
        return ToolResult(ok=True, content=content)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "技能名。不传则列出所有可用技能。",
                        }
                    },
                    "required": [],
                },
            },
        }


class RunSkillTool(Tool):
    """执行技能指令。

    模型先调 skill_guide 查看技能指令，然后调本工具执行。
    执行时将 SKILL.md 的指令注入上下文，agent 按指令自主编排调用其他工具。
    """

    name = "run_skill"
    description = (
        "执行技能。传入 skill_name 指定技能，params 传入技能参数。"
        "技能指令会注入到上下文中，请按指令自主编排调用其他工具（如 run_tool）。"
    )
    group = "default"

    def __init__(self, skills: Dict[str, SkillDefinition]):
        self._skills = skills
        self._max_recursion = 3

    def set_skills(self, skills: dict) -> None:
        self._skills = skills

    def set_max_recursion(self, n: int) -> None:
        self._max_recursion = n

    def run(self, skill_name: str, params: Optional[dict] = None) -> ToolResult:
        skill = self._skills.get(skill_name)
        if skill is None:
            return ToolResult(
                ok=False,
                content="",
                error=f"技能 '{skill_name}' 不存在。调用 skill_guide() 查看可用技能。",
            )

        # 获取完整指令
        instruction = skill.get_guide_text()
        if not instruction:
            return ToolResult(
                ok=False,
                content="",
                error=f"技能 '{skill_name}' 无执行指令",
            )

        # 渲染参数（SKILL.md body 中的 {param} 引用）
        for key, value in (params or {}).items():
            instruction = instruction.replace(f"{{{key}}}", str(value))

        # 如果有 allowed-tools，在指令前提示可用工具范围
        prefix = ""
        if skill.allowed_tools:
            prefix = f"[该技能预批准工具: {skill.allowed_tools}]\n\n"

        return ToolResult(
            ok=True,
            content=(
                f"技能 '{skill_name}' 已激活。请按以下指令执行：\n\n"
                f"{prefix}{instruction}"
            ),
        )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "要执行的技能名",
                        },
                        "params": {
                            "type": "object",
                            "description": "技能参数（参见 skill_guide 返回的指令说明）",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
        }
