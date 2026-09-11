# WorkBuddy 内部实现调研（数据目录考古）

> 调研方式：只读 `~/.workbuddy` 这个 WorkBuddy 桌面客户端的全局数据根目录，从**实际落盘的目录结构与文件格式**反推其 Agent 实现方式。所有结论均为"基于证据的推断"，文中标注证据来源（已读真实内容 / 仅依命名推断）与置信度。
>
> 状态：v0.4（已逐目录取证并填充全部子系统；§6 三个未决项已实证）。本文随取证逐步细化，见末尾「修正记录」。

---

## 0. 文档约定

- **证据等级**：`[实]` = 已读真实文件内容；`[推]` = 依目录/文件名/规模推断，未读内部。
- 路径相对 `~/.workbuddy/`。
- 体积数据截至 2026-09-11：`~/.workbuddy` 总占用约 **4.3 GB**，文件约 1 万+。

---

## 1. 一句话结论

WorkBuddy 是一个 **Electron 桌面客户端 + 本地后端进程 + 云端 LLM** 的 Agent 应用：云端模型做"脑"，本地进程做"身体"（执行 shell、读写文件、跑沙箱、管产物）。一次 Agent 运行被拆成多个可独立演进的数据子系统，通过**追加式事件流（jsonl）**和 **MCP 工具网关**串联，并配套调用链追踪、内容寻址产物库、版本化文件层、哈希链审计、SQLite 结构化状态、多 Agent 层级编排与跨端同步。

---

## 2. 目录清单（已逐块填充）

| 目录 | 大小 | 用途 | 证据 | 状态 |
|---|---|---|---|---|
| `projects/` | 141M | 各工程运行日志（jsonl 事件流）+ 工具产物 | `[实]` §3.1 | ✅ |
| `projects/<session>/subagents/` | - | 多 Agent 编排：派生子 agent 的会话转录 | `[实]` §3.9 | ✅ |
| `traces/` | 951M | 调用链追踪（每会话一目录，span 树） | `[实]` §3.2 | ✅ |
| `blobs/` | 282M | 内容寻址产物存储（哈希分桶） | `[实]` §3.4 | ✅ |
| `artifact-index/` | 3.1M | 产物元数据 / file-changes diff 索引 | `[实]` §3.4 | ✅ |
| `file-history/` | 11M | 文件版本化全量快照（@v1/@v2） | `[实]` §3.4 | ✅ |
| `workspace/` | 76K | 预览用虚拟文件系统（fs + snapfile） | `[实]` §3.4 | ✅ |
| `shell-snapshots/` | 496M | zsh shell-integration 引导脚本（**非环境快照**⚠️修正） | `[实]` §3.3 | ✅ |
| `logs/` | 998M | 运行 / 沙箱日志 | `[实]` §3.3 | ✅ |
| `connectors/` | 56K | 连接器(MCP)状态（aes-256-gcm 信封加密） | `[实]` §3.3 | ✅ |
| `skills/` | 148M | 已装 skill（SKILL.md + 资源目录；含符号链接） | `[实]` §3.5 | ✅ |
| `experts/` | 4K | 自定义专家清单 | `[实]` §3.5 | ✅ |
| `memory/` | 16K | 跨工程长期记忆（markdown + .bak） | `[实]` §3.5 | ✅ |
| `models.json` | 1.2K | LLM 抽象层配置（多供应商） | `[实]` §3.5 | ✅ |
| `settings.json` | 0.6K | 插件 / 沙箱白名单 / 频道配置 | `[实]` §3.5 | ✅ |
| `tasks/` | 1.1M | 自动化任务运行态快照（**勿手删**） | `[实]` §3.6 | ✅ |
| `workbuddy.db` | ? | 结构化状态（Drizzle ORM / SQLite） | `[实]` §3.6 | ✅ |
| `sessions/` | 36K | 运行中会话进程心跳 | `[实]` §3.8 | ✅ |
| `local_storage/` | 1.0M | 应用 localStorage（gzip+base64 leveldb） | `[实]` §3.8 | ✅ |
| `audit-log/` | 4.3M | 防篡改哈希链审计（command-safety） | `[实]` §3.7 | ✅ |
| `edge-sync-mapping*.db` | ~1.5M | 端云同步映射（SMH/COS，3 表） | `[实]` §3.7 | ✅ |
| `app/` | 797M | Electron 内核缓存 / 单实例锁 / 会话索引 | `[实]` §3.8 | ✅ |
| `binaries/` | 372M | 自带 Python 3.13.12 + Node 22.22.2 运行时 | `[实]` | ✅ |
| `connectors-marketplace/` / `plugin-marketplace-state-new/` | 39M | 市场元数据 / 状态戳 | `[推]` | ✅ |
| `.workbuddy-sqlite-migrations/` | 4.5K | Drizzle 迁移脚本（0000→0002 + meta） | `[实]` §3.6 | ✅ |
| `plans/` | 0 | Plan 模式预留（空，未启用） | `[实]` | ✅ |

---

## 3. 子系统实现（逐块反推）

### 3.1 运行日志与事件溯源 `projects/`

**结构**：`projects/<工程路径编码>/` 下每个会话一个 `<session-id>.jsonl`（命名即 session id），另有 `tool-results/` 子目录与 `subagents/` 子目录。

**jsonl 格式（已读）**：采用 **OpenAI Responses API 风格**的事件流——每行一个对象，顶层有 `type`（如 `message` / `reasoning` / `function_call` / `function_call_result`）、`role`、`content`、`timestamp`（毫秒）、`id`、`sessionId`、`cwd`、`parentId`、`providerData`。关键：

- `providerData`：携带 `agent`（具体 agent / 模型名，如 `cli`）、`isSubAgent: true`（标记该事件来自子 agent）、`reasoning`（模型委派/工具使用的内部独白）等元信息。
- 工具调用以 `function_call` / `function_call_result` 形态呈现（`name` + `arguments` / `output`），**大对象（原始工具返回）不在主 jsonl 内联**，而是 spill 到 `tool-results/`（如 `chatcmpl-tool-*.txt`），主日志只留引用。

**反推实现**：
- 主循环是 **事件溯源（event-sourced）**：一次运行 = 一个追加的 jsonl，天然支持回放、会话恢复、断点续跑与审计。代价是单工程体积随使用增长（故 `projects/` 占 141M）。
- **多 Agent 编排（已实证，见 §3.9）**：`subagents/agent-<id>.jsonl` 是派生的完整子会话。
- `tool-results/` 的 spill 模式：避免主日志被大文本撑爆。

### 3.2 推理可观测性 `traces/`

**结构**：每会话一个目录，内含若干 `trace_*.json`（标准调用链追踪形态）。实测某会话：`spanCount: 113`、`totalTokens: 11,030,019`、`status: ok`。

**span 格式（已读）**：每个 span 字段为 `traceId / spanId / parentId / name / type / startedAt / endedAt / duration / status / error`。`parentId` 构成树；`name` 为工具/阶段名，`type` 多为 `custom`。

**观察到的 span 名称集合**：`mcp_tools`、`Bash`、`Read`、`Write`、`Edit`、`Skill`（以及其它内置工具）。——这直接映射出"工具族"：内置本地工具（Bash/Read/Write/Edit）、技能加载（Skill）、外部能力（mcp_tools）。

**反推实现**：
- 每次工具调用 / 推理阶段被记成一个 span，研发侧可还原"这次回答里 LLM 调了哪些工具、各花多久、谁失败"。
- span 是**轻量计时树**（无完整输入/输出 payload），与 `projects/*.jsonl` 里完整事件流互补：jsonl 存"发生了什么"，traces 存"各步耗时与成败"。
- 多会话分目录 → 调试可按会话隔离。`parentId` 与 jsonl 的 `parentId` 一致，串成同一层级追踪。

### 3.3 工具系统：MCP 网关 + 沙箱执行 `connectors/` + `shell-snapshots/` + `logs/sandbox/`

**工具协议 = MCP（已实证）**：
- `.mcp.json`（聚合层）：
  ```json
  { "mcpServers": { "connector-proxy": {
      "type": "http", "url": "http://127.0.0.1:49157/mcp",
      "description": "Aggregated proxy containing MCP servers: agent-mail" } } }
  ```
  即本地起一个 **MCP 聚合代理**（127.0.0.1:49157），把所有已启用的连接器扇出成统一工具面暴露给 Agent。
- `connectors/default/mcp.json`：列出几十个**远端 MCP server**，多为 `type: streamable-http`（如 `txmcp.tdx.com.cn`、`stockbuddy.qq.com`、`docs.qq.com/openapi/mcp`、`mcpgw.knot.woa.com/tapd/`），几乎全部 `disabled: true`。证实连接器就是按需拉起的远程 MCP 服务。

**连接器状态加密（已读完整结构）**：`connector-states.v3.json` 顶层字段：`enabled`(空 list)、`userDisabled`(dict)、`disabledToolsOverrides`(dict)、`accountIdentityKey`(=`userId||enterprise`，账户命名空间，**非密钥**)、`headerOverrides`(如 `custom-mcp:ardot`)、`envOverrides`(dict)，以及一串布尔迁移标记 `mcpSecurityMigrated` / `cSideAutoBoundDefaultDisabledMigrated` / `headerOverridesBearerStripped` / `staleManagedAuthHeadersPurged`，`version: 3`。
**加密信封**（`encryption` 块）字段：`scheme` / `kdf` / `salt` / `userIdCheck` / `keyCheck` / `createdAt` —— 印证**信封加密**：密钥经 **HKDF-SHA256**（kdf）由设备绑定密钥 + 每文件 `salt` 派生，对称算法 **AES-256-GCM**（scheme，带 `keyCheck`/`userIdCheck` 完整性绑定防篡改/防跨用户）；明文凭证不在顶层出现，密文存于信封内。无设备密钥故无法解密，但格式已确认。

**沙箱 / 环境（⚠️修正旧推断）**：
- `shell-snapshots/`（2701 个，每个 188K）：**不是"每次命令前对环境的 diff 快照"**。实际内容是注入 zsh 的 **shell-integration 引导脚本**（定义 `vcs_info`、`precmd`、`preexec`、命令历史回写等 hook 函数），让终端支持 git 状态提示、命令起止时间标记、历史同步等。磁盘占用来自"为每个会话/环境各写一份引导脚本"，而非环境快照比对。
- `logs/sandbox/`（606M）：沙箱执行的专用日志目录（按日期 33 个子目录）。结合 `settings.json` 的"沙箱写白名单"，推断工具调用跑在**受限子进程**里（macOS 可能是 sandbox-exec / seatbelt 类机制），所有沙箱动作单独留日志用于取证。

**反推实现**：
- Agent 看到的"工具"全部经本地 MCP 网关；连接器是远程能力，按需启停 + 信封加密鉴权。
- 本地工具（Bash/Read/Write/Edit）走内置实现，Skill 走 `skills/` 装载，外部能力走 `mcp_tools` → 网关 → 远程 server，三者经同一工具调度层统一呈现（traces 里能同时看到 Bash 与 mcp_tools span 即证）。
- 执行隔离 + 日志留痕 + 凭证信封加密是三条安全基线。

### 3.4 产物与文件系统（三层隔离） `blobs/` + `artifact-index/` + `workspace/` + `file-history/`

- **内容寻址 `blobs/`**：生成图片/文件按**内容哈希分桶**（132 桶 / 177 文件，实测 `.jpg` 1080×720），天然去重。
- **产物索引 `artifact-index/`**：每个产物一个 json，记录元数据；含 `file-changes` 类型，带 `additions / deletions / diff` 文本。这是 `present_files` 的底层——展示差异而非整文件。
- **虚拟文件系统 `workspace/`**：`sessions/<id>/{fs, snapfile}`，`fs` 是虚拟文件树，`snapfile` 是快照。**预览面板渲染在隔离虚拟 FS，非真实磁盘**——用户对"文件改动"先看到虚拟层预览，确认后才落真实工程。
- **版本化写入 `file-history/`**（已读）：被 Agent 改过的文件按 `@v1 / @v2 …` 留**全量历史版本**（实测 `@v2` 里就是 `loop.py` 真实源码全文）。说明文件写入经过托管层，每改一版都留档，支持回退，不是裸 `write`。

**反推实现**："Agent 安全地动用户文件" = 虚拟层试改 → 版本留底 → 真实落盘，全程可追溯、可回退。产物用内容哈希去重 + 索引驱动差异展示，避免重复存储与传输。

### 3.5 记忆与个性化 `memory/` + `experts/` + `skills/` + `models.json`

- **长期记忆 `memory/`**：`d0c87ac3-..._memory.md` + `.bak`。markdown 形态说明它是给 LLM 当上下文注脚的"用户画像/偏好"，而非结构化 DB；`.bak` 表明"提取新记忆 → 覆盖旧文件"的沉淀流程（与分代晋升同思路）。跨工程共享。
- **专家 `experts/`**：`experts/custom/<uid>/experts.json` 存自建专家 **ID 列表**（如 `["xiaohongshu-content-creator"]`）。专家 = 角色 / 系统提示包。
- **技能 `skills/`**（已读内部结构）：每个 skill 一个目录，含 `SKILL.md`（YAML frontmatter：`name` / `description` + 正文指令）、`agents/`、`assets/`、`commands/`、`references/`、`scripts/`。`imagegen / wechat-content-hub / wechat-official-article` 是**符号链接**，指向 `git_workspace/agent-skills/` 源码——支持"开发态直连源码"，不必打包安装；其余（wechat-mosaic-article 等）为本地副本。
- **`models.json`**：LLM 抽象层，多个供应商走 **OpenAI 兼容接口**（glm-5.2 / glm-5.1 / qwen3.7-plus，均走 dashscope），带能力声明 `supportsToolCall / supportsImages / supportsReasoning / maxInputTokens`。明文 API key（本机文件，勿外泄）。
- **`settings.json`**：已启用插件清单 + 沙箱写白名单 + 微信 claw 频道配置。

**反推实现**：个性化 = 长期记忆（用户画像）+ 专家（角色）+ 技能（能力包，可源码直链）+ 模型（供应商抽象）。系统提示由这几层组装。

### 3.6 持久化 `workbuddy.db` + `local_storage/` + `tasks/` + 迁移脚本

**`workbuddy.db`（Drizzle ORM / SQLite，已读 schema + 抽样）**：
- `automations` 表：`id / name / prompt / status / schedule_type / rrule / scheduled_at / cwds / model_id / model_is_thinking / push_to_wechat / push_to_wecom_bot / wecom_bot_source / expert_id / connector_ids_json / skills_json / permission_mode / owner_user_id / created_at / updated_at / deleted_at …`
  - 关键：automation 可绑定**专家 / 连接器 / 技能**，并可配置**推送到微信公众号（`push_to_wechat`）或企业微信机器人（`push_to_wecom_bot`）**——印证"自动化跑完把结果推公众号"的工作流。
  - 采用**软删除**（`deleted_at` 字段）；本机当前 0 行（用户经对话工具创建，库内为空属正常）。
- `automation_runs`：`thread_id / automation_id / status / runs_json / result_success / read_at …`（运行历史）。
- `automation_delivery_outbox`：发件箱模式（对接公众号/企微推送的出队表）。
- `session_usage`：`session_id / used / size / credit_json`（**计费/额度**，本机 11 行，credit_json 多为 null）。
- `tasks/` 目录：自动化任务的**运行态快照**（每任务一目录，`1.json / 2.json …`）。**定义/状态在 `workbuddy.db`，`tasks/` 只是痕迹，不能手删目录来"取消任务"**——必须用 `automation_update` 工具。

**`.workbuddy-sqlite-migrations/`**：Drizzle 迁移脚本，`0000_workbuddy_sqlite_baseline.sql` → `0001_session_plugin_context.sql` → `0002_last_user_prompt_expert_selection.sql`，外加 `meta/` 记当前版本。典型"应用数据随版本演进"。

**`local_storage/`**：Electron `localStorage`，gzip+base64 压缩的 `entry_*.info`（leveldb 风格键值）。解码后露出后端域名 **copilot.tencent.com**（即云端编排/对话后端）。

**反推实现**：结构化状态（自动化、计费、设置、运行记录）走 SQLite + Drizzle + 版本化迁移；应用自身键值走 leveldb；自动化管理以"工具 API + SQLite"为唯一入口，杜绝手改文件。

### 3.7 安全 / 审计 / 跨端 `audit-log/` + `edge-sync-mapping*.db`

- **`audit-log/`**（已读）：`.jsonl` 用 `firstHash / lastHash / fileSha256` 串成**防篡改哈希链**，`manifest.jsonl` 封存各段（含 `entryCount`、`fileSha256`）。实测记录的是 **`command-safety` 安全决策**（Agent 要执行命令时，安全层判定允许/拦截并留痕）。任何篡改都会破坏哈希链 → 合规取证层。
- **`edge-sync-mapping.db`**（SQLite，已读 schema + 抽样）：3 张表：
  - `edge_sync_mapping`（`session_id ↔ conversation_id ↔ msg_channel`）：本地会话 ↔ 云端会话 + 频道（`convmsg:<userId>`）；10 行 → 多设备同步的"会话归属"映射。
  - `edge_sync_artifact_cache`（`file_path, mtime_ms, size, file_hash, download_url, smh_path, content_type, expires_at, uploaded_at`）：本地文件 → 云端 **SMH（Smart Media Hub）**路径 + 下载 URL + 过期时间；当前 0 行 → 产物（生成文件）经云端对象存储跨设备可取。
  - `edge_sync_image_mapping`（`blob_id, cos_uri, session_id, created_at`）：`blobs/` 里的图 → 腾讯云 **COS** URI；当前 0 行 → 生成图片跨设备可见。
  - 配合 `device-id`（机器指纹）、`qimei-cache.json`（`qimei36`）。
  - **反推**：跨端同步 = 会话经 `edge_sync_mapping` 对齐到云端；文件经 SMH、图片经 COS 上传后本地留带 TTL 的缓存索引，下载时按 `file_hash` 校验去重。
- 运维标记：`ioa-im-override.json`（`allowNonTencentIM:false`）、`desktop_conversation_migrated`、`install-timing-reported`、`last-launch.json`（版本 5.3.14）——部署/迁移/合规开关类。

### 3.8 桌面端进程模型 `app/` + `sessions/` + `local_storage/`

- **`app/session/`**：Electron 渲染进程浏览器内核缓存（`Code Cache` 474M、`Cache` 298M、`Cookies`、`WebStorage`）。预览面板/市场页等 UI 是内嵌网页，共享浏览器存储。
- **`app/Singleton*`**：`SingletonLock / SingletonSocket / SingletonCookie`——Electron 单实例锁，保证同时只跑一个 WorkBuddy。
- **`app/sessions.json`**：会话索引（`conversationId ↔ userId ↔ workDir ↔ startedAt`），支撑"最近会话/恢复"。
- **`sessions/`**（已读）：运行中会话的**进程心跳**（`pid / lastHeartbeat / cwd / kind`）。本地后端对"哪些 Agent 正在跑"的实时登记——崩溃检测、重复启动拦截靠它。应用退出后多半残留。
- **`local_storage/`** 见 §3.6（露出后端 copilot.tencent.com）。

### 3.9 多 Agent 层级编排协议 `projects/<session>/subagents/`（已实证）

**结构**：会话目录下 `subagents/agent-<id>.jsonl`，每个是一个**完整的子会话**（与父 jsonl 同格式：OpenAI Responses API 风格事件流，含自己的 reasoning / function_call / function_call_result / message）。

**派发机制（已读父 jsonl）**：
- 父（顶层 agent，事件里 `providerData.agent:"cli"`）通过调用一个名为 **`Agent`** 的元工具来派发子 agent：`function_call` 事件 `name:"Agent"`，其 `providerData.reasoning` 显式记载"用 Agent 工具 + Explore 子 agent 来探索项目结构"的委派决策（编排器可见的 chain-of-thought）。
- 该 `function_call` 的输入携带 `subagent_type:"Explore"`（专用于只读代码勘察的子 agent 类型）及具体任务文本。运行时据此实例化子会话，把任务作为子会话首条 `user` 消息（已读子 jsonl 首事件即为该任务全文）。
- 父主线程里，`subagent_type` 任务会以 `type:message role:user` 形式回显（UI 可见的委派卡）；同一任务文本可多次出现 → 父**并行/多轮派生多个 Explore 子 agent** 勘察不同切片（实测同一会话出现 3 组 Explore 调用）。

**返回 / 合并机制（已读）**：
- 子 agent 以 `role:assistant status:completed` 的 `message` 收尾（其 `output_text` 即调研报告）。实测子 jsonl 共 41 行：首事件=任务(user)，尾事件=报告(assistant completed)。
- 子会话的最终答案作为父侧 **`function_call_result`（name:"Agent", status:"completed）的 `output`** 回传（实测 `output:{type:"text", text:"已完成对 … 的探索。以下是结构与技术栈结论…"}`）。
- 父据此把多个子 agent 报告综合成最终回复。

**反推实现**：
- **manager/worker 模式**：顶层 agent 用 `Agent` 工具把子任务派给**专用子 agent 类型**（Explore/Plan 等，仿 Claude 的子 agent 族），子 agent 在隔离会话里独立跑（可带自己的工具集、只读约束），结果以工具返回值形式上卷。
- 全程事件溯源 + `parentId` / `sessionId` / `cwd` 关联：父的 function_call、子的会话、trace 的 span 树，靠这些指针串成可追溯的层级（与 §3.2 的 span `parentId` 一致）。
- 子会话独立落盘（`subagents/agent-<id>.jsonl`）→ 支持子 agent 的单独回放 / 调试 / 计费。

---

## 4. 一次 Agent 运行的端到端串联

```
用户输入（Electron renderer, app/）
  └─ 本地后端转发云端 copilot.tencent.com（local_storage 解码得域名）
       └─ 云端 LLM 推理（models.json 选模型）
            ├─ 多 Agent 编排（§3.9）：顶层 agent=cli 调 Agent 工具
            │    └─ 派生 subagents/agent-<id>.jsonl（如 Explore 只读勘察）
            │         └─ 子报告经 function_call_result(output) 上卷
            └─ 工具调用经 MCP 网关（connectors/ .mcp.json → 127.0.0.1:49157）
                 ├─ 内置工具：Bash/Read/Write/Edit（沙箱子进程，logs/sandbox 留痕）
                 ├─ 技能：skills/ 装载 SKILL.md
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

---

## 5. 工程取舍（从目录/格式反推）

1. **事件溯源而非 CRUD**：对话用追加 jsonl，天然支持回放/恢复/审计（代价：体积增长 → `projects/` 141M）。
2. **大对象 spill 出主日志**：工具产物落 `tool-results/`、图片落 `blobs/`，主 jsonl 只存引用，避免单文件膨胀。
3. **工具协议标准化为 MCP**：远端能力 = streamable-http MCP server，本地聚合代理扇出；连接器状态用 aes-256-gcm 信封加密 + HKDF 派生密钥 + 迁移标记状态机。
4. **轻量追踪 vs 完整事件流互补**：traces 只记计时/成败，jsonl 记完整内容，二者解耦。
5. **预览与真实磁盘分离**：`workspace/` 虚拟 FS 让"Agent 改文件"对用户零风险直到确认；`file-history/` 再兜底可回退。
6. **配置即迁移状态机**：连接器状态 + SQLite 都用版本/标记追踪升级，把"向后兼容"当一等公民。
7. **哈希链审计 + 跨端同步**：把"企业级合规"和"多端一致"做进数据层（审计哈希链 + SMH/COS 同步库）。
8. **多 Agent 层级编排**：顶层 `Agent` 元工具派发专用子 agent（Explore 等），子会话独立落盘、结果经 function_call_result 上卷；manager/worker 模式。

---

## 6. 待验证 / 未决问题

- [x] `workbuddy.db` 表结构（automations 带 wechat/wecom 推送、session_usage 带 credit_json）。
- [x] `shell-snapshots/` 真实性质（**修正**：zsh shell-integration 引导脚本，非环境快照）。
- [x] `traces/` span schema（轻量 span 树，类型含 Bash/Read/Write/Edit/Skill/mcp_tools）。
- [x] `projects/*.jsonl` 事件风格（OpenAI Responses API 风 + 多 agent）。
- [x] `file-history/` 版本内容（全量快照 @vN）。
- [x] `local_storage/` 解码（gzip+base64，露出 copilot.tencent.com）。
- [x] MCP proxy 扇出（本地 HTTP 网关 127.0.0.1:49157）。
- [x] `connector-states.v3.json` 加密格式（AES-256-GCM 信封 + HKDF-SHA256，scheme/salt/keyCheck；accountIdentityKey 为命名空间非密钥）。
- [x] `edge-sync-mapping*.db` 真实表结构（edge_sync_mapping / artifact_cache / image_mapping 三表）。
- [x] 子 agent 派发/合并协议（`Agent` 元工具 + `subagent_type` + `subagents/` 转录 + `function_call_result` 回传）。
- [ ] 子 agent 允许哪些 `subagent_type`（Explore/Plan/…）与并行度控制规则（[推]）。
- [ ] 子 agent 是否可再嵌套子 agent（多层 manager/worker）（[推]）。
- [ ] 连接器加密信封的设备密钥来源（keychain / 派生自 device-id？）（[推]）。
- [ ] `edge_sync_artifact_cache` / `image_mapping` 为空：跨端同步默认关闭还是按会话触发？（[推]）。

---

## 7. 修正记录

- **v0.2→v0.3**：`shell-snapshots/` 用途由"每次命令前环境快照"**修正**为"注入 zsh 的 shell-integration 引导脚本（vcs_info/precmd/preexec 等 hook）"，依据为实际读取文件内容。原推断错误（误把 188K 体积当成环境全量快照）。
- **v0.3→v0.4**：补齐 §6 三个未决项实证——① 连接器加密实为 AES-256-GCM 信封 + HKDF-SHA256（scheme/salt/keyCheck/userIdCheck/createdAt），`accountIdentityKey` 是 `userId||enterprise` 命名空间而非密钥；② `edge-sync-mapping.db` 升为 `[实]`，含 `edge_sync_mapping`/`artifact_cache`/`image_mapping` 三表，揭露跨端同步走 SMH+COS；③ 新增 §3.9，用真实父/子 jsonl 实锤 manager/worker 多 Agent 协议（`Agent` 元工具 + `subagent_type` + `function_call_result.output` 上卷）。

## 8. 修订历史

- 2026-09-11 v0.1：建立框架、目录地图、待验证清单。
- 2026-09-11 v0.2：批量取证 projects/traces/tool-results/artifact-index/shell-snapshots/file-history/audit-log/local_storage/workbuddy.db/memory/experts/skills/sessions。
- 2026-09-11 v0.3：补齐 MCP 网关、db 真实表数据、完整 span schema、子 agent 与 skill 结构；修正 shell-snapshots 推断；填充全部子系统章节与端到端串联图。
- 2026-09-11 v0.4：实证 §6 三个未决项（连接器加密信封、edge-sync 三表、多 Agent 编排协议 §3.9）；目录表补 `subagents/` 行；修正记录与未决清单同步更新。
