# AGENTS.md

> 本文件描述 `cagent` 项目的定位、技术栈与目录结构，供开发智能体（AI coding agent / 协作者）快速理解项目并遵循约定进行开发。

## 1. 项目简介

**cagent** 是一个用 Python 编写的轻量级 **Agent 运行框架**。

核心理念：将 **Plan-Executor（规划—执行）** 与 **ReAct（推理—行动）** 两种范式结合，形成两层驱动的 Agent 控制流：

- **上层 Plan-Executor**：`Planner` 根据目标生成结构化计划（`Plan` 由若干 `Step` 组成），`Executor` 按依赖关系顺序调度每个 `Step` 并管理状态。
- **下层 ReAct**：每个 `Step` 内部运行独立的 `Thought → Action(tool call) → Observation` 多轮循环，处理需要多次工具调用的复杂子任务，将结果收敛为该 `Step` 的结论，再回写给 `Planner`。

这种分层让"宏观规划"与"微观执行"解耦：Planner 不必关心单步内要调几次工具，ReAct 不必关心整体计划排布。

## 2. 技术栈

| 维度 | 选型 | 说明 |
|------|------|------|
| 语言 | Python >= 3.11 | 使用 `enum`、`dataclass` / `pydantic`、`asyncio`（可选异步） |
| 数据建模 | Pydantic v2 | `schema/` 下所有数据契约 |
| LLM 接入 | OpenAI 兼容 SDK | `llm/` 抽象层，默认 `openai`，可扩展其他供应商 |
| HTTP | `httpx` | 供应商请求与内置工具联网 |
| 工具系统 | 自研装饰器 `@tool` | 普通函数一键注册为 `Tool` |
| 配置 | `pydantic` / `yaml` | `config/`（端上可配置文件 + 热加载）；路径配置集中在 `config/schema.py` 的 `PathSpaceConfig`（`paths` 段） |
| 路径管理 | `pathlib` | `runtime/paths.py` 的 `PathSpace`（逻辑 `scheme://` 与物理挂载点分离，唯一安全闸门）；由 `ConfigProvider.build_path_space()` 构造，详见 `docs/path-management-design.md` |
| 日志 | 标准库 `logging` | `utils/logging.py` 分级（loop / react / tool） |
| 测试 | `pytest` | `tests/` 下 unit + integration |

> 依赖版本以 `pyproject.toml` 为准（待补充）。

## 3. 目录结构

```
cagent/
├── __init__.py
│
├── core/                      # ★ 核心 loop 引擎
│   ├── __init__.py
│   ├── agent.py               # 对外入口 Agent（组装所有组件）
│   ├── loop.py                # 主控制循环：编排 plan-executor + ReAct
│   ├── planner.py             # Plan-Executor：生成/修订计划（输出 Plan）
│   ├── executor.py            # Plan-Executor：按 Step 顺序调度执行
│   └── react.py               # ReAct 引擎：单步内的 Thought→Action→Obs 循环
│
├── schema/                    # 数据模型（pydantic）
│   ├── __init__.py
│   ├── message.py             # 对话消息、角色
│   ├── plan.py                # Plan / Step 定义
│   └── action.py              # Action（工具调用）、Observation、Thought
│
├── llm/                       # LLM 抽象层（解耦模型供应商）
│   ├── __init__.py
│   ├── base.py                # LLMClient 抽象接口
│   ├── openai.py              # OpenAI 兼容实现
│   └── ...                    # 其他供应商
│
├── tools/                     # 工具系统
│   ├── __init__.py
│   ├── base.py                # Tool 基类 / @tool 装饰器
│   ├── registry.py            # 工具注册与检索
│   └── builtin/               # 内置工具（calculator / ask_user）
│
├── memory/                    # 记忆管理
│   ├── __init__.py
│   ├── base.py                # Memory 抽象接口
│   ├── schema.py              # MemoryRecord / MemoryStatus（分代数据契约）
│   ├── short_term.py          # 工作记忆（当前任务上下文窗口）
│   ├── long_term.py           # 长期记忆双区存储（candidates/tenured）+ 标签加权召回
│   ├── extractor.py           # run 结束提取候选记忆 + 打标（1 次 LLM 调用）
│   ├── tenuring.py            # 晋升管理：去重合并 / age / 晋升 / 冲突覆盖 / TTL 淘汰
│   └── service.py             # MemoryService：召回 / 沉淀 / 快速晋升统一入口
│
├── events/                    # ★ 运行事件（标准化 + SSE 投递）
│   ├── __init__.py
│   ├── schema.py              # EventType 枚举 + AgentEvent(pydantic)
│   └── emitter.py             # EventEmitter（subscribe/emit/iter_sse）+ to_sse()
│
├── prompts/                   # 提示词模板
│   ├── planner_prompt.py      # 生成 Plan 的提示
│   └── react_prompt.py        # 单步 ReAct 提示（含工具描述）
│
├── utils/
│   ├── logging.py             # 统一日志
│   └── text.py                # sanitize_text / tokenize
│
├── runtime/                   # ★ 运行时基础设施
│   ├── paths.py               # PathSpace / Mount（路径空间，详见 docs/path-management-design.md）
│   ├── sandbox/               # ★ 系统级沙箱（详见 docs/design/sandbox-design.md）
│   │   ├── base.py            # ExecutionBackend 抽象 / LocalBackend / run_argv（进程组管理）
│   │   ├── profile_gen.py     # SandboxRules：PathSpace → 平台无关规则（单一事实源编译器）
│   │   ├── seatbelt.py        # macOS sandbox-exec 后端
│   │   ├── bwrap.py           # Linux bubblewrap 后端
│   │   └── user.py            # 低权限用户降权后端（实验性）
│   └── session_scope.py       # 会话路径作用域（ContextVar）：run 期间工具感知 scratch/空间根；
│                               # 无空间时 workspace:// 即会话 scratch；有空间（--space / paths.space_dir）
│                               # 时 workspace:// 挂空间根——结构化工具写入须显式 scheme 并登记
│                               # meta.workspace_writes 审计，shell cwd 钉 scratch、拦截重定向/cd 漂移写空间
│
├── storage/                   # 存储抽象层（本地文件）
│   ├── __init__.py            # get_storage / SessionRecorder
│   ├── base.py                # StorageBackend 抽象接口
│   ├── local.py               # LocalFileStorage（根目录 .data）
│   └── session.py             # SessionRecorder（落盘 meta/plan/trace）
│
└── config/                    # 端上配置与热加载
    ├── __init__.py
    ├── schema.py              # AgentConfig / ModelConfig / PathSpaceConfig
    └── provider.py            # ConfigProvider（yaml + 保存即生效）+ build_path_space()
```

> 顶层另含 `pyproject.toml`、`README.md`、`tests/`、`examples/`、`docs/`。

### 3.2 顶层多端接入层

核心库 `cagent/` 保持为**纯逻辑、不依赖任何 UI / CLI 框架**；各端作为独立入口，仅单向依赖 `cagent`：

```
clients/
├── cli/                       # 命令行客户端（argparse）✅ 已实现
│   ├── __init__.py
│   ├── __main__.py            # python -m clients.cli 入口
│   └── main.py                # 子命令：run / config(set/show) / sessions(list/show)
├── web/                       # Web 服务（FastAPI / Starlette）— 规划中
│   ├── app.py                 # ASGI 应用与 lifespan
│   ├── routes.py              # REST / WebSocket 路由
│   └── schemas.py             # 请求 / 响应模型
└── desktop/                   # 桌面端（PyQt6 / Tauri / Electron 壳）— 规划中
    ├── main.py                # 窗口 / 事件循环入口
    └── ui/                    # 界面层（与核心逻辑隔离）
```

## 4. 核心组件职责（速查）

| 模块 | 类 | 职责 |
|------|----|------|
| `core.agent` | `Agent` | 依赖注入组装各组件，对外暴露 `run(goal)` |
| `core.loop` | `AgentLoop` | 主循环：`plan → 逐 step 执行(内部 ReAct) → 汇总 → 必要时 replan` |
| `core.planner` | `Planner` | `plan(goal)` 产出 `Plan`；`replan()` 修订计划 |
| `core.executor` | `Executor` | `next_step()` 选步；`execute_step()` 委托 ReAct；`update()` 回写状态 |
| `core.react` | `ReActEngine` | 单 step 内多轮 `Thought→Action→Observation` 直到收敛；每 step 按配置裁剪工具集（`tools.enabled_groups` 分组过滤 + `tools.dynamic` 关键词 Top-K 选择），system prompt 只渲染工具目录（`[group] name: 描述`），完整 schema 走 function calling 声明 |
| `schema.*` | `Plan/Step/Action/Observation/Message` | 全局数据契约 |
| `llm.base` | `LLMClient`(ABC) | `complete()` / `complete_with_tools()` 归一化接口 |
| `tools.base` | `Tool`(ABC) / `@tool` | 工具基类与注册装饰器 |
| `tools.registry` | `ToolRegistry` | 注册、分组命名空间（`register(tool, group=...)`）、按组过滤（`list/schemas(groups)`）、动态 Top-K 选择（`select(query, k)`） |

## 5. 调用链

```
Agent.run(goal)
  └─ AgentLoop.run(goal)
       ├─ Planner.plan(goal)              → Plan
       └─ while not terminate:
            ├─ Executor.next_step(plan)   → Step
            ├─ ReActEngine.run(step, history, goal)  → StepResult（内部多轮 Thought/Action/Obs）
            ├─ Executor.update(plan, r)
            ├─ history.append(本步结论+工具观察) → _apply_window()（滑窗 + 滚动摘要）
            └─ if Planner.should_replan: Planner.replan()
```

### 跨 step 上下文（history）与记忆的分层

| 层 | 生命周期 | 载体 | 说明 |
|---|---|---|---|
| history | 单次 run 内跨 step | `AgentLoop.history`（dict 列表） | 步骤结论 + 工具观察（含 ask_user 用户反馈）的压缩表示；按 `history_window` 滑窗，超窗条目可经 LLM 滚动摘要（`history_summarize`），单条观察按 `observation_max_chars` 截断；含用户反馈的条目最后淘汰 |
| ShortTermMemory | 单次 run 内消息级 | `memory/short_term.py` | 预留给多轮对话续跑场景，主流程暂未接线 |
| LongTermMemory | 跨 session 永久 | `.data/memory/long_term/<ns>/{candidates,tenured}.jsonl` | 分代晋升的长期记忆，已接线 run 流程（见下节） |

### 长期记忆：分代晋升（借鉴 JVM）

```
run 结束（Minor GC）
  └─ Extractor 提取候选（1 次 LLM 调用，含打标）
       └─ TenuringManager.absorb(candidates, session_id)
            ├─ 与老年代冲突（相似度 ≥ match_threshold）→ 新事实覆盖旧内容（保留 id/age）
            ├─ 匹配既有候选（不同 session）→ 合并、age+1
            ├─ age ≥ tenuring_threshold 或 pinned → 晋升 tenured（发 memory_promoted 事件）
            └─ 候选超 candidate_ttl_days 未复发 → 淘汰

run 开始
  └─ MemoryService.recall(goal) → tenured 记忆注入每个 step 的上下文前缀（不受窗口淘汰）
```

关键语义：
- **age 按「不同 session」计数**，同一会话内重复提取不累计（防止单次长会话自我晋升）；
- **pinned 快速晋升**：`remember` 工具（用户显式指定）或提取时识别用户「记住…」话语，直通老年代；
- **冲突策略**：同主题新记忆覆盖旧内容，保留 id 与 age（符合「用户偏好变了」类可变事实）；
- **标签系统**（`memory.tags`）：`implicit`（LLM 按提示词自由生成）/ `explicit`（配置枚举分类与取值，LLM 只能选择）/ `hybrid`（固定分类 + 自由扩展）/ `off`；打标与提取在同一次 LLM 调用完成；
- 组件：`memory/schema.py`（MemoryRecord）、`memory/extractor.py`（提取+打标）、`memory/tenuring.py`（晋升管理）、`memory/service.py`（对外入口）。


## 6. 开发约定

- **数据契约优先**：新增功能先扩展 `schema/`，保持类型安全，禁止在逻辑层散落裸 `dict`。
- **依赖注入**：`Agent` 接收已构造的组件实例，不在核心类内部硬编码具体 LLM / 工具实现。
- **抽象接口**：新增 LLM 供应商实现 `LLMClient`；新增工具继承 `Tool` 或用 `@tool` 装饰。
- **日志分级**：loop / react / tool 各自 logger，便于追踪多轮执行过程。
- **分层边界**：`Planner` 不感知单步内工具调用次数；`ReActEngine` 不感知整体计划排布。
- **命名**：类名 `PascalCase`，模块/函数 `snake_case`，枚举 `UPPER_SNAKE`。

## 7. 数据存储（`.data`）

存储使用**本地文件存储**，默认根目录为当前项目下的 `.data/`（可通过配置 `paths.data_dir` 覆盖）。所有读写经由 `cagent.storage` 模块，便于后续替换为其他后端。

```
.data/
├── sessions/                 # 会话记录，按 session_id 隔离
│   └── <session_id>/
│       ├── meta.json         # 会话元信息（目标、创建时间、状态）
│       ├── messages.jsonl    # 对话消息流（每行一条 Message）
│       ├── plan.json         # 当前/最终计划（Plan 序列化）
│       └── trace.jsonl       # 执行轨迹（Thought / Action / Observation）
├── memory/
│   ├── short_term/           # 工作记忆快照（按 session 或任务）
│   └── long_term/<ns>/       # 长期记忆（分代）：candidates.jsonl（候选）/ tenured.jsonl（已晋升）
├── plans/                    # 历史计划存档（可复用 / 对比）
└── logs/                     # 运行日志（loop / react / tool 分级）
```

约定：
- key 采用相对路径风格，如 `sessions/<id>/plan.json`；`StorageBackend` 提供 `write_text/read_text/append_line/write_json/read_json`。
- `sessions/` 为每次运行的隔离单元，支撑断点续跑与回放；`memory/` 跨会话沉淀长期知识。
- `.data/` 不纳入版本控制（见 `.gitignore`）。

## 7.x 配置系统与热加载（端上可配置、保存即生效）

模型与提示词通过 `config/agent.yaml` 集中管理，由 `cagent.config.ConfigProvider` 读取。核心目标是**支持各端（CLI / Web / 桌面）在运行时修改配置，保存后无需重启即对下一次运行生效**。

### 配置结构（`agent.yaml`）

```yaml
model:
  provider: openai          # 对齐 llm.OpenAIClient
  model: gpt-4o
  api_key: ${OPENAI_API_KEY}  # 支持 ${ENV_VAR} 环境变量展开
  base_url: null
  temperature: 0.0
  max_tokens: 2048

prompts:                    # 可选：覆盖内置提示词
  planner_system: "你是一个任务规划器……"   # 自定义规划提示
  replan_system: "……"
  react_system: "……{step}……{tools}……"     # 支持 {step}/{tools} 占位符
  react_unconverged: "……"                  # ReAct 迭代耗尽未收敛时的兜底总结提示
  history_summarize_system: "……"           # history 超窗条目的滚动摘要提示
  memory_extract: "……{tags_instruction}……" # 长期记忆提取提示（支持 {tags_instruction}）
  summarize_system: "……"                   # 覆盖最终总结提示

# 迭代次数控制（可选，缺省使用内置默认值）
max_steps: 20                  # 计划层：最多执行的 Step 数
react_max_iterations: 5        # 单步 ReAct 内最多推理—行动轮数

# 跨 step 上下文窗口（可选，缺省使用内置默认值）
history_window: 10             # 注入后续步骤的最近 step 条目数；0 = 不限制
observation_max_chars: 500     # 单条工具观察注入上下文时的截断长度；0 = 不截断
history_summarize: true        # 超窗条目是否用 LLM 滚动摘要（false 时直接丢弃）

# 工具系统（可选，缺省启用全部分组、不做动态选择）
tools:
  enabled_groups: null         # 启用的工具分组；null = 全部（default 组始终启用）
  dynamic: false               # 开启后每 step 按「步骤描述+总目标」检索 Top-K 工具注入
  max_per_step: 8              # dynamic 开启时注入的工具数上限；无命中回退全量

# 长期记忆（分代晋升，可选，缺省使用内置默认值）
memory:
  enabled: true
  namespace: default
  tenuring_threshold: 3        # 跨 N 个不同会话出现后晋升
  candidate_ttl_days: 14       # 候选长期未复发则淘汰；0 = 永不过期
  recall_k: 5                  # run 开始召回条数；0 = 关闭召回
  recall_candidates: false     # 召回是否包含未晋升候选
  match_threshold: 0.6         # 去重合并的关键词相似度阈值
  tags:
    mode: hybrid               # implicit | explicit | hybrid | off
    implicit_max_tags: 5
    categories:                # explicit / hybrid 模式的固定分类
      domain: {values: [work, life, study], required: false}
```

### 热加载机制

- `ConfigProvider(path, watch=True)` 启动时后台起一个 **守护线程每秒轮询文件 mtime**；检测到变化立即重新解析。
- 每次取值都走 `get_model_config()` / `get_prompt()`，内部先 `_maybe_reload()` 核对 mtime → **保证下一次 `run` 取到的是最新文件内容**。
- 提示词优先级：`agent.yaml 的 prompts[key]` > 调用方传入 `default` > 模块内置 `DEFAULT_PROMPTS`。

### 端上配置约定

- **写入方**：各端只负责把用户编辑结果写回 `config/agent.yaml`（CLI 直接读写文件；Web 提供 PUT `/config`；桌面端用表单双向绑定）。核心库不感知"谁改的"，只认文件。
- **生效路径**：`Agent.run(goal)` → 每次运行从 `ConfigProvider` 现取 model/prompt → 改动即时反映。
- **多用户隔离**：端上如需按用户隔离配置，由各端自行维护 `config/<user>/agent.yaml` 并传给 `ConfigProvider(path=...)`，核心库无差别加载。

## 8. 运行事件机制（标准化 + SSE 投递）

核心引擎在执行过程发布标准化事件（`cagent/events/`），各端（CLI / Web / 桌面）订阅消费，实现"过程可观察、可投递、可重放"。

### 事件类型（`EventType`）

| 类型 | 触发时机 | payload 关键字段 |
|------|---------|-----------------|
| `plan_created` | 计划生成 | `plan`（步骤列表） |
| `step_started` | 步骤开始 | `step`（id/description） |
| `thought` | LLM 推理过程 | `content` |
| `tool_call` | 调用工具前 | `name`、`args` |
| `tool_result` | 工具返回后 | `name`、`content`、`ok` |
| `step_finished` | 步骤结束 | `step`、`result`（success/output/error） |
| `replanned` | 计划被修订 | `plan` |
| `user_input_request` | 请求用户补充信息 | `question` |
| `memory_recalled` | 长期记忆召回注入上下文 | `count`、`contents` |
| `memory_promoted` | 记忆晋升为长期记忆 | `id`、`content`、`tags`、`age`、`pinned` |
| `final_answer` | 最终答案产出 | `answer` |
| `error` | 运行异常 | `message` |

### 事件对象（`AgentEvent`）

统一字段：`type` / `seq`（全局递增）/ `ts` / `session_id` / `step_id` / `payload`。

### 投递方式

- **订阅**：`EventEmitter.subscribe(handler)`，handler 收到 `AgentEvent` 同步处理（CLI 用它渲染终端）。
- **SSE 流**：`EventEmitter.iter_sse()` 返回标准 SSE 生成器（`event:` + `data:` 两行），Web 端可直接挂到 `StreamingResponse`。
- **序列化**：`to_sse(event)` 单条转换；`AgentEvent.model_dump_json()` 为 JSON。
- **会话上下文**：`Agent.run` 会把 session_id 写入 `emitter.default_session_id`，期间所有事件自动附带。

### 接入方式

`Agent(tools=..., emitter=emitter)`；不传 emitter 时所有埋点静默跳过，零侵入。

### CLI 展示策略（`clients/cli/display.py`）

- 工具调用（黄色加粗 `→ 调用工具: name(args)`）、工具结果（绿色/红色）、思考（灰色）、计划/最终答案（加粗标题）、错误（红色）等分色渲染。
- 非 TTY 或设置了 `NO_COLOR` 自动降级为纯文本。
- `run` 子命令默认订阅事件并实时输出执行过程。

### ask_user 反馈工具（`cagent/tools/builtin/feedback.py`）

- 模型判断信息不足时可调用 `ask_user(question)` 向用户收集补充信息。
- 调用时发布 `user_input_request` 事件（前端据此渲染提问框）；实际输入由注入的 `input_fn` 获取（CLI 读 stdin、Web 等前端提交），核心库不关心实现。
- CLI 的 `run` 会自动注册该工具。

## 9. 实现顺序建议

1. `schema/` —— 定义 `Plan/Step/Action/Observation/Message`
2. `llm/base.py` + `tools/base.py` —— 跑通 "LLM 调用工具" 最小闭环
3. `core/react.py` —— 单步 ReAct
4. `core/planner.py` + `core/executor.py` —— 计划层
5. `core/loop.py` + `core/agent.py` —— 编排入口
6. `memory/` + `prompts/` —— 打磨与增强

## 10. 多端接入

支持 CLI、Web、桌面版等多种客户端，遵循以下原则：

- **单向依赖**：`clients/*` → `cagent`（核心库）；`cagent` 内部任何模块都**不得** import `clients`，保证核心可独立测试与复用。
- **薄接入层**：各端只负责「输入输出适配 + 生命周期管理」，所有 Agent 逻辑（plan / react / 存储 / 记忆）都落在 `cagent/` 内。例如 Web 端把 HTTP 请求转成 `Agent.run(goal)`，再把 `trace` 流推给前端。
- **共享抽象**：会话与持久化统一走 `cagent.storage`，多端天然共享同一份 `.data`；若需跨端状态（如 Web 多用户），在 `clients/web` 内做用户隔离，而非改动核心。
- **可插拔入口**：CLI 通过 `pyproject.toml` 的 `[project.scripts]` 暴露 `cagent` 命令，也可 `python -m clients.cli`；Web / 桌面各自提供独立启动入口。
- **按端隔离依赖**：CLI / Web / 桌面所需的第三方框架各自声明，避免核心库被重依赖污染。

### CLI 已实现（`clients/cli/`）

当前 CLI 支持以下子命令：

| 命令 | 用途 |
|------|------|
| `cagent run <goal>` | 用当前配置运行 Agent，输出最终答案与 session_id |
| `cagent config show` | 显示当前生效配置（模型、迭代次数、提示词） |
| `cagent config set <key> <value>` | 修改配置项（如 `max_steps 15`），写回 yaml 即时生效 |
| `cagent sessions list` | 列出 `.data/sessions/` 下所有历史会话 |
| `cagent sessions show <id>` | 查看指定会话的 meta / plan / trace |
