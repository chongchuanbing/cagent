# 系统级沙箱设计文档（ExecutionBackend 抽象 + 平台沙箱）

> 版本：v1.1（2026-09-07，含实施落地）
> 模块位置：`cagent/runtime/sandbox/`（新增）、`cagent/plugins/shell_exec.py`（改造）
> 相关配置：`config/agent.yaml` 的 `sandbox:` 段（新增）
> 前置文档：`docs/path-management-design.md`（PathSpace）、`docs/design/tool-system-v2.md`（shell 驱动执行）
>
> **实施状态**：Phase 0-2 已实现（`cagent/runtime/sandbox/` + `tests/test_sandbox.py`，
> 25 个单测全过，全量 280 项回归通过）。与 v1 设计的差异见 §10。

## 1. 背景与定位

### 1.1 现状

shell 工具当前的安全模型是**应用层软沙箱**（`ShellExecutor` 的 7 层校验链）：

1. 黑名单子串匹配（`blocked_patterns`，默认空）
2. cwd 白名单（钉在会话 scratch 或空间根）
3. 重定向目标检查（`_redirect_violation`）
4. `cd` 漂移拦截
5. 命令体绝对路径扫描（`PathSpace.command_path_violations`）
6. 环境变量过滤
7. 超时 + 输出截断

最终执行是 `subprocess.run(shell=True)`——**同用户、同权限的子进程**。这层校验能拦截 LLM 的"无心之失"（拼错路径、误写系统目录），但拦不住"有意绕过"（base64 编码命令、命令替换 `$()`、通过解释器间接执行）。

### 1.2 目标

叠加**系统级权限层**（业界分级中的第 2 层），作为校验层之下的兜底防线：

- 即使正则校验被绕过，操作系统层面也拒绝越权读写与网络访问；
- 跨平台：macOS（Seatbelt）/ Linux（bubblewrap / 低权限用户）；
- **不替换现有校验层**：校验层产出 LLM 可自纠错的 `error_kind` + `hint`，系统级是纵深防御的第二道墙。

### 1.3 非目标

- 不做容器/微 VM（第 3 层）——留给后续演进；
- 不做远程云沙箱（第 4 层）；
- 不覆盖框架代码执行的工具（`read_file` 等）——它们同进程运行且已走 `PathSpace.assert_safe` 唯一闸门。

## 2. 核心思路：PathSpace 是逻辑权限模型，系统沙箱是它的物理投影

`PathSpace` 已经完整描述了"谁可以读写哪里"：每个 `Mount` 有 `physical`（物理路径）+ `modes`（read/write/exec）+ `sandbox`（是否受沙箱约束）。

**本设计不引入第二套权限描述。** 沙箱规则由 profile 生成器从 `PathSpace` 编译产生，单一事实源，避免规则漂移：

```
PathSpace.mounts ──编译──> 平台沙箱 profile ──> sandbox-exec / bwrap
     (逻辑权限)                (物理投影)
```

例：`session://` 挂载点（每会话 scratch，可写）编译为 seatbelt 的
`(allow file-write* (subpath "<scratch物理路径>"))`；只读的 `workspace://` 编译为 `file-read*` allow；其余 `deny file-write*` 全拒。

cagent 的两个天然优势使这条路成本很低：

1. **网络已天然隔离**：内置联网工具走主进程 `httpx`，不经 shell。`deny network*` 禁掉 shell 的网络不影响正常 web 工具，无需打洞。
2. **会话 scratch 已存在**：`runtime/session_scope.py` 的每会话目录就是现成的唯一可写区，直接作为沙箱的写白名单根。

## 3. 架构设计

### 3.1 执行后端抽象

从 `ShellExecutor` 中抽出可替换的执行后端。校验链留在 `ShellExecutor`（执行前），后端只负责"怎么跑"：

```
ShellExecutor.execute(command)
  ├─ 现有 7 层校验（不变）         ← 应用层，产出 error_kind + hint，LLM 可自纠
  └─ ExecutionBackend.run(cmd, cwd, timeout)   ← 系统层，兜底防线
       ├─ LocalBackend      # 现状：subprocess.run(shell=True)，默认
       ├─ SeatbeltBackend   # macOS：sandbox-exec -f profile.sb
       ├─ BwrapBackend      # Linux：bubblewrap 命名空间隔离
       └─ UserBackend      # 低权限用户降权：runuser / sudo -u
```

接口（`cagent/runtime/sandbox/base.py`）：

```python
class ExecutionBackend(ABC):
    def prepare(self, scope: SessionScope) -> None: ...
        # 每会话/每次 run 前置：生成 profile、chmod scratch 属主等

    def wrap(self, command: str, cwd: str) -> list[str]: ...
        # 把命令包装为最终 argv（如 ["sandbox-exec", "-f", p, "-c", cmd]）

    def cleanup(self, scope: SessionScope) -> None: ...
        # 删除临时 profile 等

    def available(self) -> bool: ...
        # 平台能力探测（seatbelt 存在？bwrap 在 PATH？）
```

### 3.2 错误路径打通（关键设计）

系统级拦截的失败（exit code 1、permission denied 输出）必须映射回 `ToolResult(error_kind="SANDBOX", hint=...)`，而非裸透传。否则 ReAct 循环拿到的是神秘崩溃，无法自纠。

`_classify_exit_code` 扩展：识别各后端的拒绝特征（seatbelt 返回特定日志、bwrap 的 mount 失败消息），归一为同一 `error_kind`。hint 统一话术："命令被系统沙箱拒绝，可写区域为会话目录 session://，写 workspace:// 请使用文件工具"。

### 3.3 profile 生成器

`cagent/runtime/sandbox/profile_gen.py`：

```python
def build_rules(path_space: PathSpace, scope: SessionScope) -> SandboxRules:
    """从 PathSpace 编译出与平台无关的中间规则，各后端再翻译。"""
    # writable  = mounts 中 sandbox=True 且 allows("write") 的 physical
    #           + scope.scratch
    # readable  = 所有 mounts 的 physical（含只读）+ SYSTEM_PREFIXES
    # network   = "deny"（配置项）
```

中间规则结构（`SandboxRules`）平台无关，`SeatbeltBackend` 将其渲染为 sbpl，`BwrapBackend` 渲染为 `--ro-bind` / `--bind` 参数。新增平台后端只需实现"规则 → 平台语法"的翻译。

### 3.4 macOS Seatbelt 后端

```bash
sandbox-exec -f /tmp/cagent-<session>.sb -c "<command>"
```

profile 骨架（由生成器产出）：

```scheme
(version 1)
(deny default)                                    ; 默认全拒
(allow file-read* (subpath "/usr") (subpath "/bin") ...)   ; 系统只读前缀
(allow file-read* (subpath "<workspace>"))        ; PathSpace 只读 mount
(allow file-write* (subpath "<scratch>"))         ; 会话唯一可写区
(allow process-exec (subpath "/usr/bin") ...)
(deny network*)                                   ; 默认禁网
```

参考实现：OpenAI Codex CLI 的 macOS 沙箱即此路线。

**已知风险**：`sandbox-exec` 是私有 API，无官方文档，macOS 大版本可能变动；profile 调试全靠试错。因此必须保留 LocalBackend 逃生门。

### 3.5 Linux bubblewrap 后端

优先 bubblewrap（Flatpak 同款，比手写 seccomp 简单一个量级）：

```bash
bwrap \
  --ro-bind /usr /usr --ro-bind /etc /etc \
  --proc /proc --dev /dev \
  --ro-bind <workspace> <workspace> \    # 只读 mount
  --bind <scratch> <scratch> \           # 唯一可写区
  --unshare-net \                        # 禁网
  --die-with-parent \
  -- <command>
```

降级方案：`UserBackend`——专用低权限用户 + sudoers 免密白名单
`sudo -n -u cagent-agent --`。属主问题需部署方预处理（共享组 / umask），
属实验性后端。

### 3.6 进程组管理（附带修复）

现状 `subprocess.run` 超时后杀不干净子进程树。后端统一改用：

```python
subprocess.Popen(..., start_new_session=True)   # os.setsid
# 超时时 os.killpg(pgid, SIGKILL)
```

## 4. 配置设计

新增 `SandboxConfig`（挂入 `AgentConfig`，走现有热加载机制——改配置不重启）：

```yaml
sandbox:
  mode: off              # off / auto / strict
                        #   off    = LocalBackend，现状（校验层仍在）
                        #   auto   = 按能力探测降级：bwrap → seatbelt → user → local（记警告日志）
                        #   strict = 探测失败则拒绝执行 shell（安全优先场景）
  network: deny         # deny / allow（allow 时 profile 放行网络）
  extra_readable: []    # 额外只读前缀（罕见场景，一般用 PathSpace 声明）
```

配置类：

```python
class SandboxConfig(BaseModel):
    mode: Literal["off", "auto", "strict"] = "off"
    network: Literal["deny", "allow"] = "deny"
    extra_readable: list[str] = Field(default_factory=list)
    user: Optional[str] = None   # UserBackend 专用低权限用户
```

**默认值选 `off` 而非 v1 设计的 `auto`（实施决策）**：Seatbelt 私有 API
在 macOS 大版本间行为漂移，且存在嵌套沙箱不可用场景（见 §10.2）；v1
先默认关闭，用户按场景显式开启，观察稳定后再考虑默认 auto。

## 5. 降级链与桌面端考量

```
bwrap 在 PATH？          → BwrapBackend
macOS 且 sandbox-exec？ → SeatbeltBackend
sudoers 已配置？         → UserBackend
否则                     → LocalBackend + 启动日志警告
```

**桌面端（Tauri）现实约束**：终端用户没有 sudo、发行版不带 bwrap，实际会落在 local 后端。这是可接受的——桌面端单用户场景下，第 1 层校验 + 每会话 scratch 隔离的风险敞口有限；Web/服务端部署才是本设计的主要受益场景。

## 6. 事件与审计

- 复用 `tool_result` 的 `error_kind="SANDBOX"` 通道（暂不新增事件类型，避免膨胀）；
- `emitter` 侧观察：若后续需要用户可见的"沙箱拦截"提醒（Web/桌面弹窗），再加 `SANDBOX_DENIED` 事件；
- profile 文件与会话绑定，随 `trace.jsonl` 记录使用的后端类型，便于事后审计。

## 7. 实施阶段

| 阶段 | 内容 | 交付 |
|---|---|---|
| **Phase 0** | 抽 `ExecutionBackend` 接口；LocalBackend 承接现状；进程组管理（setsid + killpg） | 无行为变化的纯重构，测试全绿 |
| **Phase 1** | `profile_gen.py` + `SeatbeltBackend` + SandboxConfig | macOS 可用，`mode: auto` 生效 |
| **Phase 2** | `BwrapBackend` + `UserBackend` + 降级链探测 | Linux 可用 |
| **Phase 3** | strict 模式、错误分类打磨、审计完善、文档 | 收尾 |

每阶段独立可发布，随时可回退到 LocalBackend。

## 8. 已识别的坑

1. **Seatbelt 私有 API**：无文档、可能随 macOS 版本变动、profile 调试全靠试错——必须保留逃生门 + `available()` 探测。
2. **低权限用户写文件**：scratch 属主问题在 `prepare()` 解决；跨用户的共享目录用组权限。
3. **shell 环境差异**：seatbelt/bwrap 内的 `$PATH`、locale 可能与用户 shell 不同，导致命令"找不到"——profile 需放行 `/usr/bin:/bin`，并在拒绝 hint 里提示。
4. **绕过面仍存在**：bwrap 未启用时，编码命令仍可绕过正则层。本设计把爆炸半径从"整个用户目录 + 网络凭证"缩到"沙箱内"，但不宣称完全防御。

## 9. 与现有模块的关系

| 模块 | 关系 |
|---|---|
| `runtime/paths.py` | 规则单一事实源，`Mount.modes` 编译为沙箱规则；`assert_safe` 语义不变 |
| `plugins/shell_exec.py` | 校验链不动，执行段委托 backend |
| `runtime/session_scope.py` | scratch 目录即沙箱写白名单根 |
| `config/schema.py` | 新增 `SandboxConfig`，热加载生效 |
| `events/` | 沿用 `error_kind="SANDBOX"`，暂不新增事件类型 |

## 10. 实施落地记录（v1.1）

### 10.1 与 v1 设计的差异

1. **接口简化**：未实现 `prepare()/cleanup()` 生命周期——seatbelt 用
   `-p` 内联 profile（无临时文件）、bwrap 无状态、user 无状态，后端
   均为无状态包装器；`wrap_argv(command, scope, path_space)` 按次编译规则。
2. **runtime 不依赖 config**：`SandboxSettings`（dataclass）定义在
   `runtime/sandbox/base.py`，端上把 pydantic `SandboxConfig` 转换后注入，
   保持 runtime 下层纯净。
3. **进程组管理落在统一运行器** `run_argv()`（`start_new_session` +
   `killpg`），所有后端共享，超额修复原 `subprocess.run` 超时杀不干净
   子进程树的问题（有回归测试覆盖）。
4. **默认 mode=off**（见 §4 实施决策）。

### 10.2 实测发现：嵌套沙箱不可用

macOS 上若宿主进程**本身已处于 Seatbelt 沙箱内**（如 IDE 的 Bash 工具、
托管 CI），`sandbox-exec` 应用任何 profile 都会失败：

```
sandbox-exec: sandbox_apply: Operation not permitted   (exit 71)
```

该失败发生在 profile **语法解析通过之后**（解析错误是 exit 65），说明
是 macOS 拒绝嵌套 `sandbox_init`。已做处理：

- `_classify_exit_code` 识别 `sandbox_apply` 特征 → `error_kind="SANDBOX"`
  + 明确 hint（提示宿主环境已沙箱化，建议 mode=off 或原生终端运行）；
- 此环境下无法端到端验证 seatbelt 写拦截，单测覆盖到 profile 渲染与
  argv 包装层；真实拦截效果需在原生终端冒烟（用例见 §10.3）。

### 10.3 sbpl 语法实测（macOS 26 / darwin 25）

- `mach-lookup*` / `sysctl*` / `file-map-read-only` 通配符不存在 →
  用具体操作名 `mach-lookup` / `sysctl-read` / `sysctl-write`，
  mmap 放行用 `file-map-executable`；
- `file-read*` / `file-write*` / `process*` / `network*` 通配符有效。

### 10.4 文件清单

```
cagent/runtime/sandbox/
├── __init__.py        # select_backend 降级链（bwrap → seatbelt → user → local）
├── base.py            # ExecutionBackend / LocalBackend / SandboxSettings / run_argv
├── profile_gen.py     # SandboxRules + build_rules（PathSpace → 平台无关规则）
├── seatbelt.py        # macOS sandbox-exec 后端
├── bwrap.py           # Linux bubblewrap 后端
└── user.py            # 低权限用户降权后端（实验性）

cagent/config/schema.py    # SandboxConfig（mode/network/extra_readable/user）
cagent/plugins/shell_exec.py  # backend 注入 + run_argv + SANDBOX 错误映射
clients/cli/main.py        # 端上接线（配置 → SandboxSettings → select_backend）
tests/test_sandbox.py      # 25 个单测
```
