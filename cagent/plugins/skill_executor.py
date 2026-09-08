"""技能披露与执行工具：SkillGuideTool + RunSkillTool。

渐进式披露三层：
  L1: skill_guide() → 列出所有技能的 name + description
  L2: skill_guide(skill_name) → 返回完整 SKILL.md body
  L3: run_skill(skill_name, params) → 注入指令到上下文，agent 自主编排
"""
import difflib
import os
import re
from typing import Dict, Optional

from ..runtime.paths import URI_RE
from ..tools.base import Tool, ToolResult
from .skill_loader import SkillDefinition

# SKILL.md 内 markdown 链接：`[文本](目标)`
_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _rewrite_skill_links(body: str, skill_name: str) -> str:
    """把 SKILL.md 内的相对引用重写为逻辑 `skills://<name>/...` 形式。

    模型拿到重写后的链接后，用 read_skill_file(skill_name, relative_path) 读取，
    不再凭先验猜绝对路径（如 .workbuddy/skills/）。

    不处理：http(s):// 远程引用、已有 scheme:// 的绝对引用、以 / 开头的绝对路径。
    裸 `./skills/tdoc-orchestrator/SKILL.md` 这类相对当前文件目录的链接，去掉 `./`
    前缀即相对 skill 根，正好对应 skills://<name>/skills/tdoc-orchestrator/SKILL.md。
    """

    def repl(m: "re.Match") -> str:
        target = m.group(1).strip()
        if target.startswith("http://") or target.startswith("https://"):
            return m.group(0)
        if URI_RE.match(target):  # 已带 scheme（skills:// / data:// / file:// ...）
            return m.group(0)
        if target.startswith("/"):  # 绝对路径，框架不接管
            return m.group(0)
        rel = target[2:] if target.startswith("./") else target
        return f"](skills://{skill_name}/{rel})"

    return _LINK_RE.sub(repl, body)


_SKILL_PATH_NOTE = (
    "【路径说明】本技能正文内所有相对引用（references/、./skills/ 等）均相对技能根目录"
    " `skills://{name}/`。读取这些文件请调用 "
    "`read_skill_file(skill_name='{name}', relative_path='<相对路径>')`，"
    "切勿猜测绝对路径（例如不要拼接 .workbuddy/skills/ 之类平台目录）。\n"
    "【先看后读】read_skill_file 的 relative_path 传目录路径可列出该目录的文件清单。"
    "当指令中出现通配或模糊引用（如 `references/component-*.md`），或不确定确切文件名时，"
    "必须先列目录确认实际文件名，再读取对应文件；"
    "严禁按命名模式自行拼造未经确认存在的文件名。"
)


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
        # L2: 返回完整 SKILL.md body（链接重写为逻辑 skills:// 形式，附路径说明）
        content = skill.get_guide_text()
        if not content:
            return ToolResult(
                ok=True,
                content=f"技能 '{skill_name}' 无详细指令。描述：{skill.description}",
            )
        content = _rewrite_skill_links(content, skill.name)
        note = _SKILL_PATH_NOTE.format(name=skill.name)
        return ToolResult(ok=True, content=f"{note}\n\n{content}")

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

        # 链接重写为逻辑 skills:// 形式，附路径说明，避免模型瞎猜绝对路径
        instruction = _rewrite_skill_links(instruction, skill_name)
        note = _SKILL_PATH_NOTE.format(name=skill_name)

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
                f"{prefix}{note}\n\n{instruction}"
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


class ReadSkillFileTool(Tool):
    """安全读取技能目录内的参考文件（references/scripts/嵌套 skills/ 等）。

    与 shell `cat` 不同：路径相对 skill 根解析，且经 SkillDefinition.resolve_file
    做越界 + 存在性校验，模型无需知道技能在磁盘上的真实位置，也不会误读其它目录。

    额外提供「先看」能力：
      - relative_path 传目录（以 / 结尾、空、或指向已存在目录）→ 返回该目录文件清单
      - 请求的文件不存在 → 错误中附带技能目录内相近文件候选，供模型一次自纠
    """

    name = "read_skill_file"
    description = (
        "读取技能目录内的参考文件，或列出目录内容（references/、scripts/、嵌套 skills/ 等）。"
        "relative_path 传文件路径返回文件内容；传目录路径（如 references/）返回该目录的文件清单。"
        "当指令中出现通配或模糊引用（如 component-*.md 中对应文档），或不确定确切文件名时，"
        "先传目录列出清单再选择，切勿拼造或猜测文件名，也不要自行拼接绝对路径。"
    )
    group = "default"

    def __init__(self, skills: Dict[str, SkillDefinition], path_space=None):
        self._skills = skills
        self._path_space = path_space

    def set_skills(self, skills: dict) -> None:
        self._skills = skills

    def run(self, skill_name: str, relative_path: str = "") -> ToolResult:
        skill = self._skills.get(skill_name)
        if skill is None:
            return ToolResult(
                ok=False,
                content="",
                error=f"技能 '{skill_name}' 不存在。调用 skill_guide() 查看可用技能。",
            )
        rel = (relative_path or "").strip()
        # 目录清单入口：空 / ./ / / 或以 / 结尾
        if rel in ("", ".", "/"):
            return self._list_dir(skill, "")
        if rel.endswith("/"):
            return self._list_dir(skill, rel.rstrip("/"))
        try:
            abs_path = skill.resolve_file(rel, path_space=self._path_space)
        except PermissionError as e:
            return ToolResult(
                ok=False,
                content="",
                error=str(e),
                error_kind="SANDBOX",
                hint="relative_path 相对技能根目录，例如 references/create-from-scratch.md；"
                "嵌套技能用类似 skills/tdoc-orchestrator/SKILL.md。",
            )
        except FileNotFoundError:
            return self._not_found(skill, rel)
        if os.path.isdir(abs_path):
            return self._list_dir(skill, rel)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return ToolResult(ok=True, content=f.read())
        except OSError as e:  # noqa: BLE001
            return ToolResult(ok=False, content="", error=f"读取失败: {e}")

    def _list_dir(self, skill: SkillDefinition, rel_dir: str) -> ToolResult:
        """列出技能目录（或其子目录）的文件清单，供模型「先看」再读。"""
        try:
            abs_dir = skill.resolve_file(rel_dir, path_space=self._path_space)
        except (PermissionError, FileNotFoundError) as e:
            return ToolResult(
                ok=False,
                content="",
                error=str(e),
                error_kind="SANDBOX",
                hint=f"目录不存在时，可传 relative_path='' 列出技能根目录清单。",
            )
        if not os.path.isdir(abs_dir):
            return ToolResult(
                ok=False,
                content="",
                error=f"路径 '{rel_dir}' 不是目录。读取文件请直接传文件路径。",
            )
        prefix = f"{rel_dir}/" if rel_dir else ""
        lines = [f"技能 '{skill.name}' 目录 '{rel_dir or '.'}' 下的内容："]
        for entry in sorted(os.listdir(abs_dir)):
            if entry.startswith("."):
                continue
            if os.path.isdir(os.path.join(abs_dir, entry)):
                lines.append(f"  {entry}/（目录，可继续列出）")
            else:
                lines.append(f"  {entry}")
        lines.append(
            f"读取文件：read_skill_file(skill_name='{skill.name}', "
            f"relative_path='{prefix}<文件名>')。只应使用上面真实存在的文件名。"
        )
        return ToolResult(ok=True, content="\n".join(lines))

    def _not_found(self, skill: SkillDefinition, rel: str) -> ToolResult:
        """文件不存在：返回错误 + 相近文件候选，让模型一次自纠而非瞎试。"""
        candidates: list = []
        for root, _dirs, files in os.walk(skill.skill_dir):
            for f in files:
                if f.startswith("."):
                    continue
                full = os.path.join(root, f)
                candidates.append(os.path.relpath(full, skill.skill_dir).replace(os.sep, "/"))
        close = difflib.get_close_matches(rel, candidates, n=5, cutoff=0.4)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        lines = [f"文件不存在: {rel}（请勿猜测文件名）"]
        if close:
            lines.append("技能目录下相近的文件（请改用其中真实存在的路径）：")
            lines.extend(f"  - {c}" for c in close)
        lines.append(
            f"可先传目录路径列出清单确认：read_skill_file(skill_name='{skill.name}', "
            f"relative_path='{parent + '/' if parent else ''}')"
        )
        return ToolResult(
            ok=False,
            content="",
            error="\n".join(lines),
            error_kind="SANDBOX",
            hint="不要继续猜测其他文件名；先列目录或改用上面返回的候选路径。",
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
                            "description": "技能名（与 skill_guide 返回一致）",
                        },
                        "relative_path": {
                            "type": "string",
                            "description": (
                                "相对技能根目录的路径。传文件路径（如 "
                                "references/create-from-scratch.md）返回内容；"
                                "传目录路径（如 references/）返回该目录的文件清单。"
                            ),
                        },
                    },
                    "required": ["skill_name", "relative_path"],
                },
            },
        }
