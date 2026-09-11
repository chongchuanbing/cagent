# WorkBuddy 插件系统逆向设计（plugins/ 目录考古）

> 本文基于 `~/.workbuddy/plugins/` 目录的**真实文件内容**反推 WorkBuddy 的插件系统设计。
> 所有结论均附带证据路径，推断处标注 `[推]`、实证处标注 `[实]`。
> 配套总文档：见 `workbuddy-internals-investigation.md`（§3 工具/能力系统一节）。

---

## 0. 结论速览

WorkBuddy 的插件系统是一个**「市场分发 + 安装布局 + 能力声明 + 提示组合」四层解耦**的架构：

- **插件 = 一个带 `name@source` 身份、按版本落盘的"能力包"**。
- 一个插件可声明多种**能力**（`skills` / `commands` / `agents` / `hooks` / `mcpServers` / `lspServers`），框架按能力类型把插件拆进不同子系统。
- **存在两套 manifest 标记**：`.workbuddy-plugin/plugin.json`（WorkBuddy 原生 mode/prompt 类，瘦 schema）与 `.codebuddy-plugin/plugin.json`（CodeBuddy 血脉能力类，全 schema）——标记名编码**产品血统**，但二者都是 `plugin.json`。
- **"安装"与"启用"分离**：`installed_plugins.json` 记物理安装（34 个），`settings.json.enabledPlugins` 才是运行时闸门（仅 7 个 `true`）。
- **系统提示是拼出来的**：`welcomemode` 选根 agent + 模板 → `prompt.tpl` 用 Jinja 按 `workMode` `{% include %}` `interactionmode-*` 的 `fragments/*.md` → `prompt-common` 注入全局底座。agent loop 行为、工具白名单、结果投递协议**全部来自插件片段**。

---

## 1. 目录拓扑

```
~/.workbuddy/plugins/
├── installed_plugins.json        # [实] 安装清单（schema v2）：name@source → 版本/路径/时间
├── known_marketplaces.json       # [实] 市场目录缓存（124KB）：3 个官方市场的 manifest
├── cache/                        # [实] 安装布局（扁平化）：<source>/<name>/<version>/ 真实包
│   ├── workbuddy-builtin/        #   来源=内置市场（32 个插件）
│   │   ├── skill-*  (24)         #   skill 能力包
│   │   ├── interactionmode-* (4) #   交互模式（plan/ask/craft/expert）
│   │   ├── welcomemode-* (3)     #   欢迎模式（code/work/design）
│   │   ├── prompt-common         #   全局共享提示底座
│   │   ├── mcp-ardot-mcp-app     #   builtin MCP app 资源
│   │   └── tencent-*/sheetagent/weixinpay  # 重能力插件（docs/pptx/docx/sheet/微信支付）
│   └── codebuddy-plugins-official/  # 来源=官方市场（2 个：agent-browser, find-skills）
├── marketplaces/                 # [实] 各市场的本地 checkout（分发布局，按类型分组）
│   ├── codebuddy-plugins-official/  # zip 市场：plugins/(206 目录)/external_plugins/dist/.zip-metadata.json/.marketplace-version
│   ├── cb_teams_marketplace/        # zip 市场：scenes.json(场景编目)/rules-standard.md/plugins/
│   ├── workbuddy-builtin/           # directory 市场：skills/mcps/builtin-plugins/interactionmode/welcomemode/prompt-common
│   └── my-experts/                  # [实] 用户本地市场：.codebuddy-plugin/marketplace.json + plugins/<专家>/
└── data/                         # 空（预留运行时数据）
```

**关键二分**：
- `marketplaces/<name>/` = **分发布局**（官方市场按 `plugins/<name>/` 扁平，内置市场按 `skills|mcps|builtin-plugins|interactionmode|welcomemode|prompt-common` 分组）。
- `cache/<source>/<name>/<version>/` = **安装布局**（所有来源统一扁平为 `source/name/version`）。
- 即：市场是"发布形态"，cache 是"安装形态"，`source` 字段把两者桥接。

---

## 2. 插件身份与清单（plugin.json）

### 2.1 两种 manifest 标记 `[实]`

| 标记 | 使用者 | schema 胖瘦 | 代表插件 |
|---|---|---|---|
| `.workbuddy-plugin/plugin.json` | `interactionmode-*` / `welcomemode-*` / `prompt-common` | 瘦（name/version/desc/author/category/keywords + 可选 `agents` + `workbuddy.dependencies`） | plan/craft/code |
| `.codebuddy-plugin/plugin.json` | `skill-*` / `mcp-*` / `tencent-*` / `sheetagent` / `weixinpay` / `agent-browser` | 全（含 skills/commands/agents/hooks/mcpServers/workbuddy 扩展块） | 全部能力类 |

> 标记名是**产品血统戳**：`.workbuddy-plugin` = WorkBuddy 原生"模式/提示"被动插件；`.codebuddy-plugin` = CodeBuddy 血脉"主动能力"插件。二者解析逻辑一致，仅字段集不同。

### 2.2 瘦 schema 样本（interactionmode-plan）`[实]`

```json
{ "name":"interactionmode-plan","version":"0.1.0",
  "description":"WorkBuddy plan interaction mode fragment and tool policy.",
  "author":{"name":"WorkBuddy"},"category":"interaction",
  "keywords":["workbuddy","interaction","plan"] }
```

### 2.3 全 schema 样本（welcomemode-code）`[实]`

```json
{ "name":"welcomemode-code","version":"0.1.7",
  "description":"WorkBuddy code welcomeMode root agent and resources.",
  "author":{"name":"WorkBuddy"},"category":"welcomeMode",
  "keywords":["workbuddy","welcomeMode","code"],
  "agents":["agents/code.md"],
  "workbuddy":{"dependencies":[{"type":"mcp","name":"netdrive"}]} }
```

### 2.4 全 schema 样本（sheetagent，含 mcpServers）`[实]`

```json
{ "name":"sheetagent","version":"0.1.1784877812",
  "description":"由腾讯文档团队出品的电子表格智能助手…",
  "author":{"name":"Tencent Docs SheetAgent Team","url":"https://docs.qq.com"},
  "keywords":["spreadsheet","excel","xlsx","tencent-docs"],
  "commands":"./commands","skills":"./skills","hooks":"./hooks/hooks.json",
  "mcpServers":{"sheetagent":{
    "command":"node","args":["${CODEBUDDY_PLUGIN_ROOT}/mcp/start.mjs"],
    "env":{"SHEETAGENT_MCP_TRANSPORT":"stdio","SHEET_API_MODE":"local",
           "SHEET_REMOTE_MCP_URL":"https://docs.qq.com/api/v6/sheet/mcp"},
    "defer_loading":true}} }
```

要点：`${CODEBUDDY_PLUGIN_ROOT}` 是路径变量注入；`defer_loading:true` 表示 MCP server **懒启动**（首次调用才拉起）。

### 2.5 builtin-mcp-app 特殊块（mcp-ardot-mcp-app）`[实]`

```json
"workbuddy":{"kind":"builtin-mcp-app","legacyResourceName":"ardot-mcp-app",
  "bundleSegments":["plugins","workbuddy-builtin","mcps","ardot-mcp-app"],
  "runtimeSupportSegments":[["plugins","workbuddy-builtin","mcps","ardot-mcp-app",
                              "_workbuddy-runtime","mcp-app-bootstrap.cjs"]],
  "loadsCapabilities":false}
```

`bundleSegments`/`runtimeSupportSegments` 指向**应用安装包内的 app bundle**（不是本地脚本），`_workbuddy-runtime/mcp-app-bootstrap.cjs` 实测为 0 字节桩——server 实体在 app bundle 里，`loadsCapabilities:false` 表示它不直接贡献工具表。

---

## 3. 插件能力模型（7 种类型）

按 `category` / 目录前缀 / 声明字段，插件分为 7 类 `[实]+[推]`：

| 类型 | 前缀/标识 | 声明字段 | 载体目录 | 注入方式 |
|---|---|---|---|---|
| **skill** | `skill-*` | `skills`/`commands` | `SKILL.md` + `references/` `scripts/` `templates/` | Skill 工具按名加载（被动） |
| **interactionmode** | `interactionmode-*` | `category:"interaction"` | `fragments/*.md` | 系统提示 include（按 workMode） |
| **welcomemode** | `welcomemode-*` | `agents` + `category:"welcomeMode"` | `prompt.tpl` `settings.json` `agents/*.md` | 选根 agent + 模板 |
| **mcp-app** | `mcp-*` / `tencent-*` / `sheetagent` | `mcpServers` 或 `workbuddy.kind:"builtin-mcp-app"` | `mcp/` 或 app bundle | 拉起 MCP server 子进程 |
| **plugin(通用)** | `tencent-docs-plugin` / `weixinpay` | `skills`/`commands`/`agents`/`hooks` | 混合 | 能力包（可含 connector 注册） |
| **lsp** | catalog `lspServers` | `lspServers`（仅目录 manifest） | 语言服务器 | 代码智能（远程按需） |
| **prompt-common** | `prompt-common` | — | `fragments/*.md` | 全局底座（常驻） |

**能力字段并集**（来自 `known_marketplaces.json` 的 206 条 plugin 级 schema）`[实]`：
`author, category, description, description_en, homepage, keywords, license, lspServers, name, repository, source, strict, tags, version`。
→ 注意 `lspServers`（语言服务器）、`strict`、`tags` 是目录级（远程 catalog）字段，本地 plugin.json 未必全有。

**hooks.json 实证为空** `[实]`：`sheetagent`/`tencent-pptx` 的 `hooks/hooks.json` 均为 0 字节——能力已声明但未填充（保留扩展位）。

---

## 4. 提示组合管线（核心逆向）`[实]`

系统提示不是写死的，是**三层插件片段拼装**出来的。证据来自 `welcomemode-code/prompt.tpl` 与 `interactionmode-*/fragments/*`：

### 4.1 管线步骤

```
① welcomemode 插件决定根 agent
   agents/code.md:
     ---
     name: code
     description: WorkBuddy code welcomeMode root agent.
     ---
     {% include "welcomemode-code/prompt.tpl" %}

② prompt.tpl 是 Jinja 模板（变量：modelId/modelName/workMode/
   productFeatures.*/ResponseLanguage/dataFolderName/productName）
   {% if workMode=="plan" %}{% include "interactionmode-plan/fragments/interaction.md" %}
   {% elif workMode=="ask" %}{% include "interactionmode-ask/fragments/interaction.md" %}
   ... {% endif %}

③ interactionmode-<mode>/fragments/*.md 注入五段：
   - agent-loop.md       → <agent_loop> 主循环行为（可插拔！）
   - interaction.md      → <前置 YAML: tools:[Read,Write,Bash,...]> + 交互策略（工具白名单！）
   - tool-use.md         → 工具使用政策
   - result-presentation.md → <result_presentation>/<sharing_files>（present_files 协议！）
   - current-mode.md     → 当前模式说明（plan 模式实测为空占位）

④ prompt-common/fragments/*.md 常驻底座：
   - workbuddy-memory-system.md  → 记忆系统指令
   - plugin-recommendation.md    → 插件推荐指令
   - memory-context.md           → 记忆上下文注入
```

### 4.2 关键推论 `[实]`

- **agent loop 行为是可插拔的**：`<agent_loop>` 段来自 `interactionmode-*/fragments/agent-loop.md`，切换模式即切换 loop 策略。
- **工具白名单按模式策略化**：`interaction.md` 的 YAML `tools:` 决定该模式可用工具集（plan 模式启用 Read/Write/Edit/Glob/Grep/Bash/PowerShell/Task*）。
- **结果投递协议来自片段**：`result-presentation.md` 即系统提示里的 `<result_presentation>`/`present_files` 规则——所以"每任务结尾必须 present_files"是插件注入的，不是硬编码。
- **四种模式片段组合不同** `[实]`：
  - plan:   agent-loop, interaction, interaction-tool-use, result-presentation, current-mode(空), tool-use
  - craft:  agent-loop, interaction, interaction-tool-use, result-presentation, current-mode, tool-use
  - ask:    agent-loop, interaction, mode-behavior, result-presentation, current-mode, tool-use
  - expert: agent-loop, interaction, result-presentation, current-mode, tool-use

---

## 5. 安装 / 启用 / 市场 三层状态 `[实]`

### 5.1 `installed_plugins.json`（安装清单，schema v2）

```json
{ "version":2,
  "plugins":{
    "welcomemode-code@workbuddy-builtin":[
      {"scope":"user",
       "installPath":"/Users/chongcb/.workbuddy/plugins/cache/workbuddy-builtin/welcomemode-code/0.1.7",
       "version":"0.1.7","workbuddySeedManaged":true,
       "installedAt":"2026-08-24T07:38:33.463Z","lastUpdated":"2026-08-24T07:38:33.463Z"}],
    "agent-browser@codebuddy-plugins-official":[ {... "version":"1.3.0","workbuddySeedManaged":false} ]
  }}
```

- 键 = `<name>@<source>`（source = 市场名）。
- 值 = **数组**（支持同一插件多版本并存）。
- `workbuddySeedManaged:true` = 随客户端 seed 预装的内置插件（共 32 个）；`false` = 用户显式安装（agent-browser/find-skills）。
- **此处无 enabled 字段** → 安装 ≠ 启用。

### 5.2 `settings.json.enabledPlugins`（运行时闸门）

```json
"enabledPlugins":{
  "weixinpay@workbuddy-builtin":true, "tencent-docs-plugin@workbuddy-builtin":true,
  "tencent-pptx@workbuddy-builtin":true, "agent-browser@codebuddy-plugins-official":true,
  "find-skills@codebuddy-plugins-official":true, "sheetagent@workbuddy-builtin":true,
  "tencent-docx@workbuddy-builtin":true }
```

- **仅 7/34 为 `true`** `[实]`。
- 启用的全是"主动型"：起 MCP server（sheetagent/tencent-*）、注册连接器（weixinpay）、注入 agent/浏览器（agent-browser/find-skills）。
- 推断 `[推]`：`interactionmode-*`/`welcomemode-*`/`prompt-common` 与多数 `skill-*` **不经此闸门**——它们是框架常驻/被动能力（skill 经 Skill 工具按名加载，mode 经提示组合加载），故无需 enable 开关。

### 5.3 `known_marketplaces.json`（市场目录缓存）

```json
{ "codebuddy-plugins-official":{
    "type":"zip","isBuiltIn":true,"autoUpdate":true,
    "installLocation":"/Users/chongcb/.workbuddy/plugins/marketplaces/codebuddy-plugins-official",
    "description":"CodeBuddy 官方插件市场",
    "manifest":{"name":...,"owner":...,"plugins":[ /* 206 条 */ ]}},
  "cb_teams_marketplace":{"type":"zip","isBuiltIn":true,"autoUpdate":true,
    "manifest":{"name","description","description_en","owner","metadata","plugins":[...]}},
  "workbuddy-builtin":{"type":"directory","isBuiltIn":true,"autoUpdate":false,
    "installLocation":".../marketplaces/workbuddy-builtin"} }
```

- 3 个官方市场有 `manifest`（plugin 级目录）；`workbuddy-builtin` 是本地 `directory` 市场，**无远程 manifest**（直接读 `marketplaces/workbuddy-builtin/` 目录）。
- 用户市场 `my-experts` **不在此文件**（在 `marketplaces/my-experts/.codebuddy-plugin/marketplace.json` 自描述）。

---

## 6. 运行时锁 `.in_use/<pid>` `[实]`

- 会拉起子进程的插件（`sheetagent`/`tencent-docs-plugin`/`agent-browser`/`prompt-common`）在 `<version>/.in_use/` 下，每活一个进程写一个**以 PID 命名的空文件**。
- 实测 `agent-browser/1.3.0/.in_use/` 含 `45635,58291,40618,29960,17663,45638,26077,40614,58288,8444` 等 10 个 PID 锁；`prompt-common/0.1.2/.in_use/` 含 `9233,58291,68492,1910`。
- **进程退出即删锁**（复验时部分锁已随 app 退出消失）。→ 这是"插件实例占用"的轻量锁，用于防重复拉起 / 健康探测。

---

## 7. 市场分发模型 `[实]`

| 市场 | type | 同步 | 本地形态 |
|---|---|---|---|
| `codebuddy-plugins-official` | zip | autoUpdate | `plugins/`(206 包) + `external_plugins/`(远程分类) + `dist/`(构建产物) + `.zip-metadata.json` + `.marketplace-version` |
| `cb_teams_marketplace` | zip | autoUpdate | `scenes.json`(场景编目) + `rules-standard.md` + `plugins/` + `.zip-metadata.json` |
| `workbuddy-builtin` | directory | 否 | `skills/mcps/builtin-plugins/interactionmode/welcomemode/prompt-common` |
| `my-experts` | 用户本地 | 否 | `.codebuddy-plugin/marketplace.json` + `plugins/<专家>/` |

- **zip 市场下载校验** `.zip-metadata.json`：
  ```json
  {"url":"https://download.codebuddy.cn/plugin-marketplace/codebuddy-plugins-official-<uuid>.zip",
   "lastModified":"Tue, 25 Aug 2026 11:14:00 GMT","etag":"\"86983cb9...\"",
   "downloadedAt":"2026-09-10T09:36:08.732Z","installComplete":true}
  ```
  → 用 etag/lastModified 做增量校验，`.marketplace-version`(UUID) 做版本戳（对应 `plugin-marketplace-state-new/.marketplace-version-*` + `.periodic-marketplace-check` 周期检查）。
- **`external_plugins/`** = 远程托管、按需下载的第三方插件目录（`accessibility-compliance`/`agent-orchestration`/`agents-*/`…），不在本地 cache。
- **`scenes.json`（teams）** = 场景化编目：`{id, unified_id, name, icon, mode, interactionModes:[craft,plan], plugins:[{name, marketplaceName}], prompts:[...], promptTitles:[...], target}`——驱动"推荐场景"UI（场景→插件→示例 prompt）。
- **用户市场 manifest** `my-experts/.codebuddy-plugin/marketplace.json`：
  ```json
  {"name":"my-experts","description":"my-experts marketplace (auto-generated)",
   "plugins":[{"name":"xiaohongshu-content-creator","source":"./plugins/xiaohongshu-content-creator",
               "description":"An AI expert specializing in…"}]}
  ```
  → 你的自定义专家 `xiaohongshu-content-creator` 就是以"用户市场插件"形态落地（对应 `experts/custom/<uid>/experts.json` 的 `["xiaohongshu-content-creator"]`）。

> 市场 manifest 文件 `.codebuddy-plugin/plugin.json` 对**市场本身**是 0 字节占位标记；真实市场元数据在 `known_marketplaces.json`（官方）或 `marketplace.json`（用户）。

---

## 8. 版本与生命周期

- **版本格式多样** `[实]`：semver(`0.1.0`)、构建时间戳(`0.1.1784877812` = epoch ms ≈ 2026，sheetagent 自动构建)、日期标签(`v20260727`)、营销号(`1.6.107` 微信支付)。
- **多版本并存**：`installed_plugins.json` 值为数组，支持同插件多版本 side-by-side（升级不立即删旧版）。
- **更新链路** `[推]`：zip 市场按 etag 增量拉取 → 解压到 `marketplaces/<name>/` → 安装到 `cache/<source>/<name>/<newver>/` → 更新 `installed_plugins.json.lastUpdated` + `settings.json`（若启用）。`plugin-marketplace-state-new` 的周期检查标记驱动自动更新。

---

## 9. 完整数据模型（字段级速查）

| 文件 | 关键字段 | 含义 |
|---|---|---|
| `cache/<src>/<name>/<ver>/.codebuddy-plugin/plugin.json` | name,version,description,author,category,keywords,skills,commands,agents,hooks,mcpServers,workbuddy | 能力包声明 |
| 同上 `.workbuddy-plugin/plugin.json` | name,version,description,author,category,keywords,agents?,workbuddy.dependencies? | 瘦声明（mode/prompt） |
| `installed_plugins.json` | version, plugins[`<name>@<src>`][]{scope,installPath,version,installedAt,lastUpdated,workbuddySeedManaged?} | 安装态 |
| `settings.json` | enabledPlugins{`<name>@<src>`:bool} | 启用闸门 |
| `known_marketplaces.json` | `<mk>`{type,isBuiltIn,autoUpdate,installLocation,manifest{plugins[]}} | 市场目录缓存 |
| `marketplaces/<mk>/.zip-metadata.json` | url,lastModified,etag,downloadedAt,installComplete | zip 下载校验 |
| `marketplaces/<mk>/.marketplace-version` | `<uuid>` | 市场版本戳 |
| `marketplaces/my-experts/.codebuddy-plugin/marketplace.json` | name,description,plugins[]{name,source,description} | 用户市场自描述 |
| `marketplaces/cb_teams_marketplace/scenes.json` | [{id,name,mode,interactionModes,plugins,prompts}] | 场景编目 |
| `<ver>/.in_use/<pid>` | (空文件) | 运行时进程锁 |
| `welcomemode-*/prompt.tpl` | Jinja: modelId/workMode/ResponseLanguage/… + `{% include %}` | 根提示模板 |
| `interactionmode-*/fragments/*.md` | agent-loop/interaction(tools:)/tool-use/result-presentation/current-mode | 系统提示片段 |

---

## 10. 设计推演总结（工程取舍）

1. **能力包而非功能开关**：插件用"声明能力字段"（skills/commands/agents/hooks/mcpServers/lspServers）描述自己，框架按字段路由到对应子系统——新增能力类型只需新增一种声明字段，无需改宿主。
2. **市场/安装/启用三层解耦**：市场管"从哪来"，安装管"落盘"，启用管"运行"。内置插件 seed 预装但可禁用；用户市场独立自描述。
3. **提示可组合**：系统提示 = 底座(prompt-common) + 模式片段(interactionmode) + 欢迎模板(welcomemode)，全靠 Jinja `include` 拼装。agent loop、工具白名单、结果协议都是插件片段 → **行为可插件化**。
4. **产品血统双标记**：`.workbuddy-plugin` vs `.codebuddy-plugin` 并存，说明 WorkBuddy 在 CodeBuddy 插件生态上演进，原生 mode 类走瘦 schema、旧血脉能力类走全 schema，向后兼容。
5. **MCP 即插件运行时**：重能力插件（docs/pptx/sheet/微信）本质就是"声明一个 MCP server + 懒启动 + PID 锁"——和 §3 连接器生态同源（都是 MCP 协议）。
6. **轻量进程锁**：`.in_use/<pid>` 用空文件做占用标记，零依赖、进程退出即失效，适合桌面端单用户场景。
7. **目录 vs 数据库**：插件元数据用 JSON 文件（非 SQLite，区别于 automations/settings 用 `workbuddy.db`）——插件是"文件即真理"，便于符号链接开发态直连（如 `~/.workbuddy/skills/` 链接到 `git_workspace/agent-skills`）。

---

## 待验证清单

- [ ] `enabledPlugins` 对 `skill-*` 的默认加载规则：是框架全量加载，还是 Skill 工具懒加载？（当前按"被动能力不经闸门"推断）
- [ ] `workbuddy.dependencies`（`welcomemode-code` → mcp:netdrive）的实际解析：依赖未安装时如何处理？
- [ ] `hooks.json` 为空，hooks 机制的触发点与 schema 尚未实证。
- [ ] `lspServers` 插件的本地安装形态与生命周期（catalog 有声明，本地 cache 未见实例）。
- [ ] `external_plugins/` 的按需下载触发条件与落盘位置。
- [ ] `dist/` 构建产物与 `plugins/` 源包的关系（是否 dist 为主运行态）。
