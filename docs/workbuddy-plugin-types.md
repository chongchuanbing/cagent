# WorkBuddy 插件类型分类与实现逆向（v0.1）

> 基于 `~/.workbuddy/plugins/cache/workbuddy-builtin`（34 个内置插件）真实文件结构反推。
> 标注：`[实]`=已读真实文件确认；`[推]`=基于结构推断。配套文档：`workbuddy-plugin-system.md`（整体设计 / 状态分离 / 市场模型）。

---

## 0. 一句话结论

`plugins/cache/workbuddy-builtin` 下 34 个插件并非"一种插件"，而是 **8 类资源形态**。它们的"类型"不是由目录前缀唯一决定的，而是**四层判别模型**叠加的结果：manifest 标记 → `workbuddy.kind` 字段 → 目录约定推断 → `category` 分组。其中 `weixinpay` 这类"看起来不像插件"的实体，其实是**最重的形态**——原生二进制 + MCP server + 编排 skill 的混合包。

---

## 1. 类型判别的四层模型 `[实]`

| 层 | 字段/位置 | 取值 | 作用 |
|---|---|---|---|
| ① manifest 标记 | `<ver>/.workbuddy-plugin/plugin.json` 或 `.codebuddy-plugin/plugin.json` | 二选一 | **产品血统戳**：WorkBuddy 原生提示插件 vs CodeBuddy 派生能力插件，解析逻辑一致但分目录 |
| ② 资源 kind | `plugin.json` 内 `workbuddy.kind` | `builtin-skill` / `builtin-mcp-app` / 空 | **显式资源类型**，仅迁移来的资源（skill、mcp-app）带；其余靠约定推断 |
| ③ 目录约定推断 | 子目录形态 | `skills/` `agents/` `commands/` `hooks/` `.mcp.json` `prebuilds/` | **无 kind 时的类型判定**（tencent-*/sheetagent/weixinpay） |
| ④ 人类分组 | `plugin.json` 内 `category` | `interaction`/`template`/`skill`/`mcp-app`/`支付`/`开发工具` | UI 侧归类，非运行时判定 |

**关键发现**：`plugin.json` 的 `skills`/`mcpServers` 键是**可选声明**。loader 还会做**目录约定发现**（扫描 `skills/*/SKILL.md`、`*.mcp.json`）。`weixinpay` 的 `plugin.json` 仅 6 行元数据，但其 `skills/`、`dist/mcp-server.mjs`、`.mcp.json` 才是真实能力面——这就是"看起来没有 skill 的插件其实有 skill"的原因。

---

## 2. 完整清单与归类 `[实]`

| # | 插件 | 标记 | kind | category | 真实形态 |
|---|---|---|---|---|---|
| 1–4 | interactionmode-{ask,craft,plan,expert} | .workbuddy-plugin | — | interaction | 提示片段库 |
| 5–7 | welcomemode-{code,design,work} | .workbuddy-plugin | — | template | 欢迎模式根 agent |
| 8 | prompt-common | .workbuddy-plugin | — | template | 全局常驻片段 |
| 9–26 | skill-*（18 个） | .codebuddy-plugin | builtin-skill | skill | 能力/知识包 |
| 27 | tencent-docs-plugin | .codebuddy-plugin | — | 开发工具 | 复合文档套件 |
| 28 | tencent-docx | .codebuddy-plugin | — | （无） | 复合文档套件 |
| 29 | tencent-pptx | .codebuddy-plugin | — | （无） | 复合文档套件 |
| 30 | sheetagent | .codebuddy-plugin | — | （无） | MCP server + 编排 skill |
| 31 | mcp-ardot-mcp-app | .codebuddy-plugin | builtin-mcp-app | mcp-app | 内置 MCP app（bundle 托管） |
| 32 | weixinpay | .codebuddy-plugin | — | 支付 | 原生二进制+MCP+skill 包 |

> skill-* 18 个：`ardot-design-{core,router,to-code,poster,slides,ui-design}`（6，ardot 设计套件）、`buddy-multimodal-generation`、`cloudstudio-deploy`、`expert-manager`、`geo-map-compliance-guard`、`library`、`marketplace-skill-installer`、`recommend-connectors`、`recommend-experts`、`skill-creator`、`tencent-docs-routing`、`tencent-local-office-edit`、`wb-finance-skill`。
>
> 注：`tencent-docx`/`tencent-pptx`/`sheetagent`/`weixinpay` 的 `plugin.json` **无 `category` 也无 `kind`**——它们是"复合/原生包"，类型完全由目录约定判定。

---

## 3. 逐类型分析

### 3.1 interactionmode-* —— 提示片段库（控制 agent loop 行为）`[实]`

**用途**：切换"工作模式"时，注入不同的**主循环行为 + 工具白名单 + 结果投递协议**。4 个模式对应 4 套片段。

**证据**：每个插件 = `fragments/` 目录，含 `agent-loop.md` / `interaction.md` / `current-mode.md` / `result-presentation.md` / `tool-use.md`。`interaction.md` 头部是 YAML `tools:` 列表（即该模式的工具策略）：

```
# interactionmode-expert/fragments/interaction.md
tools:
  - Read / Write / Edit / Glob / Grep / Bash / PowerShell
  - TaskCreate / TaskGet / TaskUpdate / TaskList / TaskStop / TaskOutput
  - WebFetch / WebSearch / Skill / Defer(SkillManage) / AskUserQuestion
  - Defer(LSP) / Defer(ImageGen) / Defer(VideoGen)
```

`agent-loop.md` 是 `<agent_loop>` 区块本身（如 expert 模式要求"每次非平凡任务后写 `{{ArtifactDirectoryPath}}/overview.md`"）。

**内部实现推断 `[推]`**：这些片段不是"被加载的模块"，而是被 **Jinja `{% include %}` 拼进系统提示**（见 §3.2 的 `prompt.tpl`）。模式差异 = 拼不同的片段组合。这意味着**agent loop 行为、工具白名单、结果投递协议全部可插拔**——来自插件而非硬编码。

**四模式差异** `[实]`：
- `ask`：纯问答，几乎不调工具（最瘦 tools 列表）。
- `craft`：直接动手（默认模式）。
- `plan`：先规划后执行（`plan` 片段强制"先出计划待确认"）。
- `expert`：工具面最宽（含 `Defer(ImageGen/VideoGen/LSP)`、完整 Task*、AskUserQuestion），且 agent loop 偏"产出 artifact 文档"。

### 3.2 welcomemode-* —— 欢迎模式根 agent（系统提示模板）`[实]`

**用途**：定义进入某个"入口模式"（code/design/work）时的**基础系统提示（persona）**。"欢迎屏"背后是完整的根 agent。

**证据**：`welcomemode-code` 结构：
- `settings.json` → `{"agent":"code"}`（声明这是 code 入口的根 agent）
- `agents/code.md` → 仅一行：`{% include "welcomemode-code/prompt.tpl" %}`
- `prompt.tpl` → **真正的系统提示模板**，含 Jinja 条件分支：

```jinja
This conversation is powered by {% if modelId=="fast-model"… %}Auto{% else %}{{ modelName }}{% endif %}
…Your main goal is to follow the USER's instructions…
{% if workMode=="ask" %}{% include "interactionmode-ask/fragments/interaction.md" %}
{% elif workMode=="plan" %}{% include "interactionmode-plan/fragments/interaction.md" %}
{% elif workMode=="expert" %}{% include "interactionmode-expert/fragments/interaction.md" %}
{% else %}{% include "interactionmode-craft/fragments/interaction.md" %}{% endif %}
…{% if not productFeatures.DisableMultimodalGeneration %}…多模态…{% endif %}
…{% if '中文' in ResponseLanguage %}https://www.workbuddy.cn/docs…{% else %}…workbuddy.ai…{% endif %}…
```

**内部实现推断 `[推]`**：系统提示 = **`welcomemode-<mode>/prompt.tpl` 经 Jinja 渲染**，渲染期注入 `modelId`/`workMode`/`ResponseLanguage`/`productFeatures`/`dataFolderName` 等变量，再 `include` 对应 `interactionmode-<mode>` 片段。即：**welcomemode 是"外壳"，interactionmode 是"内胆"**，二者通过 `workMode` 变量在模板层拼接。`design`/`work` 形态与 `code` 平行（`settings.json` 应为 `{"agent":"design"}`/`{"agent":"work"}`）。

### 3.3 prompt-common —— 全局常驻片段 `[实]`

**用途**：**每个会话都会注入**的共享系统提示片段（如记忆系统、插件推荐指令）。

**证据**：`.workbuddy-plugin` 标记 + `category:template`；含 `fragments/workbuddy-memory-system.md`、`plugin-recommendation.md`；有 `.in_use/` 运行时锁（常驻活跃）。

**内部实现推断 `[推]`**：与 welcomemode 不同，它**不被某个 `prompt.tpl` 显式 include**，而是由运行时在组装系统提示时**无条件拼接**（类似"全局 prepend"）。这是"被动常驻能力"的载体之一。

### 3.4 skill-* —— 能力/知识包（builtin-skill）`[实]`

**用途**：领域工作流与知识包。LLM 读 `SKILL.md` 判定"何时/如何"做某类任务；触发后按需读 `references/` `workflows/`。

**证据**：`workbuddy.kind: builtin-skill` + `legacyResourceName`（= 旧资源名）。物理形态 = `SKILL.md`（YAML frontmatter）+ 可选 `references/`、`workflows/`、`assets/`、`scripts/`。`skill-ardot-slides/SKILL.md` 头：

```yaml
---
name: ardot-slides
description: "Use this skill for Ardot canvas design tasks whose deliverable is a slide deck…"
allowed-tools:
disable-model-invocation: true
user-invocable: false
---
```

**内部实现推断 `[推]`**：
- frontmatter `disable-model-invocation:true` + `user-invocable:false` → **模型自动触发**（靠 `description` 关键词匹配），不靠用户 `/slash` 调用。
- loader 通过**目录约定**发现 `SKILL.md`（多数 skill-* 的 `plugin.json` 未声明 `skills` 键，但 `skills/<name>/SKILL.md` 物理存在）。
- `legacyResourceName` 揭示：这些 skill **前身是"resource"（资源）**，后迁移为插件形态，kind 字段即"迁移标记"。

**关键子发现——skill 依赖注入** `[实]`：`ardot-slides` 的 SKILL.md 明确写"general workflow lives in `ardot-design-core`, **which is injected alongside this skill**"——说明 ardot 6 件套（core/router/to-code/poster/slides/ui-design）是**一个 skill 套件**，`slides` 依赖 `core` 被注入同一上下文。`core` 的绝对路径"在注入本 skill 的同一 prompt 中提供"。这是**插件内 skill 间依赖**机制。

### 3.5 tencent-* —— 复合文档生产力套件（skills+agents+hooks）`[实]`

**用途**：在线腾讯文档 / Word / PPT 三套生产力能力。三者是**同类但不同粒度**的复合插件。

**证据（能力差异）**：
| 插件 | skills | agents | hooks |
|---|---|---|---|
| tencent-docs-plugin | `tencent-docs`, `tencent-saas-docs` | — | — |
| tencent-docx | 10 个（`design-token`/`doc-typeset`/`format-extract`/`html-to-docx`/`humanizer`/`humanizer-zh`/`tdoc-orchestrator`/`underline-toolkit`/…） | `doc-writer`/`doc-formatter`/`doc-converter` | `SessionStart` 注入 |
| tencent-pptx | `tencent-pptx` | — | `PreToolUse` matcher `Skill` |

**内部实现推断 `[推]`**：
- `tencent-docs-plugin` = **路由/伞插件**：把"在线文档"请求分发到个人版(`tencent-docs`)或企业版(`tencent-saas-docs`)。
- `tencent-docx` = **流水线插件**：3 个 agent 是 Stage 角色——`doc-writer`(Stage1 创作 md) → `doc-formatter`(Stage2 排版) → `doc-converter`(Stage3 转 docx)；`tdoc-orchestrator` skill 编排三阶段；`SessionStart` hook 在会话开始注入样式令牌。`design-token`/`doc-typeset` 等是各 Stage 调用的子 skill。
- `tencent-pptx` = **单 skill + PreToolUse 门禁**：用 `PreToolUse` hook 在 `Skill` 工具调用前做拦截/校验（典型用途：注入 PPT 生成约束或防误用）。
- 三者**均无 `kind`/`category`**——纯靠"含 skills+agents+hooks"判定为复合插件。

### 3.6 sheetagent —— 电子表格 agent（MCP server + 编排 skill）`[实]`

**用途**：表格处理 agent。既是**暴露表格工具的 MCP server**，又是**编排这些工具的 skill 包**。

**证据**：`plugin.json` 声明 `skills: ./skills` + `commands: ./commands` + `hooks` + `mcpServers`：

```json
"mcpServers": { "sheetagent": {
  "command": "node",
  "args": ["${CODEBUDDY_PLUGIN_ROOT}/mcp/start.mjs"],
  "env": { "SHEETAGENT_MCP_TRANSPORT":"stdio",
           "SHEET_API_MODE":"local",
           "SHEET_REMOTE_MCP_URL":"https://docs.qq.com/api/v6/sheet/mcp" },
  "defer_loading": true }}
```
`skills/` = `excel-generation` / `excel-handler`。

**内部实现推断 `[推]`**：
- `defer_loading:true` → MCP server **懒启动**（首次用到才起 node 进程）。
- `SHEET_API_MODE:local` + `SHEET_REMOTE_MCP_URL` → 可切**本地模式**（直接读写本地 xlsx）或**代理到腾讯文档远程 MCP**（docs.qq.com 的 sheet MCP）——一种"本地/云端双后端"设计。
- 名字里的 "agent" = 它把"表格处理"做成**自主子 agent**，经 MCP 暴露给主 agent 调用。

### 3.7 mcp-ardot-mcp-app —— 内置 MCP app（bundle 托管）`[实]`

**用途**：Ardot 画布的内置 MCP 应用。与 sheetagent 不同——**server 二进制不在插件缓存里，而在主程序 bundle 内**。

**证据**：`workbuddy.kind: builtin-mcp-app` + `legacyResourceName: ardot-mcp-app` + 关键字段：
```json
"bundleSegments": ["plugins","workbuddy-builtin","mcps","ardot-mcp-app"],
"runtimeSupportSegments": [["plugins","workbuddy-builtin","mcps","ardot-mcp-app",
                             "_workbuddy-runtime","mcp-app-bootstrap.cjs"]],
"loadsCapabilities": false
```

**内部实现推断 `[推]`**：
- `loadsCapabilities:false` → **不从本插件加载 skills/tools**，能力由主程序内嵌的 Ardot 画布提供。
- `bundleSegments` 指向主 bundle 内位置；`_workbuddy-runtime/mcp-app-bootstrap.cjs`（6471 字节）是**引导桩**，负责把主程序里的 Ardot MCP server 接出来。
- 对比 sheetagent（自带 `mcp/start.mjs` 在缓存里跑）= **"插件自带 server"**；ardot = **"server 在 bundle，插件只做注册/引导"**——两种 MCP 托管模型（见 §4.3）。

### 3.8 weixinpay —— 原生二进制 + MCP + skill 的支付包（最重形态）`[实]`

**用途**：微信支付 AI 专属卡集成——让 agent 在对话里完成开通/绑定/支付/反馈。关键词 `claw`（微信侧支付通道）。

**证据（物理结构远超其他插件）**：
```
weixinpay/1.6.107/
├── .codebuddy-plugin/plugin.json   # 仅 6 行元数据（无 skills/mcpServers 声明）
├── .mcp.json                        # MCP server 声明（约定发现入口）
├── skills/weixinpay-{register,pay,feedback}/SKILL.md   # 3 个编排 skill（约定发现）
├── dist/mcp-server.mjs              # MCP server 实现
├── bin/run-node[.cmd]              # node 启动器
├── prebuilds/
│   ├── darwin-arm64/WeChatPayCLI.app/.../wechatpay-cli   # macOS 原生签名 CLI
│   ├── darwin-x64/WeChatPayCLI.app/...
│   └── win32-x64/{wechatpay-cli.exe, QmProtectorDriver.sys, libTencentSM.dll}
├── manifest.qmsig                  # 腾讯签名（完整性/来源校验）
└── .in_use/                        # 51 个 PID 锁文件（高频运行）
```
`.mcp.json`：`{"mcpServers":{"weixinpay":{"command":"${CODEBUDDY_PLUGIN_ROOT}/bin/run-node","args":["…/dist/mcp-server.mjs"],"env":{"WECHATPAY_PAYSIGN_SERVICE_ID":"agentpay"}}}}`
`skills/weixinpay-register/SKILL.md` frontmatter：`version:1.5.110, metadata:{author:weixinpay, category:wallet}`。

**内部实现推断 `[推]`**：
- **三层结构**：3 个 skill = **编排层**（LLM 读 SKILL.md 决定何时开通/支付/反馈）；`mcp-server.mjs` = **工具层**（实际支付/查询 API）；`prebuilds/WeChatPayCLI` = **安全签名层**（国密 `libTencentSM.dll` + Windows 驱动级 `QmProtectorDriver.sys` 做密钥保护）。
- `manifest.qmsig` = 插件包**发布签名**（腾讯格式），安装时校验完整性/来源，防篡改——这是其他纯提示插件没有的"供应链安全"环节。
- `.in_use/` 51 个 PID 锁 = 它是**最活跃的运行组件**（MCP server 长期驻留 + 多次拉起）。
- 这解释了为什么 `plugin.json` 极瘦却功能最强：**真正的类型与能力来自目录约定（`.mcp.json` + `skills/` + `prebuilds/`），`plugin.json` 只是"包身份证"**。

---

## 4. 三个关键机制揭示

### 4.1 资源演进：`legacyResourceName` 透露的"resource → plugin"迁移 `[实]`
所有 `builtin-skill` / `builtin-mcp-app` 插件都带 `legacyResourceName`（值 = 旧资源名，如 `ardot-slides`/`ardot-mcp-app`/ `library`）。这说明 WorkBuddy 早期有"resource"概念（单一能力单元），后统一收敛为"plugin"包格式，`kind`+`legacyResourceName` 是**迁移状态标记**，保证旧资源 ID 仍可解析。

### 4.2 skill 依赖注入（ardot 套件）`[实]`
`ardot-slides` 依赖 `ardot-design-core` 被"injected alongside"。即插件系统支持 **skill 间依赖**：注入某 skill 时，其声明的依赖 skill 也一并注入同一上下文，并共享 `SKILL_ROOT` 绝对路径。这是"skill 套件/家族"的实现基础（ardot 6 件套、tencent-docx 10 件套皆此模式）。

### 4.3 两种 MCP 托管模型 `[实]`
| 模型 | 代表 | server 位置 | loadsCapabilities |
|---|---|---|---|
| 插件自带 server | sheetagent (`mcp/start.mjs`) | 插件缓存内，node 进程 | 通常 true |
| bundle 托管 | mcp-ardot-mcp-app | 主程序 bundle 内 | false（引导桩接出） |

### 4.4 声明 vs 约定发现 `[实]`
`plugin.json` 的 `skills`/`mcpServers` 键**可选**。两类加载并存：
- **声明式**：sheetagent、tencent-docx/pptx、tencent-docs-plugin 显式列 capabilities。
- **约定式**：weixinpay（扫描 `skills/*/SKILL.md` + `.mcp.json`）、所有 skill-*（扫描 `SKILL.md`）。
推断 loader 逻辑：先读声明，再回退到目录扫描——这也是"plugin.json 看起来没 skill 的插件其实有 skill"的根因。

---

## 5. 待验证清单

- `[推]` welcomemode-design / welcomemode-work 的 `settings.json` 是否分别为 `{"agent":"design"}` / `{"agent":"work"}`（与 code 平行）。
- `[推]` prompt-common 是否由运行时"无条件 prepend"而非被某个 tpl include（需读运行时源码确认）。
- `[推]` skill 依赖注入的"依赖声明"写在哪（SKILL.md body 还是某 manifest 字段）——当前只见 slides 在正文描述，未见结构化 `dependencies` 键。
- `[推]` weixinpay 的 `mcp-server.mjs` 如何调用 `prebuilds/WeChatPayCLI`（子进程？IPC？）——需读 mcp-server.mjs 确认。
- `[推]` `manifest.qmsig` 的校验时机（安装时 / 每次加载时）。
- `[推]` `defer_loading` 的触发条件（首次 tool call？首次 skill 触发？）。

---

## 附：类型判别速查

```
插件名前缀/标记                           → 真实形态
─────────────────────────────────────────────────────────
.workbuddy-plugin + fragments/           → 提示片段库（interactionmode / prompt-common）
.workbuddy-plugin + prompt.tpl+agents/   → 欢迎模式根 agent（welcomemode）
.codebuddy-plugin + SKILL.md + kind=     → 能力/知识包（skill-*）
                 builtin-skill
.codebuddy-plugin + skills+agents+hooks  → 复合生产力套件（tencent-*）
.codebuddy-plugin + mcpServers+skills    → MCP 电子表格 agent（sheetagent）
.codebuddy-plugin + kind=builtin-mcp-app → 内置 MCP app，bundle 托管（mcp-ardot）
.codebuddy-plugin + .mcp.json+prebuilds/ → 原生二进制混合包（weixinpay）
```
