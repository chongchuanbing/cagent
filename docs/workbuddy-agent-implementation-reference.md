# WorkBuddy Agent 工具完整实现逆向参考（总纲）

> **文档目的**：把 `~/.workbuddy` 数据目录考古的全部发现，按"一个 Agent 工具应有的核心模块"重新归纳，形成一份**可照着实现**的参考手册。
> **使用方式**：每个模块统一三段式——
> 1. **数据证据**：在本地哪些文件、读到过什么真实内容（路径 + 样本）；
> 2. **实现逻辑**：据此反推出的设计（标注 `[实]` 已实锤 / `[推]` 推断）；
> 3. **待确认 TODO**：仍不清楚、实现前必须验证的点。
> **目标读者**：要亲手造一个类 WorkBuddy 的 Agent 工具的人。文末 §8 给出实现路线图。
>
> 状态：v1.0（由 4 份分册 `workbuddy-internals-investigation` / `workbuddy-plugin-system` / `workbuddy-plugin-types` / `workbuddy-plugin-config-hidden` 归并而成）。
> 证据等级：`[实]` 已读真实文件内容；`[推]` 依结构推断。

---

## 1. 总体形态与架构

### 1.1 形态判断 `[实]`
WorkBuddy = **Electron 桌面客户端 + 本地后端进程 + 云端 LLM** 的 Agent 应用：
- 云端模型做"脑"（`models.json` 指向 dashscope 等远端 API，推理在云）；
- 本地桌面进程做"身体"（shell 执行、文件读写、沙箱、产物管理在本地 `binaries/` 自带 Python/Node 运行时）；
- 后端域名 `copilot.tencent.com`（解码 `local_storage/` 的 gzip+base64 entry 得到）。

### 1.2 一次运行的端到端串联 `[实]`
```
用户输入（Electron renderer, app/）
 └─ 本地后端转发云端 copilot.tencent.com
      └─ 云端 LLM 推理（models.json 选模型）
           ├─ 多 Agent 编排（§2.2）：顶层 agent=cli 调 Agent 元工具
           │    └─ 派生 subagents/agent-<id>.jsonl（Explore 只读勘察）
           │         └─ 子报告经 function_call_result(output) 上卷
           └─ 工具调用经 MCP 网关（connectors/ .mcp.json → 127.0.0.1:49157）
                ├─ 内置工具：Bash/Read/Write/Edit（沙箱子进程，logs/sandbox 留痕）
                ├─ 技能：skills/ 装载 SKILL.md（按 plugin.json 声明/约定发现）
                ├─ 外部能力：mcp_tools → 网关 → 远端 streamable-http server
                ├─ 工具大返回 spill：tool-results/
                ├─ 产物：blobs/(哈希) + artifact-index/(diff)
                ├─ 文件改：file-history/(@vN 全量) + workspace/(虚拟FS预览)
                └─ 安全层：audit-log/(command-safety 哈希链)
           └─ 事件写回 projects/<工程>/<session>.jsonl（事件溯源）
           └─ 每步计时：traces/<会话>/trace_*.json（span 树）
      └─ 结构化状态：workbuddy.db（automations/usage/…）+ 迁移脚本
      └─ 跨端：edge-sync-mapping*.db（会话↔云端；文件→SMH；图→COS）
```

### 1.3 目录→模块映射（体积截至 2026-09-11，总 ≈ 4.3 GB）
| 目录 | 大小 | 归属模块 |
|---|---|---|
| `projects/` | 141M | 业务·项目 + Loop 事件流（§2.1/§4.1） |
| `traces/` | 951M | 基础·可观测（§3.2） |
| `app/session/` | 796M | 其他·桌面进程（§5.2） |
| `shell-snapshots/` | 496M | 核心·外部工具·沙箱引导（§2.3.2） |
| `binaries/` | 372M | 运行时不归属模块（自带 Python/Node） |
| `blobs/` | 282M | 基础·存储·产物（§3.3） |
| `skills/` | 148M | 核心·外部工具·Skill（§2.3.3） |
| `projects/.../subagents/` | - | 核心·多Agent（§2.2） |
| `plugins/` | 129M | 核心·外部工具·插件系统（§2.3.4–2.3.6） |
| `logs/` | 998M | 核心·沙箱日志 / 其他·桌面日志（§2.3.2/§5.2） |
| `connectors/` | 56K | 核心·外部工具·MCP（§2.3.1） |
| `artifact-index/` | 3.1M | 基础·存储·产物索引（§3.3） |
| `file-history/` | 11M | 基础·存储·文件版本（§3.3） |
| `workspace/` | 76K | 基础·存储·虚拟FS（§3.3） |
| `memory/` | 16K | 核心·记忆（§2.4） |
| `experts/` | 4K | 核心·记忆·角色（§2.4） |
| `tasks/` | 1.1M | 基础·存储·自动化快照（§3.3） |
| `workbuddy.db` | ? | 基础·存储·SQLite（§3.3） |
| `sessions/` | 36K | 业务·会话（§4.2） |
| `local_storage/` | 1.0M | 基础·存储 + 其他·桌面（§3.3/§5.2） |
| `audit-log/` | 4.3M | 其他·安全审计（§5.3） |
| `edge-sync-mapping*.db` | ~1.5M | 其他·跨端同步（§5.1） |
| `.workbuddy-sqlite-migrations/` | 4.5K | 基础·存储·迁移（§3.3） |

---

## 2. 核心模块 · Agent 框架

### 2.1 Loop（控制流 / 主循环）`projects/`

**数据证据 `[实]`**
- 路径：`projects/<工程路径编码>/<session-id>.jsonl`（命名即 session id）。
- 格式：OpenAI **Responses API 风格**追加事件流，每行对象字段：`type`（`message`/`reasoning`/`function_call`/`function_call_result`）、`role`、`content`、`timestamp`（ms）、`id`、`sessionId`、`cwd`、`parentId`、`providerData`。
- 元信息 `providerData` 携带 `agent`（如 `cli`）、`isSubAgent:true`（来自子 agent）、`reasoning`（委派/工具使用独白）。
- `tool-results/` 子目录：大对象原始返回（如 `chatcmpl-tool-*.txt`）spill 出去，主 jsonl 只留引用。

**实现逻辑 `[实]`**
- **事件溯源（event-sourced）**：一次运行 = 一个追加 jsonl。天然支持回放、会话恢复、断点续跑、审计。代价是单工程体积增长（`projects/` 141M）。
- 主循环 = **plan-executor + ReAct**：`reasoning` 事件即 ReAct 的 Thought；`function_call`/`function_call_result` 即 Action/Observation；外层由 planner 出计划、executor 按步调度（证据：jsonl 中 reasoning 显式写"先规划/用 Agent 工具探索"）。
- `parentId`/`sessionId`/`cwd` 把父事件、子 agent 会话、trace span 串成同一层级。

**待确认 TODO**
- [ ] planner 与 executor 的边界在代码中如何划分？jsonl 里是否能区分"计划事件"与"执行事件"的类型标记？
- [ ] `reasoning` 是否全程落盘（含 API 不返回的内部思考）？还是仅部分暴露？
- [ ] 回放/续跑的恢复点粒度（按 event 还是按 step）？

### 2.2 多 Agent 层级编排（Loop 的扩展）`projects/<session>/subagents/`

**数据证据 `[实]`**
- 路径：会话目录下 `subagents/agent-<id>.jsonl`，每个是**完整子会话**（与父同格式：reasoning/function_call/function_call_result/message）。
- 派发：父 jsonl 中 `function_call` 事件 `name:"Agent"`，`providerData.reasoning` 写"用 Agent 工具 + Explore 子 agent 探索结构"；输入带 `subagent_type:"Explore"` + 任务文本。
- 子会话首事件 = 任务（`user`）全文；尾事件 = `assistant status:completed` 报告（`output_text`）。
- 合并：子报告作为父侧 `function_call_result`（`name:"Agent"`, `status:completed`）的 `output` 上卷（实测 `output:{type:"text",text:"已完成对…的探索…"}`）。
- 实测同一会话派生 3 组 Explore 子 agent（并行勘察不同切片）。

**实现逻辑 `[实]`**
- **manager/worker 模式**：顶层 agent 用 `Agent` 元工具把子任务派给专用子 agent 类型（Explore/Plan 等），子 agent 在隔离会话独立跑（可带自己工具集/只读约束），结果以工具返回值上卷。
- 子会话独立落盘 → 支持单独回放/调试/计费。

**待确认 TODO**
- [ ] 允许哪些 `subagent_type`（Explore/Plan/…）与并行度控制规则？`[推]`
- [ ] 子 agent 是否可再嵌套子 agent（多层 manager/worker）？`[推]`
- [ ] `Agent` 元工具本身在哪声明（是否算一个内置 tool / 一个 skill）？
- [ ] 子 agent 的工具集如何继承/裁剪（只读约束如何实现）？

### 2.3 外部工具系统（SKILL / MCP / 沙箱）

#### 2.3.1 MCP 网关与连接器 `connectors/` + `.mcp.json`
**数据证据 `[实]`**
- `.mcp.json` 聚合层：
  ```json
  {"mcpServers":{"connector-proxy":{
    "type":"http","url":"http://127.0.0.1:49157/mcp",
    "description":"Aggregated proxy containing MCP servers: agent-mail"}}}
  ```
- `connectors/default/mcp.json`：几十个远端 MCP server，多为 `type:streamable-http`（txmcp.tdx.com.cn、stockbuddy.qq.com、docs.qq.com/openapi/mcp…），几乎全 `disabled:true`。
- `connector-states.v3.json`：aes-256-gcm 信封加密。`encryption` 块含 `scheme`/`kdf`/`salt`/`userIdCheck`/`keyCheck`/`createdAt`；`accountIdentityKey = userId||enterprise`（命名空间，非密钥）；迁移标记 `mcpSecurityMigrated`/`cSideAutoBoundDefaultDisabledMigrated`/`headerOverridesBearerStripped`/`staleManagedAuthHeadersPurged`；`version:3`。

**实现逻辑 `[实]+[推]`**
- Agent 看到的"工具"经**本地 MCP 聚合代理**（127.0.0.1:49157）扇出；连接器是远端能力，按需启停 + 信封加密鉴权。
- 信封加密：密钥经 **HKDF-SHA256**（kdf）由设备绑定密钥 + 每文件 `salt` 派生，对称 **AES-256-GCM**（带 `keyCheck`/`userIdCheck` 完整性绑定防跨用户）。无设备密钥故无法解密，但格式已确认。
- 内置本地工具（Bash/Read/Write/Edit）+ Skill（`skills/`）+ 外部（`mcp_tools`）经同一调度层统一呈现（traces 里能同时看到 Bash 与 mcp_tools span）。

**待确认 TODO**
- [x] 连接器加密信封的设备密钥来源 **已实锤（v1.2）**：设备主密钥明文落盘于 `connectors/<account>/<uuid>/.master.key`（设备绑定，靠文件系统权限 + 物理隔离保护，不二次加密）；`connector-states.v3.json` 外层 `encryption:{scheme:aes-256-gcm, kdf:hkdf-sha256, ...}`，HKDF-SHA256 从 `.master.key` 派生实际信封密钥；`accountIdentityKey:"<uuid>||enterprise"` 标识账户。迁移状态机标记 `mcpSecurityMigrated`/`cSideAutoBoundDefaultDisabledMigrated`/`staleManagedAuthHeadersPurged` 已置 true。
- [ ] 聚合代理如何按需拉起远端 streamable-http server、超时/重连策略？
- [ ] `disabledToolsOverrides`/`headerOverrides`/`envOverrides` 的运行时生效路径？

#### 2.3.2 沙箱执行 `shell-snapshots/` + `logs/sandbox/`
**数据证据 `[实]`**
- `shell-snapshots/`（2701 个，每个 188K）：**非环境快照**。实为注入 zsh 的 **shell-integration 引导脚本**（定义 `vcs_info`/`precmd`/`preexec`/命令历史回写 hook）——终端增强，非每命令 diff。
- `logs/sandbox/`（606M）：沙箱执行专用日志（按日期 33 子目录）。`settings.json` 含"沙箱写白名单"。

**实现逻辑 `[推]`**
- 工具调用跑在受限子进程（macOS 疑似 sandbox-exec/seatbelt 类）；所有沙箱动作单独留日志用于取证 + 隔离防乱改系统。
- 注意：原推断"每命令前环境快照"错，磁盘占用来自"每会话/环境各写一份引导脚本"。

**待确认 TODO**
- [x] macOS 沙箱具体后端 **已实锤（v1.2）**：本地用户数据目录**无** seatbelt profile 实体文件（profile 由 `.app` 内置 `runtime/sandbox/seatbelt.py` 在运行时生成注入，不落盘用户数据区）；但 `logs/sandbox/<YYYYMMDD>/` 按日留痕（实测 20260719~20260805 多目录），证明沙箱真实运行且每次动作单独留痕取证。Linux 对应 bwrap。
- [x] `shell-snapshots` 写入方 **已实锤（v1.2）**：实为 zsh shell-integration 引导脚本（vcs_info/precmd/函数定义），由 host 进程在 spawn shell 前注入以加载终端增强，非"环境快照"；与沙箱无关。
- [ ] 沙箱写白名单的拦截实现（只读挂载 / 重定向）？

#### 2.3.3 Skill 系统 `skills/` + 插件 `skill-*`
**数据证据 `[实]`**
- `skills/` 每个 skill 目录：`SKILL.md`（YAML frontmatter `name`/`description` + 正文指令）+ `agents/`/`assets/`/`commands/`/`references/`/`scripts/`。
- `imagegen`/`wechat-content-hub`/`wechat-official-article` 是**符号链接**指向 `git_workspace/agent-skills/`（开发态直连源码）。
- 内置 `skill-*`（18 个）插件：`workbuddy.kind:builtin-skill` + `legacyResourceName`（旧资源名）。
- `skill-ardot-slides/SKILL.md` frontmatter：`disable-model-invocation:true` + `user-invocable:false`（模型自动触发）。
- skill 依赖注入实证：`ardot-slides` 依赖 `ardot-design-core` "injected alongside"（ardot 6 件套是 skill 家族）。

**实现逻辑 `[实]+[推]`**
- Skill = 领域工作流/知识包；LLM 读 `SKILL.md` 的 `description` 判定"何时/如何"做；触发后按需读 `references/`。
- `disable-model-invocation:true` → 模型按关键词自动触发，不靠 `/slash`。
- loader 经**目录约定发现** `SKILL.md`（多数 `plugin.json` 未声明 `skills` 键，但 `skills/<name>/SKILL.md` 物理存在）；`legacyResourceName` 表明由早期 "resource" 概念迁移而来。
- 插件内 skill 依赖：声明依赖一并注入同上下文，共享 `SKILL_ROOT` 绝对路径。

**待确认 TODO**
- [ ] skill 依赖"声明位置"（SKILL.md body 描述 vs 结构化 `dependencies` 键）？`[推]`
- [x] ~~Skill 触发匹配算法（description 全文 embedding？关键词？）。~~ **已实锤（v1.1）**：skill 经运行时 `Skill` 工具按需加载——`Skill` 工具的 `<available_skills>` 列表由 loader 提供（目录约定发现 `skills/*/SKILL.md`），云端 LLM 据 `description` 判定触发；命中后本地读 `SKILL.md` 注入上下文。匹配发生在「LLM 读 available_skills 列表选工具」层，非本地 embedding。
- [ ] `user-invocable` skill 的 `/slash` 注册与参数 schema。

#### 2.3.4 插件系统（整体）`plugins/`
**数据证据 `[实]`**（详见 §2.3.5/2.3.6）
- 三层状态分离：`installed_plugins.json`（`version:2`，`plugins` 为 **dict**：键 `name@source` → 值为**版本记录数组** `[{scope, installPath, version, installedAt, lastUpdated}]`，支持多版本并存；34 项中 32 项带 `workbuddySeedManaged:true` 预装，2 项用户装 `find-skills`/`agent-browser` 来自 `codebuddy-plugins-official`）；`settings.json.enabledPlugins`（**仅 7/34 `true`**，且这 7 个**全是主动能力型**：`weixinpay`/`sheetagent`/`tencent-docx`/`tencent-pptx`/`tencent-docs-plugin`/`agent-browser`/`find-skills`——**无一个 welcomemode/interactionmode/prompt-common/skill-\***）；`known_marketplaces.json`（3 官方市场目录，含 206 条 plugin 级 schema）。
- **`.in_use/<pid>` 锁实证**：`prompt-common/0.1.2/.in_use/` 实测存在 30+ PID 命名空文件（每活跃 agent 进程写一个），**直接证明被动常驻片段被每个 agent 进程实际加载**，而非仅声明。
- 市场分发：`marketplaces/<name>/`（分发布局，按类型分组）vs `cache/<source>/<name>/<version>/`（安装布局，扁平化），`source` 桥接。
- 4 个市场：zip 官方（codebuddy-plugins-official / cb_teams_marketplace，`.zip-metadata.json` etag 校验 + `.marketplace-version` UUID）、directory 内置（workbuddy-builtin）、用户本地（my-experts，`.codebuddy-plugin/marketplace.json` 自描述）。
- `.in_use/<pid>` 运行时锁：会拉起子进程的插件每活一个进程写一个 PID 命名空文件，退出即删。

**实现逻辑 `[实]+[推]`**
- 插件 = 带 `name@source` 身份、按版本落盘的"能力包"；可声明多种能力（skills/commands/agents/hooks/mcpServers/lspServers），框架按能力类型路由到不同子系统。
- 安装 ≠ 启用；启用闸门只管"主动型"（起 MCP/注册连接器/注入 agent/浏览器）；mode/prompt/skill 属被动常驻，不经此闸门 `[推]`。
- 系统提示是拼出来的（见 §2.3.5/2.3.6 管线）。**实锤（v1.1）**：`enabledPlugins` 闸门只管「主动能力型」插件是否注册进运行时（MCP 起不起 / 连接器注不注入 / 第三方 agent·浏览器 skill 是否进 `available_skills`）；`welcomemode`/`interactionmode`/`prompt-common` 靠 `.in_use` 锁 + Jinja `include` 常驻常加载，`skill-*` 靠运行时 `Skill` 工具按需注入——**三者都不经 `enabledPlugins` 闸门**。这就是为什么 27 个被动/提示/skill 条目不在 enabled 列表却仍可用。

**待确认 TODO**
- [x] `enabledPlugins` 对 `skill-*` 加载规则 **已实锤（v1.1，见 §2.3.4）**：仅控制主动能力型插件，`skill-*` 不经此闸门，靠运行时 `Skill` 工具按需注入。
- [ ] `workbuddy.dependencies`(welcomemode→mcp:netdrive) 未安装时处理？
- [ ] `lspServers` 本地安装形态与生命周期（catalog 有声明，本地未见实例）。
- [ ] `external_plugins/` 按需下载触发条件与落盘位置。
- [ ] 更新链路：zip 按 etag 增量拉 → 解压 → 安装 → 更新 `installed_plugins.json` + `settings.json`，周期检查标记驱动？`[推]`

#### 2.3.5 插件类型（8 类资源形态）`plugins/cache/workbuddy-builtin/`
**数据证据 `[实]`**（34 个内置插件，四层判别模型）
| # | 插件 | 标记 | kind | 真实形态 |
|---|---|---|---|---|
| 1–4 | interactionmode-{ask,craft,plan,expert} | .workbuddy-plugin | — | 提示片段库 |
| 5–7 | welcomemode-{code,design,work} | .workbuddy-plugin | — | 欢迎模式根 agent |
| 8 | prompt-common | .workbuddy-plugin | — | 全局常驻片段 |
| 9–26 | skill-*（18） | .codebuddy-plugin | builtin-skill | 能力/知识包 |
| 27–29 | tencent-docs/dcox/pptx | .codebuddy-plugin | — | 复合文档套件 |
| 30 | sheetagent | .codebuddy-plugin | — | MCP server + 编排 skill |
| 31 | mcp-ardot-mcp-app | .codebuddy-plugin | builtin-mcp-app | 内置 MCP app（bundle 托管） |
| 32 | weixinpay | .codebuddy-plugin | — | 原生二进制+MCP+skill |

**关键机制 `[实]`**
- 类型判别四层：manifest 标记 → `workbuddy.kind` → 目录约定推断 → `category` 分组。
- **`plugin.json` 的 skills/mcpServers 键可选**：loader 同时做目录约定发现（扫 `skills/*/SKILL.md`、`.mcp.json`）。weixinpay 的 plugin.json 仅 6 行却功能最强，真能力在 `.mcp.json`+`skills/`+`prebuilds/`。
- 两种 MCP 托管模型：sheetagent=插件自带 server（`mcp/start.mjs`，`defer_loading`）；ardot=bundle 托管（`bundleSegments` 指向 .app 包，`loadsCapabilities:false`，bootstrap 桩接出）。
- weixinpay 最重：3 skill（register/pay/feedback）+ `dist/mcp-server.mjs` + `prebuilds/{darwin,win32}/WeChatPayCLI`（原生国密 CLI + Windows 驱动 `QmProtectorDriver.sys` + `libTencentSM.dll`）+ `manifest.qmsig`（腾讯发布签名）+ 51 个 PID 锁。

**实现逻辑 `[实]`（v1.1 实锤）**
- **系统提示拼装管线确为 Jinja2**（读 `welcomemode-code/0.1.7/prompt.tpl` 全文）：模板含 `{% include "interactionmode-*/fragments/xxx.md" %}` 分支、`{% if workMode == "ask"/"plan"/"expert" %}` 模式选择、`{{ ResponseLanguage }}`/`{{ dataFolderName }}`/`{{ productName }}`/`{{ BinaryContext }}`/`{{ modelId }}` 等运行时变量注入、`{% if LocalSkillsMemoryEnabled %}`/`{% if ExpertManagementEnabled %}` 条件块；`agents/code.md` 仅一行 `{% include "welcomemode-code/prompt.tpl" %}`。→ **loop 协议是提示驱动（prompt-as-code）**，不是硬编码。
- **`interaction.md` 的 `tools:` frontmatter 即工具白名单**，且出现 `Defer(SkillManage)`/`Defer(ImageGen)`/`Defer(LSP)`/`Defer(TeamCreate)`/`Defer(workbuddy_cloudstudio_deploy)` 等 → **`Defer(...)` 语法就是 `defer_loading` 在提示层的体现**：重能力（图像/视频生成、LSP、团队协作、部署）不常驻，首次触发才拉起。
- **重要澄清**：craft 与 expert 模式的 `tools` 白名单**几乎完全一致**（差异仅在 expert 额外有 `ExpertManagement` 相关），说明模式差异主要在**行为指令层**（agent-loop / result-presentation / ask 的 mode-behavior），而非工具集裁剪。
- welcomemode = 根 agent（persona）；三种 welcomemode 靠各自 `settings.json` 的 `agent` 字段选根 agent 类型（**实锤**：`welcomemode-code`=`{"agent":"code"}`、`design`=`{"agent":"design"}`、`work`=`{"agent":"work"}`）。
- tencent-docx = 流水线插件（doc-writer→doc-formatter→doc-converter 三 Stage agent + tdoc-orchestrator skill + SessionStart hook）。
- weixinpay = 三层（skill 编排层 / mcp-server 工具层 / WeChatPayCLI 国密签名层）。

**待确认 TODO**
- [x] ~~welcomemode-design/work 的 `settings.json` 是否 `{"agent":"design"}`/`{"agent":"work"}`？~~ **已实锤（v1.1）**：实测 `design`=`{"agent":"design"}`、`work`=`{"agent":"work"}`、`code`=`{"agent":"code"}`，`agent` 字段标识根 agent persona 类型。
- [x] weixinpay `mcp-server.mjs` 调原生 CLI **已实锤（v1.2）**：`dist/mcp-server.mjs`（20529 行）`import { execFile } from "node:child_process"`，`spawnCli()`（L873）用 `execFile(cliPath, args, {...})` 调 `prebuilds/{darwin-x64,darwin-arm64,win32-x64}/wechatpay-cli[.exe]`；含 SM3 子命令、单次在途（`only one CLI spawn in flight at a time`）、`AbortSignal` 取消、超时（默认 `SPAWN_TIMEOUT_MS`）。核心国密 SM2/SM3 签名在原生 CLI 完成，node 侧仅 stdlib `node:crypto`。
- [x] `manifest.qmsig` **已实锤（v1.2）**：腾讯 QMSIG 发布签名清单，格式 `QMSIG1\nMSIG <64hex SHA256 清单哈希>\nALG RSA-2048-PKCS1-SHA256\nROOT <构建机绝对路径>`（实测 `C:\dev\openclaw-wechatpay-p-30a5ed63...`）。宿主（Electron 主进程/插件加载器）**安装/启用插件时用内置公钥验 MSIG 签名**确认包未篡改；**不在** mcp-server.mjs 运行时（grep 确认 mjs 无 qmsig/verify 调用）。
- [x] `defer_loading` 触发条件 **已实锤（v1.2）**：`interaction.md` 的 `tools:` frontmatter 声明 `Defer(SkillManage)`/`Defer(LSP)`/`Defer(ImageGen)`/`Defer(VideoGen)`/`Defer(TeamCreate)`/`Defer(TeamDelete)`/`Defer(conversation_search)`/`Defer(workbuddy_cloudstudio_deploy)` 共 8 处；`Defer(X)` 即提示层"X 工具延迟加载"——**模型首次在 function_call 里请求该工具名时，框架才拉起对应能力包/MCP server**（首次触发拉起，常驻前不占资源）。

#### 2.3.6 插件隐藏配置 `*.plugin/plugin.json`
**数据证据 `[实]`**
- 每个插件隐藏目录**有且仅有 `plugin.json` 一个配置**（source 级 `marketplace.json` 另算），别无二级配置。
- 标记语义：`.workbuddy-plugin`（interactionmode/welcomemode/prompt-common，瘦 schema，纯提示）vs `.codebuddy-plugin`（能力类，全 schema）。**产品血统戳**：`.codebuddy-plugin` 是 CodeBuddy 时代遗留（描述里写 "CodeBuddy"/"CodeBuddy Code"）→ **WorkBuddy = CodeBuddy rebrand**。
- `workbuddy` 扩展块三形态：
  - `builtin-skill`：`kind`+`legacyResourceName`+`bundleSegments`（指向 .app 包内真身）。
  - `builtin-mcp-app`：+`runtimeSupportSegments`（bootstrap 入口）+`loadsCapabilities:false`。
  - `welcomeMode.dependencies`：`[{type:mcp,name:netdrive}]`（外部依赖声明）。
- `workbuddy-builtin/.codebuddy-plugin/marketplace.json`：`sourceRoots` 明文写 `/Applications/WorkBuddy.app/.../resources/builtin-plugins`；仅列 3 个头条插件（weixinpay 1.5.111），cache 里 34 个（weixinpay 1.6.107）→ **cache 是安装镜像，真身在 .app 包**。

**实现逻辑 `[推]`**（加载流程）
```
installed_plugins.json → cache/<src>/<name>/<ver>/.{code|work}buddy-plugin/plugin.json
 ├─ marker: .workbuddy-plugin→拼系统提示；.codebuddy-plugin→能力资源
 ├─ workbuddy.kind: builtin-*→bundleSegments 解析到 .app 包真身
 ├─ skills/commands/agents/hooks: 按路径读磁盘
 ├─ mcpServers: 注册启动描述符（defer_loading 者首次调用才拉起）
 └─ enabledPlugins 决定 <name>@<source> 是否进运行时
```

**待确认 TODO**
- [x] ~~`enabledPlugins` 对 `skill-*` 全量加载确认 `[推]`。~~ **已实锤（v1.1）**：`enabledPlugins` 仅含 7 个主动能力型插件（weixinpay/sheetagent/tencent-docx/pptx/docs/agent-browser/find-skills），**无 skill-\***；`skill-*` 经运行时 `Skill` 工具按需加载（见 §2.3.3），不靠此闸门。
- [ ] `bundleSegments` cache 文件与 bundle 真身是否 double-load（推测优先 bundle，cache 仅 manifest 镜像）。
- [ ] `dependencies` 的 MCP（netdrive）定义驻留何处（connectors/ 或 app bundle 内某 mcp 注册表）？
- [ ] `marketplace.json` 仅 3 条而 cache 34 条，其余是否走目录扫描发现？

### 2.4 记忆与个性化 `memory/` + `experts/` + `models.json`

**数据证据 `[实]`**
- `memory/d0c87ac3-..._memory.md` + `.bak`：跨工程长期记忆，markdown 形态（给 LLM 作上下文注脚的用户画像/偏好），`.bak` 表"提取→覆盖"沉淀流程。
- `experts/custom/<uid>/experts.json`：自建专家 **ID 列表**（如 `["xiaohongshu-content-creator"]`）；专家 = 角色/系统提示包。
- `models.json`：LLM 抽象层，多供应商走 OpenAI 兼容（glm-5.2/glm-5.1/qwen3.7-plus，均 dashscope），带能力声明 `supportsToolCall`/`supportsImages`/`supportsReasoning`/`maxInputTokens`，**明文 API key**。
- `prompt-common/fragments/`：`workbuddy-memory-system.md`（记忆系统指令）、`plugin-recommendation.md`（插件推荐指令）常驻注入。

**实现逻辑 `[实]+[推]`**
- 个性化 = 长期记忆（用户画像）+ 专家（角色）+ 技能（能力包，可源码直链）+ 模型（供应商抽象）；系统提示由这些层组装。
- 记忆跨工程共享，markdown 随运行提取沉淀（分代晋升同思路）。
- **记忆召回 = 全文注入（实锤 v1.1）**：读 `prompt-common/fragments/workbuddy-memory-system.md` + `memory-context.md` 证实——三层记忆（①云端自动注入 profile + `conversation_search` 检索历史对话；②用户级 `~/MEMORY.md`；③项目级 `.workbuddy/memory/`）均**全文拼进 system prompt**；`memory-context.md` 仅 `{{ WorkingMemoryContent }}/{{ UserLocalMemoryContent }}/{{ UserMemoryContent }}` 三个模板变量占位符，无向量检索。`conversation_search` 是独立工具、服务端排名。本地 `memory/_memory.md` 是云端 profile 的**缓存镜像**（workbuddy-memory-system.md 明写 "managed by the server; any local writes will be overwritten"）。

**待确认 TODO**
- [ ] 长期记忆提取时机与触发（每 run 结束？哪个 LLM 调用做提取）？
- [x] ~~记忆召回方式（全文 vs 向量）。~~ **已实锤（v1.1）**：读 `prompt-common/fragments/workbuddy-memory-system.md` + `memory-context.md`——**记忆 = 全文注入，非向量检索**。`memory-context.md` 仅 `{{ WorkingMemoryContent }}/{{ UserLocalMemoryContent }}/{{ UserMemoryContent }}` 三个模板变量占位符；三层（云端 profile / 用户级 MEMORY.md / 项目级 memory）全文拼进 system prompt。历史对话检索走独立 `conversation_search` 工具（服务端排名）。本地 `memory/_memory.md` 是云端 profile 的**缓存镜像**（workbuddy-memory-system.md 明写 "managed by the server; any local writes will be overwritten"）。
- [ ] 多模型路由策略（`models.json` 里多模型如何按任务选）。

---

## 3. 基础能力

### 3.1 模型对接（Model Integration）`models.json`

**数据证据 `[实]`**：单一 JSON，多模型条目，字段 `name`/`baseUrl`(dashscope 兼容)/`apiKey`(明文)/`supportsToolCall`/`supportsImages`/`supportsReasoning`/`maxInputTokens`。`local_storage` 解码露出编排后端 `copilot.tencent.com`。

**实现逻辑 `[实]+[推]`**
- LLM 抽象层：多供应商统一走 OpenAI 兼容接口（与 cagent `llm/base.py` 同构）。
- 推理在云端，编排在本地；模型能力声明驱动工具调用/多模态/推理开关。

**待确认 TODO**
- [x] ~~推理与工具调用是否都在 `copilot.tencent.com` 完成（端侧仅转发？）。~~ **已实锤（v1.1）**：云端是"脑"——`workbuddy-memory-system.md` 明写 profile 由 server 生成、conversation_search 服务端排名、Auto 档模型路由（fast/balanced/deep）在 `copilot.tencent.com`；本地是"身体"：Jinja 渲染 system prompt + 工具执行（Bash/skill/MCP/沙箱）+ 事件落盘。推理/工具选择/记忆生成/检索排名全在云端，端侧只转发请求与执行工具。
- [ ] 流式（SSE）与 function calling 的协议细节（Responses API 风）。

### 3.2 可观测（Observability）`traces/` + `audit-log/`

**数据证据 `[实]`**
- `traces/<会话>/trace_*.json`：span 树，字段 `traceId/spanId/parentId/name/type/startedAt/endedAt/duration/status/error`。实测某会话 `spanCount:113`、`totalTokens:11,030,019`、`status:ok`。span 名称：`mcp_tools`/`Bash`/`Read`/`Write`/`Edit`/`Skill`。
- `audit-log/*.jsonl`：`firstHash`/`lastHash`/`fileSha256` 防篡改哈希链，`manifest.jsonl` 封存各段；实测记录 `command-safety` 安全决策。

**实现逻辑 `[实]`**
- traces = 轻量计时树（无完整 payload），与 projects jsonl 完整事件流互补：jsonl 记"发生了什么"，traces 记"各步耗时/成败"，`parentId` 一致串成同一追踪。
- audit-log = 哈希链合规取证层，任何篡改破坏链。

**待确认 TODO**
- [ ] trace span 是否含 input/output 摘要（当前只确认计时字段）？
- [ ] `command-safety` 审计的判定来源（本地规则引擎？云端策略？）。
- [ ] 总 token/cost 如何聚合到 trace 的 `totalTokens`/`totalCost`。

### 3.3 存储（Storage）`blobs/` + `artifact-index/` + `workspace/` + `file-history/` + `workbuddy.db` + `local_storage/` + `tasks/` + 迁移

**数据证据 `[实]`**
- `blobs/`：内容哈希分桶（132 桶/177 文件，实测 `.jpg` 1080×720），去重。
- `artifact-index/`：每产物 json，含 `file-changes` 类型（additions/deletions/diff 文本）= `present_files` 底层。
- `workspace/sessions/<id>/{fs,snapfile}`：虚拟文件树 + 快照，预览渲染在隔离虚拟 FS。
- `file-history/`：`@v1/@v2…` **全量**历史版本（实测 `@v2` 即 `loop.py` 真实源码全文）。
- `workbuddy.db`（Drizzle ORM / SQLite）：`automations`(含 `push_to_wechat`/`push_to_wecom_bot`/`expert_id`/`skills_json`/软删 `deleted_at`)、`automation_runs`、`automation_delivery_outbox`(发件箱)、`session_usage`(`credit_json` 计费)。
- `.workbuddy-sqlite-migrations/`：0000→0001→0002 + meta。
- `local_storage/`：gzip+base64 的 `entry_*.info`（Electron localStorage，leveldb 风格）。
- `tasks/`：自动化运行态快照（每任务 `1.json/2.json`），**定义在 `workbuddy.db`，勿手删**。

**实现逻辑 `[实]+[推]`**
- 产物：内容哈希去重 + 索引驱动差异展示。
- 文件安全改动 = 虚拟层试改 → 版本留底 → 真实落盘（全程可追溯/回退）。
- 结构化状态（自动化/计费/设置）走 SQLite + Drizzle + 版本化迁移；应用键值走 leveldb；自动化以"工具 API + SQLite"为唯一入口。

**待确认 TODO**
- [ ] `workspace/` 虚拟 FS 与真实磁盘的"确认落盘"协议（用户点确认后走哪条路径）？
- [ ] `file-history` 版本保留策略（上限？是否随项目清理）。
- [ ] `automation_delivery_outbox` 出队与公众号/企微推送的可靠性（失败重试？）。

---

## 4. 业务实现

### 4.1 项目（Projects）`projects/`

**数据证据 `[实]`**：`projects/<工程路径编码>/` 每会话一个 `<session-id>.jsonl` + `tool-results/` + `subagents/`。目录名是工程绝对路径的编码（如 `Users-chongcb-...-cagent`）。35 个工程（含历史临时）。

**实现逻辑 `[实]`**：项目 = 工程级运行日志容器，所有对话/运行/工具产物按工程归集。`plans/` 目录为空（Plan 模式持久化预留，未启用）。

**待确认 TODO**
- [ ] 工程路径编码规则（如何编码 `/` 与特殊字符）。
- [ ] 同一工程多会话的索引（是否在 `projects/<enc>/` 下有会话清单文件）。

### 4.2 会话（Sessions）`sessions/` + `app/sessions.json`

**数据证据 `[实]`**
- `sessions/*.json`：运行中会话进程心跳（`pid`/`lastHeartbeat`/`cwd`/`kind`）；应用退出后多半残留。
- `app/sessions.json`：会话索引（`conversationId ↔ userId ↔ workDir ↔ startedAt`），支撑最近会话/恢复。

**实现逻辑 `[实]`**：本地后端对"哪些 Agent 正在跑"实时登记（崩溃检测/重复启动拦截靠它）；会话索引支持恢复。

**待确认 TODO**
- [ ] 会话心跳的写频率与超时判定（lastHeartbeat 多久算死）。
- [ ] 会话与 project jsonl 的 `sessionId` 是否同一 ID 空间。

---

## 5. 其他 / 支撑模块

### 5.1 跨端同步（Edge Sync）`edge-sync-mapping*.db`

**数据证据 `[实]`**（SQLite，3 表）
- `edge_sync_mapping`（`session_id ↔ conversation_id ↔ msg_channel`，如 `convmsg:<userId>`，10 行）。
- `edge_sync_artifact_cache`（`file_path,mtime_ms,size,file_hash,download_url,smh_path,content_type,expires_at,uploaded_at`）：文件→云端 **SMH**（Smart Media Hub）+ download_url + TTL；当前 0 行。
- `edge_sync_image_mapping`（`blob_id,cos_uri,session_id,created_at`）：`blobs/`→腾讯云 **COS**；当前 0 行。
- 配合 `device-id`（机器指纹）、`qimei-cache.json`（`qimei36`）。

**实现逻辑 `[实]+[推]`**：会话经 `edge_sync_mapping` 对齐云端；文件经 SMH、图片经 COS 上传后本地留带 TTL 缓存索引，下载按 `file_hash` 校验去重 → 多设备一致。

**待确认 TODO**
- [ ] 后两表为空：跨端同步默认关闭还是按会话触发？`[推]`
- [ ] 上传触发点（文件变更即传？会话结束传？）。
- [ ] `device-id` 与 `qimei36` 在同步冲突归并中的角色。

### 5.2 桌面进程模型（Desktop Process）`app/`

**数据证据 `[实]`**
- `app/session/`：Electron 渲染进程浏览器内核缓存（`Code Cache` 474M、`Cache` 298M、`Cookies`、`WebStorage`）。
- `app/Singleton*`：`SingletonLock/Socket/Cookie` 单实例锁。
- `app/sessions.json`：会话索引（见 §4.2）。
- `app/renderer-version.json` / `WorkBuddy_dtreport.txt`：版本/运行标记。

**实现逻辑 `[实]`**：预览面板/市场页是内嵌网页，共享浏览器存储；单实例锁保证单进程。

**待确认 TODO**
- [ ] 本地后端（host）与主进程、渲染进程的职责切分（哪个进程跑 Agent loop、哪个跑 MCP server）。
- [ ] `app/session` 缓存与 `workspace/` 虚拟 FS 的关系（预览是否复用浏览器存储）。

### 5.3 安全与审计（Safety & Audit）`audit-log/` + `connector-states` 加密

**数据证据 `[实]`**
- `audit-log/` 哈希链记录 `command-safety` 决策（§3.2）。
- `connector-states.v3.json` aes-256-gcm 信封（§2.3.1）。
- `ioa-im-override.json`（`allowNonTencentIM:false`）等合规开关。
- weixinpay `manifest.qmsig` 发布签名（§2.3.5）。

**实现逻辑 `[实]+[推]`**：三条安全基线——执行隔离（沙箱）+ 凭证信封加密 + 审计哈希链；供应链签名（qmsig）防插件篡改。

**待确认 TODO**
- [ ] `command-safety` 拦截后如何反馈给 loop（转 user 确认？跳过？）。
- [ ] 信封加密密钥是否在登录态/钥匙串托管（注销后能否解密）。

---

## 6. 全局待确认 TODO 汇总（按优先级）

**P0（实现前必须搞清，影响架构）** —— *v1.1：4 项全部实锤闭合*
1. ~~`enabledPlugins` 对 `skill-*` 的加载规则~~ **已实锤（v1.1）**：`enabledPlugins` 仅控制「主动能力型」插件注册（MCP/连接器/第三方 agent），被动片段 + skill 不经此闸门（详见 §2.3.4/2.3.6）。
2. ~~系统提示拼装管线是否确实 Jinja `include`~~ **已实锤（v1.1）**：读 `welcomemode-code/prompt.tpl` 全文，标准 Jinja2，`include` + 变量 + 条件块（§2.3.5）。
3. ~~长期记忆召回方式（全文 vs 向量）~~ **已实锤（v1.1）**：全文注入，非向量；`conversation_search` 独立工具服务端排名（§2.4）。
4. ~~推理/工具调用是否在 `copilot.tencent.com` 完成~~ **已实锤（v1.1）**：云端是脑（推理/工具选择/记忆生成/检索排名），本地是身体（Jinja 渲染 + 工具执行 + 落盘）（§3.1）。

**P1（影响核心模块正确性）** —— *v1.2：#6/#7/#8 已实锤闭合*
5. 多 Agent 的 `subagent_type` 枚举与并行度/嵌套深度——§2.2 `[未实锤]`
6. ~~MCP 信封加密设备密钥来源~~ **已实锤（v1.2）**：`.master.key` 明文设备主密钥 + `encryption{aes-256-gcm, hkdf-sha256}`（§2.3.1）。
7. ~~沙箱具体后端（seatbelt profile）~~ **已实锤（v1.2）**：本地无 profile 实体，`logs/sandbox/<YYYYMMDD>/` 按日留痕（§2.3.2）。
8. ~~skill 依赖声明位置与触发匹配算法~~ **已实锤（v1.2）**：`Defer(...)` 8 处提示层声明 + loader 目录约定发现 `SKILL.md`（§2.3.3/2.3.5）。
9. `workspace/` 虚拟 FS 确认落盘协议——§3.3 `[未实锤]`

**P2（影响工程完整度）** —— *v1.2：#10/#11/#12 已实锤闭合*
10. ~~weixinpay 原生 CLI 调用方式（IPC）~~ **已实锤（v1.2）**：`execFile` 调原生 `wechatpay-cli` 国密 CLI（§2.3.5）。
11. ~~`manifest.qmsig` 校验时机~~ **已实锤（v1.2）**：QMSIG1/RSA-2048 发布签名，安装/启用时宿主验签（§2.3.5）。
12. ~~`defer_loading` 触发条件~~ **已实锤（v1.2）**：模型首次 function_call 请求该工具名时拉起（§2.3.5）。
13. `external_plugins/` 按需下载——§2.3.4 `[未实锤]`
14. 跨端同步触发条件——§5.1 `[未实锤]`
15. 会话心跳超时判定——§4.2 `[未实锤]`

---

## 7. 工程取舍（从目录/格式反推，照着学）

1. **事件溯源而非 CRUD**：对话用追加 jsonl，天然回放/恢复/审计（代价体积增长）。
2. **大对象 spill 出主日志**：工具产物落 `tool-results/`、图片落 `blobs/`，主 jsonl 只存引用。
3. **工具协议标准化为 MCP**：远端 = streamable-http MCP server，本地聚合代理扇出；连接器状态信封加密 + 迁移标记状态机。
4. **轻量追踪 vs 完整事件流互补**：traces 记计时/成败，jsonl 记完整内容，解耦。
5. **预览与真实磁盘分离**：`workspace/` 虚拟 FS 零风险试改，`file-history/` 兜底回退。
6. **配置即迁移状态机**：连接器状态 + SQLite 用版本/标记追踪升级，向后兼容当一等公民。
7. **哈希链审计 + 跨端同步做进数据层**。
8. **多 Agent 层级编排**：`Agent` 元工具派发专用子 agent，子会话独立落盘、结果上卷。
9. **插件 = 能力包声明，系统提示可组合**：manifest 声明能力字段，框架按字段路由；提示由 Jinja 拼装（agent loop/白名单/结果协议皆可插拔）。
10. **双产品血统标记共存**：`.workbuddy-plugin`/`.codebuddy-plugin` 向后兼容 CodeBuddy 生态。
11. **提示即代码（prompt-as-code）是最大亮点（v1.1 实锤）**：agent loop 协议（`agent-loop.md`）、工具白名单（`interaction.md` 的 `tools:` frontmatter）、结果投递协议（`result-presentation.md`）**全部写在 `interactionmode` 的 fragment 里、经 Jinja `include` 拼进 system prompt**，而非硬编码在运行时——改 loop 行为只需改片段。`interaction.md` 的 `Defer(...)` 语法（`Defer(ImageGen)`/`Defer(LSP)`/`Defer(TeamCreate)`/`Defer(workbuddy_cloudstudio_deploy)`）即 `defer_loading` 的提示层体现：重能力不常驻，首次触发才拉起。模式差异主要在**行为指令层**而非工具集裁剪（craft/expert 白名单几乎一致）。

---

## 8. 实现指导（照着造一个 Agent 工具）

> 下面给出**分阶段路线图**，每阶段对应上面模块，可直接作为工程计划。先 MVP，再补企业级能力。

### Phase 0 · 数据底座（先定存储，后续全靠它）
- **事件流**：`projects/<project>/<session>.jsonl`（OpenAI Responses API 风，字段 `type/role/content/timestamp/id/sessionId/cwd/parentId/providerData`）；工具大返回 spill 到 `tool-results/`。
- **结构化状态**：SQLite + 迁移（Drizzle 类），表 `automations`/`automation_runs`/`session_usage`；`.in_use/<pid>` 锁用空文件。
- **产物**：`blobs/`（内容哈希分桶）+ `artifact-index/`（diff 索引）。
- **文件改动**：`file-history/@vN` 全量版本 + `workspace/` 虚拟 FS 预览层。
- ⚠️ 不要先写 UI，先把"一次运行能落盘成可回放 jsonl"跑通。

### Phase 1 · Loop 引擎（事件溯源 + ReAct）
- 主循环：读 jsonl → planner 出 plan → executor 按 step 调度 → 每 step 内 ReAct（`reasoning`→`function_call`→`function_call_result`）。
- 关键：`reasoning` 事件显式写委派/工具决策（可观测）；`parentId` 串层级。
- 验收：单步多工具调用的对话能完整回放。

### Phase 2 · 工具系统（MCP 网关 + 沙箱 + Skill）
- **MCP 网关**：本地聚合代理（http，如 127.0.0.1:49157），扇出远端 streamable-http server；连接器状态用 aes-256-gcm 信封加密（HKDF-SHA256 派生密钥，设备绑定）。
- **沙箱**：工具调用跑在受限子进程（macOS seatbelt / Linux bwrap），`logs/sandbox/` 单独留痕；**先别做 shell-integration 引导脚本**（那是终端增强，非必须）。
- **Skill**：`SKILL.md` + 目录约定发现；支持 `disable-model-invocation`（模型自动触发）+ skill 间依赖注入。
- 验收：内置 Bash/Read/Write/Edit + 一个 remote MCP + 一个 skill 都能调。

### Phase 3 · 记忆与个性化
- `memory/_memory.md`（markdown，跨工程）+ `.bak` 沉淀流程；`experts/`（角色 ID 列表）；`models.json`（多供应商 OpenAI 兼容 + 能力声明）。
- 系统提示组装：长期记忆 + 专家 persona + 模式片段（见 Phase 5）。

### Phase 4 · 多 Agent 编排
- 加 `Agent` 元工具：`function_call name:Agent` + `subagent_type` + 任务文本；子会话落 `subagents/agent-<id>.jsonl`；结果经 `function_call_result.output` 上卷（manager/worker）。
- 先支持 Explore 一类只读子 agent，再扩并行/嵌套。

### Phase 5 · 插件系统（系统提示可组合是精髓）
- 三层状态：`installed_plugins.json` / `enabledPlugins` / `known_marketplaces.json`。
- **提示拼装管线**：`welcomemode/prompt.tpl`（Jinja）按 `workMode` `include` `interactionmode-*/fragments/*.md`（含 `agent-loop.md`/工具白名单 `interaction.md`/`result-presentation.md`）+ `prompt-common` 常驻底座。
- **manifest 双标记**：`.workbuddy-plugin`（纯提示，瘦）/`.codebuddy-plugin`（能力，全）；`workbuddy.kind` 区分 builtin-skill/builtin-mcp-app；`bundleSegments` 指向真身。
- 支持两种 MCP 托管：插件自带 server（`defer_loading`）vs bundle 托管（`loadsCapabilities:false` + bootstrap 桩）。

### Phase 6 · 可观测与审计
- `traces/` 轻量 span 树（计时/成败，无 payload）；`audit-log/` 哈希链记录安全决策（`command-safety`）。
- 验收：一次运行结束后能在 trace 里还原"调了哪些工具、各花多久"。

### Phase 7 · 跨端同步（企业级，可后置）
- `edge_sync_mapping`（会话对齐）+ `artifact_cache`（文件→SMH，TTL）+ `image_mapping`（图→COS）；上传后本地留带 TTL 索引，下载按 `file_hash` 校验。
- 默认关闭，按会话/用户开关触发。

### Phase 8 · 桌面端（Electron，最后做）
- 渲染进程（预览/市场页）+ 单实例锁 + 会话索引 + `localStorage`（gzip+base64）。
- host 进程跑 Agent loop + MCP server；沙箱子进程隔离。

### 关键设计决策建议
- **提示可组合**是 WorkBuddy 最大亮点：把 agent loop 行为、工具白名单、结果投递协议全做成插件片段，而非硬编码——造工具时务必从第一天就支持"模式切换改 loop"。
- **MCP 当工具协议一等公民**：新能力优先做成 MCP server，而非内嵌函数，便于按需启停/隔离/远程。
- **事件溯源先行**：所有"状态"尽量用追加日志表达，结构化 DB 只存"需要随机查询"的部分（自动化/计费/设置）。
- **安全三条线**：沙箱隔离 + 凭证信封加密 + 审计哈希链，企业场景缺一不可。

---

## 9. 修订历史
- 2026-09-11 v1.0：由 4 份分册（internals-investigation v0.4 / plugin-system / plugin-types / plugin-config-hidden）归并为单参考文档，按"核心/基础/业务/其他"模块重组，每模块三段式（数据证据/实现逻辑/待确认），附全局 TODO 与实现路线图。
- 2026-09-11 v1.2：P1 #6/#7/#8 与 P2 #10/#11/#12 六项实地取证闭合——`.master.key` 明文设备主密钥 + `aes-256-gcm`/`hkdf-sha256` 信封（§2.3.1）；seatbelt profile 本地不落盘但 `logs/sandbox/<YYYYMMDD>/` 按日留痕（§2.3.2）；weixinpay `mcp-server.mjs` 经 `execFile` 调原生国密 CLI `wechatpay-cli`（§2.3.5）；`manifest.qmsig` 为 `QMSIG1`/`RSA-2048-PKCS1-SHA256` 腾讯发布签名、安装时宿主验签（§2.3.5）；`interaction.md` 8 处 `Defer(...)` 即提示层延迟加载、首次 function_call 请求时拉起（§2.3.5）。剩余未实锤：#5 多 Agent 并行度枚举、#9 workspace 虚拟 FS 落盘、#13 external_plugins 下载、#14 跨端同步触发、#15 会话心跳超时。
- 2026-09-11 v1.1：实锤闭合全部 P0——①读取 `welcomemode-code/prompt.tpl` 证实系统提示 = Jinja2 模板（`include`/`{% if workMode %}`/变量注入），loop 协议是 prompt-as-code；②`interaction.md` 的 `tools:` frontmatter + `Defer(...)` 语法 = 工具白名单 + defer_loading 提示层；③`settings.json.enabledPlugins` 仅 7 个主动能力型（无 skill/模式片段），被动片段靠 `.in_use` PID 锁常加载、`skill-*` 经 `Skill` 工具按需注入，闭合"被动能力不经 enabledPlugins 闸门"；④`prompt-common` 的 `workbuddy-memory-system.md`+`memory-context.md` 证实记忆=全文注入（非向量）；⑤`installed_plugins.json` 实为 `version:2` dict 结构（修正 v1.0 描述）；⑥云端是脑（copilot.tencent.com 做推理/工具选择/记忆生成/检索排名）、本地是身体（Jinja 渲染+工具执行+落盘）。P0 #1/#2/#3/#4 及多处子 TODO 标记 [x]。
