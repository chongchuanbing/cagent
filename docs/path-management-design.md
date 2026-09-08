# cagent 统一路径管理体系设计

> 背景：会话 `d37199f9…` 中，Agent 执行 `tencent-pptx` 技能时按 SKILL.md 的相对链接去读
> `.workbuddy/skills/tencent-pptx/references/create-from-scratch.md`（不存在），靠 `find` 才定位到
> 真实路径 `./cagent/skills/tencent-pptx/…`。根因不是某个技能写错，而是**框架对"路径"没有统一
> 抽象**——正文里的相对路径没有可信的解析基座，模型只能瞎猜。本设计给出通用 Agent 的路径管理方案。

---

## 1. 现状问题（证据）

### 1.1 路径接触点散落，无单一事实来源

| 路径概念 | 当前来源 | 位置 |
|---|---|---|
| 工具沙箱根 `work_dir` | `os.getcwd()`，客户端层散落注入 | `clients/cli/main.py:81` → `setdefault("work_dir", …)` |
| 数据存储根 `data_dir` | 旧 `Config.data_dir=".data"`（占位实现，未接线） | `cagent/utils/config.py:18` |
| 技能目录 `skills.dirs` | `SkillsConfig.dirs=["cagent/skills"]` | `cagent/config/schema.py:115` |
| 插件目录 `plugins_dir` | `../../../cagent/plugins` 三层 `dirname` 跳算 | `clients/cli/main.py:91` |
| 配置目录 | `ConfigProvider` 默认 `.` / 各端自定 | `cagent/config/provider.py` |
| 会话/记忆落盘 | `storage.LocalFileStorage(root)` 相对 `.data` | `cagent/storage/local.py` |

- **两套 Config 并存**：`utils/config.py:Config`（旧、占位）与 `config/schema.py:AgentConfig`（新）职责重叠；`AgentConfig` 里**既无 `work_dir` 也无 `data_dir`**，二者游离在统一配置之外。
- `plugins_dir` 用 `dirname(__file__)` 三次上跳计算，脆弱、不可配、跨端难复用。

### 1.2 安全校验三处重复实现

| 校验点 | 位置 | 逻辑 |
|---|---|---|
| `PathGuard.safe_path` | `cagent/tools/builtin/_guard.py:16` | `realpath` + `startswith(work_dir)` |
| `SkillDefinition.resolve_file` | `cagent/plugins/skill_loader.py:47` | `realpath` + `startswith(skill_dir)` |
| `LocalFileStorage._resolve` | `cagent/storage/local.py` | `os.path.join(root, key)`，**无越界校验** |

同一份 "realpath 归一 + 前缀校验 + 符号链接防护" 写了三遍，形态还不一致；storage 干脆漏了校验。

### 1.3 对 LLM 不友好（直接触发本次 bug）

- `SkillGuideTool.run` 只回 SKILL.md 正文（`skill_executor.py:49`），不告知技能根目录；正文里的 `references/…`、`skills/tdoc-orchestrator/SKILL.md` 是相对链接，模型**没有可信基座**可解析。
- 模型输出路径形态混合：`work_dir` 相对、`data_dir` 相对、`skill_dir` 相对，无统一约定 → 只能按平台先验猜 `.workbuddy/skills/`。
- 工具 observation 直接回真实绝对路径（如 `/Users/chongcb/…/cagent/skills/…`），既冗长又泄漏机器目录结构。

---

## 2. 设计目标与原则

1. **单一事实来源**：所有根目录集中在一个 `PathSpace`，全局取用，杜绝散落拼接。
2. **逻辑路径 / 物理路径分离**：模型只接触紧凑、稳定的**逻辑路径**（URI scheme）；物理绝对路径只在框架内部流动。
3. **安全校验集中一处**：`PathSpace.assert_safe()` 是所有文件访问的唯一闸门。
4. **对 LLM 友好**：模型用 `scheme://` 或 `./相对` 表达路径；技能正文相对链接被重写；返回路径自动 relativize。
5. **端无关**：纯逻辑核心不读 `os.getcwd()` / `__file__`；`base_dir` 由端（cli/web）注入。

---

## 3. 核心抽象：`PathSpace`

新增 `cagent/runtime/paths.py`（纯逻辑，无 IO 副作用、可单测）。

### 3.1 挂载点（Mount）

```python
@dataclass
class Mount:
    name: str                 # 逻辑名，如 "workspace" / "data" / "skills:tencent-pptx"
    physical: Path            # 归一化后的绝对物理路径（沙箱根）
    expose_to_llm: bool       # 是否允许模型直接引用（self:// 等设为 False）
    sandbox: bool             # 是否校验越界（默认 True）
```

标准挂载点：

| scheme | 物理 | 对 LLM | 说明 |
|---|---|---|---|
| `workspace://` | `work_dir` | ✅（默认） | 工具读写主战场、沙箱根 |
| `data://` | `data_dir` | ⚙️ 可选 | 框架私有（sessions/memory），默认隐藏 |
| `skills://<name>/` | `skills_dir/<name>` | ✅ | 技能内 references/scripts/assets |
| `plugins://` | `plugins_dir` | ❌ | 框架内部 |
| `config://` | `config_dir` | ❌（读配置用） | 端上配置 |
| `self://` | cagent 包目录 | ❌ | 读自身 prompts/schema |

### 3.2 API

```python
class PathSpace:
    def __init__(self, base_dir: Path, mounts: dict[str, Mount])

    # 物理解析：识别 scheme://；否则当作相对路径，默认归 workspace://
    def resolve(self, uri_or_rel: str, default_mount: str = "workspace") -> Path

    # 模型输入归一：./相对、裸相对、绝对都收敛到物理路径
    def resolve_user_input(self, s: str, default_mount: str = "workspace") -> Path

    # 物理路径 → 给模型的逻辑路径（优先 workspace://，其次其它 mount；兜底绝对）
    def relativize(self, abs_path: Path) -> str

    # 唯一安全闸门：realpath 后校验归属于某个允许前缀；符号链接也先 realpath
    def assert_safe(self, path: Path, allowed: Iterable[str]) -> Path

    # 给模型看的安全显示（脱敏机器前缀）
    def as_display(self, abs_path: Path) -> str
```

- `resolve` 与 `assert_safe` 组合 = 所有文件系统入口的标准姿势：`p = space.assert_safe(space.resolve(uri))`。
- `relativize` 用于工具 observation / 返回值，避免向模型泄露 `/Users/chongcb/…`。

---

## 4. 与 LLM 的路径契约（解决 `.workbuddy` 猜测）

这是修复本次 bug 的关键，需要三处协同：

1. **System prompt 注入"路径速查卡"**（新增模板片段，组装进 `Agent`）：
   ```
   路径约定：
   - 用户项目根用 workspace://  例：workspace://cagent/skills/tencent-pptx/references/x.md
   - 技能内文件用 skills://<技能名>/  例：skills://tencent-pptx/references/create-from-scratch.md
   - 引用相对路径时，以 workspace:// 为基准；不要臆测 .workbuddy/skills 等未声明路径
   ```
2. **`skill_guide` / `run_skill` 链接重写**：返回前把正文里的
   `references/create-from-scratch.md` / `./skills/tdoc-orchestrator/SKILL.md`
   重写为 `skills://<name>/references/create-from-scratch.md`（`skill_executor.py:49`、`131`）。
   或新增 `read_skill_file(skill_name, rel)` 工具，走 `PathSpace.resolve("skills://…")`，模型不再手拼。
3. **工具 observation 走 `relativize`**：filesystem / shell 返回的文件路径以 `workspace://…` 呈现。

---

## 5. 配置收敛

新增 `PathSpaceConfig`（并入 `AgentConfig`，删除旧 `utils/config.Config`）：

```python
class PathSpaceConfig(BaseModel):
    base_dir: str = "."          # 所有相对目录的解析基准，由端注入（cli: os.getcwd()）
    work_dir: str = "."          # → workspace://
    data_dir: str = ".data"      # → data://
    skills_dirs: List[str] = ["cagent/skills"]
    plugins_dir: str = "cagent/plugins"
    config_dir: str = "config"
    expose_data_to_llm: bool = False
```

- 所有相对目录在构造 `PathSpace` 时相对 `base_dir` 解析为绝对（修复 `../../../cagent/plugins` 跳算）。
- 单 Mount 列表由 `PathSpaceConfig` 一次性生成，全局注入 `Agent` 与其依赖组件。

---

## 6. 工具层统一接入

- 所有文件/目录类工具（filesystem、shell、skill 读取、MCP 文件操作）参数先过
  `space.resolve_user_input`，返回过 `space.relativize`。
- 删除 `PathGuard.safe_path` 与 `SkillDefinition.resolve_file` 的重复实现：
  保留为**薄封装**或直接替换为 `space.assert_safe(space.resolve(...))`。
- `StorageBackend._resolve` 补越界保护（统一走 `assert_safe`，`data://` 沙箱）。

---

## 7. 安全模型

- `assert_safe` 统一：`os.path.realpath` 归一 → 校验归属某 mount 的允许前缀集合。
- **符号链接**：先 `realpath` 再校验，防 symlink escape 越出沙箱。
- **沙箱隔离**：`workspace://` 与 `data://` 互为独立沙箱；模型默认无法用 `data://` 越到 `workspace://` 或反之。
- `self://` / `plugins://` / `config://` 默认 `expose_to_llm=False`，模型引用即报错，防止探针框架内部。

---

## 8. 实施路线图

| Phase | 内容 | 验收 |
|---|---|---|
| 0 | 新增 `cagent/runtime/paths.py`（`PathSpace`/`Mount`）+ 单测（解析、relativize、越界、symlink） | 单测全绿 |
| 1 | `PathSpaceConfig` 并入 `AgentConfig`；删除旧 `utils/config.Config`；`base_dir` 由 cli/web 注入 | 配置收敛、无散落注入 |
| 2 | `filesystem`/`shell`/`skill_loader`/`storage` 改为统一 `assert_safe(resolve(...))` | 三处校验合并为入口 |
| 3 | `skill_guide` 链接重写 + system prompt 速查卡 + observation `relativize` | 复跑 d37199f9，不再出现 `.workbuddy/skills` |
| 4 | cli/web 接入 `base_dir`；回归历史会话 | 跨端一致 |

---

## 9. 风险与取舍

- **逻辑路径选 URI scheme 而非纯 `pathlib`**：scheme 对 LLM 自解释、可校验命名空间；物理 path 仅在框架内流动。二者双轨，对外只暴露 scheme。
- **向后兼容**：旧 `trace.jsonl` 里的绝对路径仍可被 `relativize` 处理（按 `base_dir` 匹配前缀转 `workspace://`），不影响历史会话恢复。
- **`data://` 默认隐藏**：避免模型误把 `.data` 当作可写工作区；确有需要再开 `expose_data_to_llm`。

---

## 10. 覆盖性审视：PathSpace 能 / 不能 / 管不到

> 对 §3 方案的批判性自检。结论：**PathSpace 能覆盖"框架自有资源的解析与沙箱"，但有几类情况要么需修正、要么超出其边界**。

### 10.1 ✅ 能覆盖（原设计已含）
- 单一 work_dir 内的文件/目录读写、相对路径归一、符号链接越界防护。
- 框架自有 skills/plugins/config/self 的逻辑命名与沙箱隔离。
- 工具 observation 路径回写给模型时的脱敏（relativize）。
- 历史会话恢复（相对路径按 `base_dir` 匹配前缀转 `workspace://`）。

### 10.2 ⚠️ 需修正才能覆盖（设计补丁）
1. **Shell 命令体路径（不是真空）**：`ShellExecutor` 已有 cwd 限制 + `_check_command_paths` 命令体绝对路径正则检查（`shell_exec.py:55-60`）。PathSpace 不应另写"单参数 resolve"，应提供 **`scan_and_resolve_command(cmd)`**：扫描命令体所有 `/` 开头 token，逐个 `resolve`，区分"系统命令白名单（/usr/bin/* 等）"与"用户目标路径"，再校验。即**收编**现有 shadow 实现，统一到 `assert_safe`。
2. **技能内"相对当前文件"的多级解析**：`tencent-docx` 用 `./skills/tdoc-orchestrator/SKILL.md`，相对的是*当前 SKILL.md 所在目录*，不是技能根；内嵌子技能是两层（`skills://tencent-docx/skills/tdoc-orchestrator/`）。§3.2 的"默认 `default_mount=workspace`"在 skill 上下文是错的。→ 引入 **resolver scope**：每次解析携带"当前 mount/base"，`skill_guide`/`run_skill` 返回时把裸 `references/x.md` 绑定到 `skills://<name>/`，而非全局默认 workspace。
3. **沙箱 vs 合法越界**：只读访问项目外（如汇总 `/Users/me/Desktop/x.pdf`）、用户故意用 symlink 把外部目录挂进项目（`./data -> /external/bigdata`）会被纯沙箱误伤。→ 增加 **受信外挂区 `allow_paths` + `allow_symlink_targets`**，沙箱校验改为"归属任一允许前缀（work_dir / data_dir / allow_paths）"，而非仅 work_dir。
4. **per-mount 读写执行三态权限**：`self://`/`plugins://`/`config://` 应**只读**，workspace/data 可写，某些需可执行。§3.1 只有 `expose_to_llm` 与 `sandbox`，缺 `read/write/exec` 枚举。
5. **per-run 冻结快照**：`base_dir` 必须固化进 `session.meta`，resume 时复用；多 Agent 并发各自 work_dir 不同 → **PathSpace 必须 per-run/per-agent 实例，禁止全局单例**。§5 只说"base_dir 由端注入"，未强调冻结与并发。
6. **实现用 pathlib 而非 os.path**：§3 示例用 `os.path.join`，跨平台（Windows `C:\`、分隔符、大小写）应统一 `pathlib.Path` / `PurePosixPath`，避免 sep 拼接。
7. **URL / 远程引用分流**：模型可能给 `https://…`、`s3://…`。PathSpace 应识别并**分流**到专门 fetch，不进本地沙箱；scheme 机制天然支持，但需显式"可解析性分流"判断。

### 10.3 🚫 架构硬边界（PathSpace 管不到，需其他机制）
1. **技能脚本内部 `open()`**：`pptx2image.py`、`build_tokens.py` 自己 `expanduser().resolve()` 读绝对路径，框架无法拦截工具*内部*的 IO。→ 这是契约边界：框架提供 `SKILL_DIR` 环境变量 + `relativize` 库供脚本调用，强制"文件 IO 经框架 IO 代理"只能靠约定/规范，不能靠 PathSpace 拦截。
2. **MCP 资源的自有沙箱**：MCP 服务器暴露自己的文件系统/远程资源，路径是其协议内部概念，框架 wrap 不到 `run_tool` 参数里的路径（schema 千变万化）。→ PathSpace **只管框架自有资源**；MCP 资源靠 MCP 协议自身 sandbox，框架最多注册 `mcp://<server>/` 仅作*展示别名*。
3. **模型"幻觉路径"**：模型编造不存在的路径（`/Users/xxx/.workbuddy/...`），PathSpace 能拒绝越界，但无法判断"存在但猜错"。→ 由 `skill_guide` 链接重写 + 速查卡从源头减少；越界/不存在由 `assert_safe` + `exists` 兜底报错，交给 ReAct 重试。

### 10.4 修正后的边界一句话
**PathSpace = "框架自有资源（workspace/data/skills/plugins/config/self + 受信外挂区）的解析、沙箱、逻辑视图"；命令体路径走 `scan_and_resolve_command` 收编现有 shell 沙箱；脚本内部 IO 与 MCP 资源不在其内，由契约/协议负责。**

### 10.5 修订后的 Phase 清单
- Phase 2 拆细：2a 收编 shell 命令体扫描；2b 接入 allow_paths/symlink 策略；2c per-mount 权限。
- Phase 3 增加：resolver scope（skill 上下文默认 skills://）+ URL 分流。
- Phase 0 单测增加：命令体扫描、受信外挂区、多级技能相对解析、Windows 路径。

### 10.6 实现进度（已落地）

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 0 | `cagent/runtime/paths.py`（PathSpace + Mount，frozen，per-mount rw/exec、allow_paths/symlink、command_path_violations、classify、relativize 最具体 mount 优先、resolver scope） | ✅ |
| Phase 2a | ShellExecutor 接入 PathSpace，收编 `_check_command_paths`（未注入回退兼容） | ✅ |
| Phase 3 | 根因修复：`read_skill_file` 工具 + SKILL.md 链接重写 `skills://<name>/...` + 路径速查卡贯穿 prompt（Agent→ReActEngine→build_react_system_prompt→path_card） | ✅ |
| Phase 1 | 配置收敛：`PathSpaceConfig` 并入 `AgentConfig`（`paths` 段）；`ConfigProvider.build_path_space()` 为唯一构造入口，`data_dir` 唯一来源（显式覆盖 > `paths.data_dir`，热加载生效）；旧 `cagent/utils/config.py`（两套 Config 并存）已删除 | ✅ |
| Phase 4 | storage/`_resolve` 收编 PathSpace 统一沙箱；`SkillDefinition.resolve_file` 与 PathSpace 合并；端上（web/desktop）复用 | ✅ |

Phase 3 直接消除 `d37199f9…` 会话的 `.workbuddy/skills/` 误读：模型读技能参考文档改用 `read_skill_file`，不再 shell `cat` 猜路径。

Phase 1 后路径事实来源收敛为一条链：

```
config/agent.yaml (paths:)
   └─ ConfigProvider.build_path_space(base_dir, plugins_dir, config_dir)
        └─ PathSpace（frozen，per-run）
             ├─ workspace:// / data:// / self:// / plugins:// / config://
             ├─ Agent.path_space → ReActEngine → path_card（注入 system prompt）
             └─ ShellExecutor（收编命令体路径沙箱）
```

约定：`skills://<name>/...` 是给模型的逻辑约定，由 `read_skill_file` 经
`SkillDefinition.resolve_file` 承接，**不在 PathSpace 注册挂载点**——避免与
「mount 名即 scheme」的解析规则冲突。

Phase 4 收编成果（`storage/_resolve` 与 `SkillDefinition.resolve_file` 统一走
`PathSpace.assert_safe`，消除第三处散落校验）：

```
config/agent.yaml (paths:)        ← data_dir 唯一来源：显式覆盖 > paths.data_dir
   └─ ConfigProvider.build_path_space()
        └─ PathSpace（frozen，per-run）
             ├─ Agent.path_space → ReActEngine → path_card（注入 system prompt）
             ├─ ShellExecutor（命令体沙箱，2a 收编）
             ├─ Storage（get_storage 注入，root 取 data:// 挂载点；_safe 再经 assert_safe）
             └─ SkillDefinition.resolve_file(..., path_space)（ReadSkillFileTool 注入）
```

端上（CLI）三处 `get_storage`（`_build_agent` / `cmd_sessions_list` / `_memory_store`）
均已注入 `path_space`，且 `build_path_space` 的 data 挂载点优先使用 CLI `--data-dir`
覆盖值，避免 `--data-dir` 与 `data://` 两处配置漂移。
