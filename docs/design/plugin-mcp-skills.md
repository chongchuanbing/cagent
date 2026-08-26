# MCP 插件与 Skills 插件设计文档

> 版本：v3（2026-08-25）
> 模块位置：`cagent/plugins/`、`cagent/skills/`
> 前置文档：`docs/design/tool-system-v2.md`（插件体系基础设计）
> 参考标准：[Agent Skills 开放标准](https://agentskills.io/specification)（Anthropic 发布，已被 Claude Code / Codex CLI / Cursor / GitHub Copilot 等广泛采用）

## 1. 设计目标

在现有 tools 类型插件（filesystem/shell）基础上，扩展两类能力：

1. **MCP 插件**：连接多个外部 MCP 服务器，将 MCP 工具自动注册到操作树，走 tool_guide + run_tool 统一披露和执行。支持端上配置动态生效。
2. **Skills 插件**：采用业界 **Agent Skills 开放标准**（SKILL.md 格式），支持配置多个 skill 目录，动态扫描加载标准 skill 包。渐进式披露与现有 tool_guide 机制天然对齐。

## 2. Agent Skills 开放标准

### 2.1 核心概念

Agent Skills 是 Anthropic 于 2025 年 10 月推出、12 月正式发布为开放标准的格式。一个 Skill 就是一个包含 `SKILL.md` 文件的目录，通过**渐进式披露**（Progressive Disclosure）让 agent 按需加载技能。

已被 Claude Code、OpenAI Codex CLI、Cursor、GitHub Copilot、Gemini CLI 等广泛采用——**写一次，到处运行**。

### 2.2 目录结构

```
my-skill/
├── SKILL.md          # 必需：元数据（frontmatter）+ 指令（markdown body）
├── scripts/          # 可选：可执行代码（Python/Bash/JS）
├── references/       # 可选：详细文档（按需加载）
├── assets/           # 可选：模板、图片、数据文件
└── ...               # 任意额外文件
```

### 2.3 SKILL.md 格式

```markdown
---
name: code-review
description: 对指定分支做全面代码审查，输出结构化报告。当用户要求代码审查、PR review 或质量检查时使用。
license: Apache-2.0
compatibility: Requires git, access to the repository
metadata:
  author: cagent
  version: "1.0"
allowed-tools: run_tool(shell.git:*) run_tool(shell.cat:*) run_tool(filesystem.apply_patch:*)
---

## 步骤

1. 使用 `git diff` 获取变更内容
2. 分析变更，关注安全、性能、代码规范
3. 生成结构化审查报告
4. 使用 apply_patch 保存报告

## 输入

- target: 审查目标（分支名或 commit 范围）
- focus: 审查重点（security/performance/style/all，默认 all）

## 输出

Markdown 格式的审查报告，包含问题列表、严重级别、修复建议。

## 边界情况

- 空变更集：直接返回"无变更"
- 大量变更：分文件审查，避免上下文溢出
```

### 2.4 Frontmatter 字段

| 字段 | 必需 | 约束 | 说明 |
|---|---|---|---|
| `name` | 是 | 1-64 字符，小写字母+数字+连字符，不能以连字符开头/结尾，不能有连续连字符，**必须与目录名一致** | 技能名 |
| `description` | 是 | 1-1024 字符，描述技能做什么及何时使用 | agent 启动时加载此字段用于匹配 |
| `license` | 否 | 许可证名或许可证文件引用 | — |
| `compatibility` | 否 | 1-500 字符 | 环境要求（产品、系统包、网络等） |
| `metadata` | 否 | string→string map | 额外元数据（author/version 等） |
| `allowed-tools` | 否 | 空格分隔的工具列表 | 预批准工具（实验性） |

### 2.5 渐进式披露（三层）

| 层级 | 加载时机 | 加载内容 | token 成本 |
|---|---|---|---|
| **L1: 发现** | Agent 启动时 | 所有 skill 的 `name` + `description` | ~100 tokens/skill |
| **L2: 激活** | 任务匹配 description 时 | 完整 `SKILL.md` body | < 5000 tokens（建议） |
| **L3: 执行** | 按指令执行 | 按需加载 scripts/references/assets 文件 | 按需 |

这与 cagent 现有的 `tool_guide` 渐进式披露**天然对齐**：
- L1 发现 → skill_guide() 列出所有技能的 name + description
- L2 激活 → skill_guide(skill_name) 返回完整 SKILL.md body
- L3 执行 → run_skill 执行指令，按需加载脚本和引用文件

### 2.6 与我们自定义 skill.yaml 的区别

| 维度 | 旧设计（skill.yaml） | 新设计（SKILL.md 标准） |
|---|---|---|
| 格式 | 自定义 YAML | **业界开放标准**（SKILL.md） |
| 兼容性 | 仅 cagent | Claude Code / Codex / Cursor / Copilot 等通用 |
| 指令 | 步骤声明式（action.type） | **Markdown 自然语言指令**（agent 按指令自行编排） |
| 脚本 | 无 | `scripts/` 目录，agent 按需执行 |
| 引用 | 无 | `references/` 目录，按需加载详细文档 |
| 资源 | 无 | `assets/` 目录，模板/图片/数据 |
| 工具限制 | 无 | `allowed-tools` 字段限制可使用的工具 |
| 编排 | 框架按步骤执行 | **agent 自主编排**（读指令后自行决定调哪些工具） |

**关键转变**：从"框架声明式编排步骤"变为"agent 读自然语言指令后自主编排"。这是 Agent Skills 标准的核心设计——skill 不是程序，而是**给 agent 的专家知识包**。

## 3. cagent 对 Agent Skills 标准的实现

### 3.1 加载机制

```python
class SkillsPluginLoader:
    """扫描多个目录，加载符合 Agent Skills 标准的 skill 包。"""

    def load_all(self, skills_config: SkillsConfig) -> SkillsLoadResult:
        skills = {}
        for dir_path in skills_config.dirs:
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
                    # 后加载的覆盖前面的同名 skill
                    skills[skill.name] = skill
        return SkillsLoadResult(skills=skills)

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
        frontmatter = yaml.safe_load(parts[1]) or {}
        body = parts[2].strip()
        # 校验 name
        name = frontmatter.get("name", "")
        if not name or not self._valid_name(name):
            return None
        # name 必须与目录名一致
        if name != os.path.basename(skill_dir):
            return None
        return SkillDefinition(
            name=name,
            description=frontmatter.get("description", ""),
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
```

### 3.2 SkillDefinition 数据结构

```python
@dataclass
class SkillDefinition:
    """Agent Skills 标准的技能定义。"""
    name: str                           # 技能名（必须与目录名一致）
    description: str                     # 描述（启动时加载，用于匹配任务）
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    allowed_tools: str = ""             # 预批准工具列表
    body: str = ""                      # SKILL.md 的 markdown body（激活时加载）
    skill_dir: str = ""                 # skill 目录路径（用于加载 scripts/references/assets）

    def get_guide_text(self) -> str:
        """L2 激活时返回的完整说明（SKILL.md body）。"""
        return self.body

    def get_metadata_text(self) -> str:
        """L1 发现时返回的简短说明。"""
        return f"{self.name}: {self.description}"

    def resolve_file(self, relative_path: str) -> str:
        """解析 skill 目录内的相对路径文件。"""
        full = os.path.join(self.skill_dir, relative_path)
        # 安全检查：不能越出 skill 目录
        if not os.path.realpath(full).startswith(os.path.realpath(self.skill_dir)):
            raise PermissionError(f"路径越界: {relative_path}")
        return full
```

### 3.3 披露与执行工具

```python
class SkillGuideTool(Tool):
    """查询技能列表和技能说明（渐进式披露）。

    L1: skill_guide() → 列出所有技能的 name + description（~100 tokens/skill）
    L2: skill_guide(skill_name="code-review") → 返回完整 SKILL.md body
    """
    name = "skill_guide"
    description = "查询可用技能及其说明。不传 skill_name 列出所有技能；传入则返回该技能的完整指令。"
    group = "default"

    def run(self, skill_name: str = "") -> ToolResult:
        if not skill_name:
            names = list(self._skills.keys())
            if not names:
                return ToolResult(ok=True, content="（暂无已加载的技能）")
            lines = ["可用技能："]
            for name, skill in self._skills.items():
                lines.append(f"  - {name}: {skill.description}")
            return ToolResult(ok=True, content="\n".join(lines))
        skill = self._skills.get(skill_name)
        if skill is None:
            return ToolResult(ok=False, content="", error=f"技能 '{skill_name}' 不存在")
        # L2: 返回完整 SKILL.md body
        return ToolResult(ok=True, content=skill.get_guide_text())


class RunSkillTool(Tool):
    """执行技能指令。

    模型先调 skill_guide 查看技能指令，然后调本工具执行。
    执行时 agent 读 SKILL.md 的指令，自主编排调用哪些工具。
    框架负责加载 skill 目录内的 scripts/references/assets。
    """
    name = "run_skill"
    description = "执行技能。传入 skill_name 指定技能，params 传入技能参数。"
    group = "default"

    def run(self, skill_name: str, params: dict = None) -> ToolResult:
        skill = self._skills.get(skill_name)
        if skill is None:
            return ToolResult(ok=False, content="", error=f"技能 '{skill_name}' 不存在")
        # 将 skill 的 body + params 注入到当前 ReAct 的上下文中
        # agent 读到指令后自行编排调用 run_tool 等工具
        instruction = skill.get_guide_text()
        # 渲染参数（如果 body 中有 {param} 引用）
        for key, value in (params or {}).items():
            instruction = instruction.replace(f"{{{key}}}", str(value))
        return ToolResult(
            ok=True,
            content=f"技能 '{skill_name}' 已激活，请按以下指令执行：\n\n{instruction}",
        )
```

### 3.4 执行模式的转变

旧设计（skill.yaml 声明式步骤）：
```
框架按 steps 列表逐步执行 → run_tool → llm_analyze → run_tool
```

新设计（SKILL.md 自然语言指令）：
```
模型调 skill_guide(skill_name) → 读到 SKILL.md 指令
模型调 run_skill(skill_name, params) → 指令注入上下文
模型按指令自主编排 → 自行决定调哪些 run_tool / run_command
```

**这是根本性的转变**：skill 不是程序，而是给 agent 的专家知识。agent 读指令后自己决定调什么工具、什么顺序——更像人类读 SOP 后自行操作。

### 3.5 allowed-tools 限制

`SKILL.md` 的 `allowed-tools` 字段限制技能激活后可以使用的工具：

```yaml
allowed-tools: run_tool(shell.git:*) run_tool(shell.cat:*) run_tool(filesystem.apply_patch:*)
```

`RunSkillTool` 在执行时将 `allowed-tools` 注入到当前 ReAct 步骤的工具过滤中，限制技能只能调用声明的工具。这是可选的——不声明则不限制。

## 4. MCP 多服务器配置

### 4.1 agent.yaml 中的 mcp 配置

```yaml
mcp:
  servers:
    github:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: "${GITHUB_TOKEN}"
      enabled: true
      group: mcp_github
      tool_filter:
        exclude: ["delete_repo"]
        rename:
          create_issue: "gh_create_issue"

    postgres:
      transport: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-postgres"]
      env:
        DATABASE_URL: "${DATABASE_URL}"
      enabled: false
      group: mcp_postgres

    figma:
      transport: sse
      url: "http://127.0.0.1:3845/sse"
      enabled: false
      group: mcp_figma

  defaults:
    connect_timeout: 30
    call_timeout: 60
    max_tools_per_server: 30
    auto_detail: true
```

### 4.2 端上动态配置

```bash
# 新增 MCP 服务器
python -m clients.cli config set mcp.servers.linear.transport stdio
python -m clients.cli config set mcp.servers.linear.command npx
python -m clients.cli config set mcp.servers.linear.args '["-y","@modelcontextprotocol/server-linear"]'
python -m clients.cli config set mcp.servers.linear.enabled true

# 启停
python -m clients.cli config set mcp.servers.postgres.enabled true

# CLI 管理命令
python -m clients.cli mcp list          # 列出所有服务器及连接状态
python -m clients.cli mcp test github   # 测试连接 + 列出工具
```

ConfigProvider mtime 轮询检测 yaml 变更 → 下次 run 自动重连。

### 4.3 MCP 加载与执行

MCP 工具自动注册到 OperationTree，走统一的 `tool_guide` + `run_tool`（`mode: mcp`）：

```
模型 → tool_guide(path="mcp_github") → 列出 GitHub MCP 工具
模型 → tool_guide(path="mcp_github.gh_create_issue") → 返回参数格式
模型 → run_tool(path="mcp_github.gh_create_issue", params={...}) → 转发给 MCP 服务器
```

## 5. Skills 多目录配置

### 5.1 agent.yaml

```yaml
skills:
  enabled: true
  dirs:
    - "cagent/skills"              # 内置 skill 目录（随框架发布）
    - ".data/skills"               # 用户本地 skill 目录
    - "~/my-skills"                # 用户自定义/团队共享目录
  max_recursion: 3                 # 技能嵌套深度限制（可选，自然语言模式下很少嵌套）
```

### 5.2 标准目录结构

```
cagent/skills/                     # 内置 skill 目录
  ├── code-review/
  │   ├── SKILL.md                 # 必需
  │   ├── scripts/
  │   │   └── format_report.py     # 可选：格式化报告脚本
  │   ├── references/
  │   │   └── CHECKLIST.md         # 可选：审查清单
  │   └── assets/
  │       └── report_template.md   # 可选：报告模板
  ├── test-gen/
  │   └── SKILL.md
  └── refactor/
      ├── SKILL.md
      └── scripts/
          └── analyze_complexity.py

~/my-skills/                       # 用户自定义目录
  ├── deploy/
  │   └── SKILL.md
  └── data-pipeline/
      ├── SKILL.md
      └── scripts/
          └── transform.py
```

### 5.3 内置 Skill 示例

#### code-review

```markdown
---
name: code-review
description: 对指定分支或 PR 做全面代码审查，输出结构化报告。当用户要求代码审查、review、质量检查时使用。
license: Apache-2.0
compatibility: Requires git and access to the repository
metadata:
  author: cagent
  version: "1.0"
allowed-tools: run_tool(shell.git:*) run_tool(shell.cat:*) run_tool(filesystem.apply_patch:*)
---

## 代码审查流程

### 1. 获取变更
使用 `git diff` 获取变更概况：
```
run_tool(path="shell.git.diff", params={"command": "git diff --stat {target}"})
```

### 2. 获取完整 diff
```
run_tool(path="shell.git.diff", params={"command": "git diff {target}"})
```

### 3. 审查分析
根据 `focus` 参数关注以下方面：
- security: 安全漏洞（注入、XSS、敏感信息泄露）
- performance: 性能问题（N+1 查询、不必要的循环、内存泄漏）
- style: 代码规范（命名、注释、复杂度）
- all: 以上全部

参考 [审查清单](references/CHECKLIST.md) 逐项检查。

### 4. 生成报告
使用 [报告模板](assets/report_template.md) 生成结构化报告，包含：
- 问题列表（含文件、行号、严重级别）
- 修复建议
- 总体评价

### 5. 保存报告
```
run_tool(path="filesystem.apply_patch", params={
  "patch": "*** Begin Patch\n*** Add File: .data/reports/{target}_review.md\n+{report}\n*** End Patch"
})
```

## 边界情况
- 空变更集：直接返回"无变更"
- 大量变更（>500行）：分文件审查，每批不超过 5 个文件
- 二进制文件：跳过
```

#### test-gen

```markdown
---
name: test-gen
description: 为指定源文件生成单元测试。当用户要求生成测试、补测试、提高覆盖率时使用。
license: Apache-2.0
metadata:
  author: cagent
  version: "1.0"
allowed-tools: run_tool(shell.cat:*) run_tool(shell.run_command:*) run_tool(filesystem.apply_patch:*)
---

## 测试生成流程

### 1. 读取源文件
```
run_tool(path="shell.cat", params={"command": "cat -n {target}"})
```

### 2. 分析公共接口
识别所有 public 函数和类，分析参数和返回值。

### 3. 生成测试
为每个 public 函数生成测试用例，覆盖：
- 正常输入
- 边界值（空、零、最大值）
- 异常输入

框架默认使用 pytest，可通过 `framework` 参数指定其他框架。

### 4. 写入测试文件
测试文件路径：`tests/test_{源文件名}`

```
run_tool(path="filesystem.apply_patch", params={
  "patch": "*** Begin Patch\n*** Add File: tests/test_{name}\n+{test_code}\n*** End Patch"
})
```

### 5. 运行测试验证
```
run_tool(path="shell.run_command", params={"command": "python -m pytest tests/test_{name} -v"})
```

如有失败，修复后重新运行。
```

## 6. AgentConfig 扩展

```python
class McpServerConfig(BaseModel):
    transport: str = "stdio"
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None
    enabled: bool = True
    group: Optional[str] = None
    tool_filter: Dict[str, Any] = Field(default_factory=dict)

class McpDefaults(BaseModel):
    connect_timeout: int = 30
    call_timeout: int = 60
    max_tools_per_server: int = 30
    auto_detail: bool = True

class McpConfig(BaseModel):
    servers: Dict[str, McpServerConfig] = Field(default_factory=dict)
    defaults: McpDefaults = Field(default_factory=McpDefaults)

class SkillsConfig(BaseModel):
    enabled: bool = True
    dirs: List[str] = Field(default_factory=lambda: ["cagent/skills"])
    max_recursion: int = 3

class AgentConfig(BaseModel):
    ...
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
```

## 7. 统一加载流程

```
Agent.run()
  │
  ├─ 加载 tools 插件（filesystem/shell）
  │   └─ PluginManager → OperationTree + executors
  │
  ├─ 加载 MCP 服务器
  │   └─ McpClientManager.connect_all(config.mcp)
  │       └─ 每个启用的服务器 → 启动进程 → tools/list → 注册到 OperationTree
  │
  ├─ 加载 Skills
  │   └─ SkillsPluginLoader.load_all(config.skills)
  │       └─ 扫描所有 dirs → 解析 SKILL.md → 注册到 SkillRegistry
  │
  └─ 注册工具到 ToolRegistry
      ├─ calculator（直接执行）
      ├─ tool_guide + run_tool（tools + mcp 统一披露执行）
      └─ skill_guide + run_skill（skills 披露执行）
```

## 8. 端到端工作流

### 8.1 Skill 自然语言执行

```
模型 → skill_guide()
  ← "可用技能：
       code-review: 对指定分支做全面代码审查...
       test-gen: 为指定源文件生成单元测试...
       refactor: 对指定文件做重构建议并执行..."

模型 → skill_guide(skill_name="code-review")
  ← 完整 SKILL.md body：
     "## 代码审查流程
      1. 获取变更: run_tool(shell.git.diff, ...)
      2. 获取完整 diff: run_tool(shell.git.diff, ...)
      3. 审查分析: 参考 references/CHECKLIST.md
      4. 生成报告: 使用 assets/report_template.md
      5. 保存: run_tool(filesystem.apply_patch, ...)"

模型 → run_skill(skill_name="code-review", params={"target": "feature/auth", "focus": "security"})
  ← "技能 'code-review' 已激活，请按以下指令执行：[完整指令]"
  ← 模型按指令自主编排：
     1. run_tool(shell.git.diff, {"command": "git diff --stat feature/auth"}) → 变更概况
     2. run_tool(shell.git.diff, {"command": "git diff feature/auth"}) → 完整 diff
     3. 自主分析（LLM 推理，不需调 llm_analyze 工具）
     4. run_tool(filesystem.apply_patch, {"patch": "..."}) → 保存报告
```

### 8.2 MCP + Skill 混合

```
模型 → skill_guide(skill_name="code-review") → 读审查指令
模型 → run_skill(skill_name="code-review", params={"target": "main...HEAD"})
  → 技能激活，模型按指令执行：
    1. run_tool(shell.git.diff) → 获取 diff
    2. 自主分析
    3. run_tool(mcp_github.gh_create_issue, {"title": "安全问题", "body": "..."}) → 把问题提为 GitHub Issue
    4. run_tool(filesystem.apply_patch) → 保存本地报告
```

## 9. 动态生效

### 9.1 MCP 动态

```
config set mcp.servers.xxx.enabled true/false
  → ConfigProvider mtime 轮询检测变更
  → 下次 Agent.run() 时 McpClientManager.connect_all() 重连
```

### 9.2 Skills 动态

```
新增 skill 目录 / 新增 SKILL.md
  → 下次 Agent.run() 时 SkillsPluginLoader 重新扫描
  → 新 skill 的 name+description 自动出现在 skill_guide 列表中

修改 skills.dirs 配置
  → 下次 run 扫描新目录
```

## 10. 插件目录结构

```
cagent/
  ├── cagent/
  │   ├── plugins/              # tools + mcp 加载
  │   │   ├── manager.py
  │   │   ├── guide.py           # ToolGuideTool + RunToolTool
  │   │   ├── shell_exec.py
  │   │   ├── tree.py
  │   │   ├── mcp_client.py      # 新增：McpClient + McpClientManager
  │   │   ├── mcp_loader.py      # 新增：MCP 服务器加载
  │   │   ├── skill_loader.py    # 新增：SkillsPluginLoader（解析 SKILL.md）
  │   │   ├── skill_executor.py # 新增：SkillGuideTool + RunSkillTool
  │   │   ├── filesystem/        # tools 插件
  │   │   └── shell/             # tools 插件
  │   ├── skills/               # 内置 skill 目录（Agent Skills 标准）
  │   │   ├── code-review/
  │   │   │   ├── SKILL.md
  │   │   │   ├── references/
  │   │   │   │   └── CHECKLIST.md
  │   │   │   └── assets/
  │   │   │       └── report_template.md
  │   │   ├── test-gen/
  │   │   │   └── SKILL.md
  │   │   └── refactor/
  │   │       ├── SKILL.md
  │   │       └── scripts/
  │   │           └── analyze_complexity.py
  │   └── ...
  ├── config/
  │   └── agent.yaml             # 含 mcp.servers + skills.dirs
  └── tests/
      ├── test_mcp.py
      └── test_skills.py
```

## 11. 设计取舍

| 项 | 现状 | 演进方向 |
|---|---|---|
| Skill 格式 | 对齐 Agent Skills 开放标准（SKILL.md） | 跟随标准演进 |
| Skill 编排 | 自然语言指令，agent 自主编排 | 可加 `allowed-tools` 精确限制 |
| Skill 分发 | 本地目录 | 可加 `npx skills add` 安装（skills.sh 生态） |
| MCP 传输 | stdio 优先 | SSE/WebSocket 二期 |
| MCP 发现 | 加载时拉取 | 可加运行时动态发现 |
| 兼容性 | SKILL.md 格式与 Claude Code/Codex/Cursor 通用 | 可直接使用社区 skill 包 |
