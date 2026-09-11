# WorkBuddy 插件隐藏目录配置逆向（`*.plugin` 的 `.codebuddy-plugin` / `.workbuddy-plugin`）

> 本文聚焦 `plugins/cache/<source>/<name>/<version>/` 下两个隐藏目录中的 **`plugin.json`**（以及 source 级 `marketplace.json`）配置本身，逐字段拆解其语义与内部实现。
> 配套文档：`workbuddy-plugin-system.md`（插件系统全局设计）、`workbuddy-plugin-types.md`（8 类资源形态）。
> 证据全部来自真实文件读取，标注 `[实]`；推断标注 `[推]`。

---

## 0. 一句话结论

每个插件的隐藏目录里**有且仅有 `plugin.json` 一个配置文件**（source 级 `marketplace.json` 另算），别无二级配置。这份 `plugin.json` 是整个插件系统的**单一事实源（manifest）**，它不直接塞代码，而是通过三类机制描述"能力在哪、怎么加载"：

1. **路径声明**（`skills/commands/agents/hooks` → 相对路径或路径数组）→ loader 去磁盘按约定读取；
2. **`workbuddy` 扩展块** → 描述资源类型（`kind`）、在 app 包内的真实位置（`bundleSegments`）、运行时依赖（`dependencies`）；
3. **`mcpServers` 启动描述符** → 一条完整的 MCP stdio server 拉起规格（含 `defer_loading` 懒加载）。

---

## 1. 两个隐藏标记 = 两种资源性质（核心发现）

| 标记 | 用于哪类插件 | 语义 `[推]` | 典型字段 |
|---|---|---|---|
| `.workbuddy-plugin` | interactionmode-*(4)、welcomemode-*(3)、prompt-common(1) | **纯提示/根 agent 资源，不带可执行代码** | 仅 name/version/description/author/category/keywords，+ `workbuddy.dependencies`(welcomeMode) |
| `.codebuddy-plugin` | skill-*、tencent-*、sheetagent、mcp-ardot、weixinpay、agent-browser、find-skills | **承载可执行能力（skill 代码 / MCP server / agent 文件）** | 上述 + skills/commands/agents/hooks/mcpServers + `workbuddy.kind` |

**关键推断**：标记名本身编码了资源性质——`.workbuddy-plugin` = "只动提示层（workbuddy prompt）"，`.codebuddy-plugin` = "带 code/runtime 能力（codebuddy = 带代码）"。这印证了 WorkBuddy 是 **CodeBuddy 的 rebrand**：`.codebuddy-plugin` 是旧 CodeBuddy 时代的遗留标记，`.workbuddy-plugin` 是新原生 plugin 层的标记。

实证：agent-browser 描述写 "teaches **CodeBuddy** to use agent-browser"，weixinpay 写 "Weixin Pay integration for **CodeBuddy Code**"——这些 `.codebuddy-plugin` 插件是从 CodeBuddy 迁移过来的。

---

## 2. `plugin.json` 字段级拆解

### 2.1 标准元数据字段（两类标记共有）

| 字段 | 含义 | 备注 |
|---|---|---|
| `name` | 插件 ID | 与目录名一致；`name@source` 是全局唯一键 |
| `version` | 版本 | 形态多样：semver(`0.1.0`) / 时间戳(`0.1.1784877812`) / 日期(`v20260727`) / 营销号(`1.6.107`) |
| `description` | 描述 | 部分带 `description_en`（i18n） |
| `author` | 作者 | `{name, email, url}` |
| `category` | UI 分组 | 混合中英文：interaction / template / skill / mcp-app / 开发工具 / 支付 / welcomeMode / 办公协作 |
| `keywords` | 搜索词 | 数组 |

### 2.2 能力路径声明（仅 `.codebuddy-plugin` 出现）

这些值**不是内联能力，而是相对路径/路径数组**，loader 按约定去磁盘读取：

| 字段 | 形态 | 示例 | 含义 |
|---|---|---|---|
| `skills` | 字符串目录 `"./skills"` / 数组 `["./skills/x",…]` / 空 `[]` | sheetagent=`"./skills"`；tencent-docx=10 个显式路径 | 指向 SKILL.md 所在目录或文件 |
| `commands` | 同 skills | sheetagent=`"./commands"`；tencent-docs-plugin=`[]` | 命令定义 |
| `agents` | 字符串/数组 | welcomemode-code=`["agents/code.md"]`；tencent-docx=3 个 `.md` | 根 agent / 子 agent 定义文件 |
| `hooks` | 字符串路径或空串 | sheetagent=`"./hooks/hooks.json"`；tencent-docs-plugin=`""` | hook 注册表（实测为 0 字节占位 → 能力声明未填充） |

**重要实证**：`tencent-docs-plugin` 显式声明 `skills:[], commands:[], agents:[], hooks:""`——空数组/空串表示"我是路由容器，真实 skill 是子资源"，与 `tencent-docx`/`tencent-pptx` 的实路径声明形成对照。

### 2.3 `mcpServers` 启动描述符（MCP 类插件）

仅 `sheetagent` 出现完整形态：

```json
"mcpServers": {
  "sheetagent": {
    "command": "node",
    "args": ["${CODEBUDDY_PLUGIN_ROOT}/mcp/start.mjs"],
    "env": {
      "SHEETAGENT_MCP_TRANSPORT": "stdio",
      "SHEET_MCP_DEBUG": "0",
      "SHEETAGENT_LOG_LEVEL": "info",
      "SHEET_API_MODE": "local",
      "SHEET_REMOTE_MCP_URL": "https://docs.qq.com/api/v6/sheet/mcp"
    },
    "defer_loading": true
  }
}
```

- `command`+`args`：stdio server 拉起命令；`${CODEBUDDY_PLUGIN_ROOT}` 是运行时变量，解析为插件安装根（`cache/<source>/<name>/<version>`）。
- `env`：双后端开关——`SHEET_API_MODE: local` 走本地 Node server，`SHEET_REMOTE_MCP_URL` 走远端 `https://docs.qq.com/api/v6/sheet/mcp`。
- `defer_loading: true`：MCP server **首次工具调用时才启动**（懒加载），非进程启动即拉起。

### 2.4 `workbuddy` 扩展块（隐藏目录真正的"私有配置"）[实]

三类形态，是区分资源加载方式的关键：

#### 形态 A：`builtin-skill`（19 个 skill-*）
```json
"workbuddy": {
  "kind": "builtin-skill",
  "legacyResourceName": "ardot-design-core",
  "bundleSegments": ["plugins","workbuddy-builtin","skills","ardot-design-core"]
}
```
- `kind: builtin-skill`：声明这是"内置 skill 资源"。
- `legacyResourceName`：**迁移遗留名**——说明这些 skill 早先是 bake 进 app 的 "resource"，现抽象成 plugin；名字保留以便向后兼容。
- `bundleSegments`：一段**相对 app 包的路径分段**，拼接后指向 `/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-plugins/skills/ardot-design-core`。
  → **实证 `cache/<name>/<version>/` 是安装镜像，真实 SKILL.md/代码在 .app 包内**，loader 经 `bundleSegments` 解析回真身。

#### 形态 B：`builtin-mcp-app`（1 个 mcp-ardot-mcp-app）
```json
"workbuddy": {
  "kind": "builtin-mcp-app",
  "legacyResourceName": "ardot-mcp-app",
  "bundleSegments": ["plugins","workbuddy-builtin","mcps","ardot-mcp-app"],
  "runtimeSupportSegments": [["plugins","workbuddy-builtin","mcps","ardot-mcp-app","_workbuddy-runtime","mcp-app-bootstrap.cjs"]],
  "loadsCapabilities": false
}
```
- 比 skill 多了 `runtimeSupportSegments`（bootstrap 入口）与 `loadsCapabilities:false`（不自动注册 MCP 能力，按需加载）。
- 解释了前一文发现：`_workbuddy-runtime/mcp-app-bootstrap.cjs` 在 cache 里是 0 字节桩，真 server 在 app 包内（`bundleSegments` 指向）。

#### 形态 C：`welcomeMode.dependencies`（3 个 welcomemode-*）
```json
"workbuddy": {
  "dependencies": [{"type": "mcp", "name": "netdrive"}]
}
```
- `kind` 缺失，代之以 `dependencies`：**声明外部依赖**。welcomeMode 根 agent 要求一个名为 `netdrive` 的 MCP server 必须可用（启动前解析依赖）。
- 这是插件间依赖解析机制的实证——loader 在加载 welcomeMode 前先确保 `netdrive` MCP 已就绪。

---

## 3. source 级 `marketplace.json`（随 .app 分发）

`workbuddy-builtin/.codebuddy-plugin/marketplace.json` 是**内置市场清单**，随 Desktop 分发（非用户安装）：

```json
{
  "name": "workbuddy-builtin",
  "owner": {"name": "WorkBuddy", "email": "workbuddy@tencent.com"},
  "metadata": {
    "version": "1.0.0",
    "generatedAt": "2026-08-08T02:03:21.116Z",
    "sourceMarketplaces": ["builtin-plugins"],
    "sourceRoots": ["/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-plugins"]
  },
  "plugins": [
    {"name":"weixinpay","source":"./weixinpay/1.5.111","version":"1.5.111","category":"支付"},
    {"name":"tencent-docs-plugin","source":"./tencent-docs-plugin/1.0.0","version":"1.0.0","category":"办公协作"},
    {"name":"tencent-pptx","source":"./tencent-pptx/v20260712","version":"v20260712"}
  ]
}
```

关键推断 `[推]`：
- `sourceRoots` 直接点明内置插件**物理驻留在 .app 包内**（asar.unpacked）——与 §2.4 的 `bundleSegments` 完全吻合，**彻底证实 cache 是镜像**。
- 仅列 3 个"头条"内置插件（generatedAt 快照），而 cache 有 34 个——说明 cache 是**安装/更新后的实时态**：例如 marketplace 里 weixinpay=`1.5.111`，cache 里已是 `1.6.107`，证明 cache 在分发后被更新过。
- `source: "./weixinpay/1.5.111"` 指向包内版本目录；loader 用 `sourceRoots`+`source` 定位真身。

---

## 4. 加载流程推断 [推]（基于配置形状）

```
读取 installed_plugins.json（物理安装清单，含 installPath）
  └─ 进 cache/<source>/<name>/<version>/.{code|work}buddy-plugin/plugin.json
       ├─ 解析 marker：
       │    .workbuddy-plugin → 纯提示资源，读 fragments/agents/prompt.tpl 拼系统提示
       │    .codebuddy-plugin → 能力资源，继续：
       ├─ 解析 workbuddy 块：
       │    kind=builtin-skill/mcp-app → 用 bundleSegments 解析到 .app 包内真身（SKILL.md/代码/mcp）
       │    welcomeMode.dependencies → 先解析依赖 MCP（如 netdrive）再加载根 agent
       ├─ 解析 skills/commands/agents/hooks：按路径去磁盘读 SKILL.md/agent.md/hook 注册表
       ├─ 解析 mcpServers：注册 MCP 启动描述符（defer_loading 者首次调用才拉起）
       └─ 由 settings.json.enabledPlugins 决定<name>@<source> 是否进入运行时（仅 7/34 启用）
```

---

## 5. 配置维度速查表

| 维度 | 取值/形态 | 出现位置 |
|---|---|---|
| 标记 | `.codebuddy-plugin` / `.workbuddy-plugin` | 每插件目录名 |
| `category` | interaction / template / skill / mcp-app / 开发工具 / 支付 / welcomeMode / 办公协作 | 元数据 |
| `workbuddy.kind` | builtin-skill / builtin-mcp-app / (无, welcomeMode 用 dependencies) | 扩展块 |
| `legacyResourceName` | 旧 resource 名 | 仅 builtin-* |
| `bundleSegments` | app 包内路径分段数组 | 仅 builtin-* |
| `runtimeSupportSegments` | bootstrap 入口 | 仅 builtin-mcp-app |
| `loadsCapabilities` | bool | 仅 builtin-mcp-app |
| `dependencies` | `[{type:mcp,name}]` | 仅 welcomeMode |
| `skills/commands/agents` | 路径串 / 路径数组 / `[]` | 能力型 |
| `hooks` | 路径串 / `""` | 能力型 |
| `mcpServers` | `{name:{command,args,env,defer_loading}}` | 仅 sheetagent（完整） |
| `defer_loading` | bool | mcpServers 内 |

---

## 6. 推断（待后续验证）

1. `enabledPlugins` 对 `skill-*` 是否全量加载——`plugin.json` 含 `workbuddy.kind:builtin-skill` 但不在启用表 7 个内；结合 §4，"被动能力"可能不经该闸门。需读 loader 源码确认。
2. `bundleSegments` 解析时 cache 内文件（如 `skill-*/SKILL.md`）与 bundle 内真身是否 double-load——推测 loader 优先 bundle，cache 仅作 manifest 镜像。
3. `dependencies` 的 MCP（`netdrive`）定义驻留何处——应在 `connectors/` 或 app bundle 内某个 mcp 注册表，未在 plugins 树内。
4. `marketplace.json` 仅 3 条而 cache 34 条，其余 31 条是否也来 self builtin 但未被市场清单收录（走目录扫描发现）——待比对 `sourceRoots` 目录实际内容。

---

*文档版本 v0.1 — 聚焦隐藏目录 `plugin.json`/`marketplace.json` 配置本身。所有字段均来自真实文件读取。*
