"""Skills 加载器：扫描多个目录，解析 SKILL.md（Agent Skills 开放标准）。

SKILL.md 格式：
  - YAML frontmatter（name + description + 可选字段）
  - Markdown body（自然语言指令）

目录结构：
  skill-name/
    ├── SKILL.md          # 必需
    ├── scripts/          # 可选
    ├── references/       # 可选
    └── assets/           # 可选

渐进式披露三层：
  L1: 启动时加载 name + description（~100 tokens/skill）
  L2: 激活时加载完整 SKILL.md body
  L3: 执行时按需加载 scripts/references/assets
"""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class SkillDefinition:
    """Agent Skills 标准的技能定义。"""

    name: str                           # 技能名（必须与目录名一致）
    description: str                    # 描述（启动时加载，用于匹配任务）
    license: Optional[str] = None       # 许可证
    compatibility: Optional[str] = None  # 环境要求
    metadata: Dict[str, str] = field(default_factory=dict)  # 额外元数据
    allowed_tools: str = ""             # 预批准工具列表（空格分隔）
    body: str = ""                      # SKILL.md 的 markdown body（激活时加载）
    skill_dir: str = ""                 # skill 目录路径

    def get_metadata_text(self) -> str:
        """L1 发现时返回的简短说明。"""
        return f"{self.name}: {self.description}"

    def get_guide_text(self) -> str:
        """L2 激活时返回的完整说明（SKILL.md body）。"""
        return self.body

    def resolve_file(self, relative_path: str, path_space=None) -> str:
        """解析 skill 目录内的相对路径文件（L3 按需加载）。

        传入 `path_space` 时走 PathSpace 统一沙箱（assert_safe）做越界/符号链接
        校验；未传则保留原 skill_dir 子树包含校验作为回退。两种路径都先校验存在性。
        """
        full = os.path.join(self.skill_dir, relative_path)
        if path_space is not None:
            real_full = path_space.assert_safe(full, mode="read")
        else:
            real_full = os.path.realpath(full)
            real_dir = os.path.realpath(self.skill_dir)
            if not real_full.startswith(real_dir + os.sep) and real_full != real_dir:
                raise PermissionError(f"路径越界: {relative_path}")
        if not os.path.exists(real_full):
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        return real_full


class SkillsPluginLoader:
    """扫描多个目录，加载符合 Agent Skills 标准的 skill 包。"""

    def load_all(self, skills_config: Any) -> Dict[str, SkillDefinition]:
        """扫描所有配置的目录，加载标准 skill 包。

        返回 {skill_name: SkillDefinition} 映射。
        后加载的目录中的同名 skill 覆盖前面的。
        """
        skills: Dict[str, SkillDefinition] = {}
        dirs = self._get_dirs(skills_config)
        for dir_path in dirs:
            dir_path = os.path.expanduser(dir_path)
            if not os.path.isabs(dir_path):
                dir_path = os.path.abspath(dir_path)
            if not os.path.isdir(dir_path):
                continue
            for name in sorted(os.listdir(dir_path)):
                skill_dir = os.path.join(dir_path, name)
                if not os.path.isdir(skill_dir):
                    continue
                skill_md = os.path.join(skill_dir, "SKILL.md")
                if not os.path.exists(skill_md):
                    continue
                skill = self._parse_skill_md(skill_md, skill_dir)
                if skill is not None:
                    skills[skill.name] = skill
        return skills

    def _get_dirs(self, config: Any) -> List[str]:
        if isinstance(config, dict):
            return config.get("dirs", ["cagent/skills"])
        dirs = getattr(config, "dirs", None)
        if dirs is None:
            return ["cagent/skills"]
        return list(dirs)

    def _parse_skill_md(self, path: str, skill_dir: str) -> Optional[SkillDefinition]:
        """解析 SKILL.md：YAML frontmatter + markdown body。"""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # 解析 frontmatter
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        try:
            frontmatter = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return None
        body = parts[2].strip()

        name = frontmatter.get("name", "")
        if not name or not self._valid_name(name):
            return None
        # name 必须与目录名一致
        if name != os.path.basename(skill_dir):
            return None

        description = frontmatter.get("description", "")

        return SkillDefinition(
            name=name,
            description=description,
            license=frontmatter.get("license"),
            compatibility=frontmatter.get("compatibility"),
            metadata=frontmatter.get("metadata", {}),
            allowed_tools=frontmatter.get("allowed-tools", ""),
            body=body,
            skill_dir=skill_dir,
        )

    def _valid_name(self, name: str) -> bool:
        """校验 name 格式：小写字母+数字+连字符，不以连字符开头/结尾，无连续连字符。"""
        if not name or len(name) > 64:
            return False
        if name.startswith("-") or name.endswith("-"):
            return False
        if "--" in name:
            return False
        return all(c.islower() or c.isdigit() or c == "-" for c in name)
