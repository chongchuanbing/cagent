# 工具系统（Tool System）设计文档

> 版本：v1（2026-08-24）
> 模块位置：`cagent/tools/`、`cagent/core/react.py`、`cagent/prompts/react_prompt.py`
> 相关配置：`config/agent.yaml` 的 `tools:` 段（支持热加载，保存后约 1s 生效）

## 1. 设计目标

1. **统一抽象**：所有工具实现同一基类，统一执行入口（`run()`）与返回格式（`ToolResult`），统一 Schema 生成（OpenAI function calling 风格）。
2. **双路径创建**：简单函数用 `@tool` 装饰器自动包装；需要依赖注入的工具（如 ask_user 需要 emitter/input_fn）直接继承 `Tool` 子类。
3. **分组命名空间**：`group` 属性实现工具面的按组裁剪，`default` 组不可裁剪；当工具数量增长时，按组启停比全量注入更精准。
4. **动态 Top-K 选择**：零 LLM 开销的关键词重叠检索，按步骤语义动态裁剪注入工具数，避免 token 膨胀与选择准确率下降。
5. **Prompt 与 Schema 分离**：system prompt 只注入工具目录（名称 + 一句话描述），完整参数 schema 走 function calling 的 `tools` 参数，避免双份冗余。
6. **幻觉防护**：ReAct 引擎用本 step 选中的工具集限制可用范围，模型幻觉调用不存在的工具返回明确错误提示。
7. **延迟绑定**：部分工具（如 RememberTool）依赖的组件在 Agent 构造阶段尚不可用，支持延迟注入。

## 2. 核心数据结构

### 2.1 ToolResult — 统一返回

```python
@dataclass
class ToolResult:
    ok: bool                         # 执行是否成功
    content: str                     # 结果内容（成功时）或空串
    error: Optional[str] = None      # 错误信息（失败时）
```

所有工具的 `run()` 统一返回 `ToolResult`，调用方无需关心工具内部异常格式。

### 2.2 Tool — 抽象基类

```python
class Tool(ABC):
    name: str = ""          # 工具唯一标识，注册时作为 key
    description: str = ""   # 一句话说明，供目录渲染与 LLM 选参
    group: str = "default"  # 分组命名空间，default 组始终启用

    @abstractmethod
    def run(self, **kwargs) -> ToolResult: ...

    def schema(self) -> dict: ...    # OpenAI function calling JSON Schema
```

`group` 默认为 `"default"`。分组裁剪时，`default` 组无条件保留，确保基础工具不被误裁。

`schema()` 返回格式：

```json
{
  "type": "function",
  "function": {
    "name": "tool_name",
    "description": "...",
    "parameters": {
      "type": "object",
      "properties": { ... },
      "required": [ ... ]
    }
  }
}
```

### 2.3 FunctionTool — 装饰器包装

```python
class FunctionTool(Tool):
    def __init__(self, func, name, description, group="default"): ...

    def run(self, **kwargs) -> ToolResult:
        # 调用 func，自动捕获异常 → ToolResult(ok=False, error=...)

    def schema(self) -> dict:
        # inspect.signature 自动提取参数 → properties + required
```

`@tool` 装饰器把普通函数转换为 `FunctionTool`：

```python
@tool(name="calculator", description="对两个数字做加法", group="math")
def calculator(a: int, b: int) -> int:
    return a + b
```

参数 schema 自动从函数签名提取：无默认值的参数标记为 `required`，类型统一映射为 `string`（LLM 的自然输入均为字符串）。

### 2.4 Action / Observation — ReAct 循环中的工具调用记录

```python
class Action(BaseModel):
    tool_name: str
    args: dict
    thought_ref: Optional[str]

class Observation(BaseModel):
    action_ref: Optional[str]   # 关联的工具名
    content: str                # 工具返回内容
    ok: bool                    # 是否成功
```

ReAct 循环中每轮工具调用产生一对 Action → Observation，记录在 `last_trace` 中供存储与调试。

## 3. ToolRegistry — 注册与检索

```python
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}    # name → Tool

    def register(self, tool, group=None)     # 注册；group 可覆盖工具自身声明
    def get(self, name) -> Optional[Tool]    # 按名查找
    def list(self, groups=None) -> List[Tool]   # 按组过滤（default 组始终保留）
    def groups(self) -> Dict[str, List[str]]    # 分组视图：group → [tool names]
    def schemas(self, groups=None) -> List[dict] # JSON Schema 列表
    def select(self, query, k=8, groups=None) -> List[Tool]  # 动态 Top-K
    def __contains__(self, name) -> bool
```

### 3.1 分组裁剪

`list(groups)` 的过滤逻辑：

```
groups = None → 返回全部工具
groups = ["math", "interaction"] → 返回 math + interaction + default 组的工具
```

`default` 组始终加入启用集合，无法被裁剪：

```python
enabled = set(groups) | {"default"}
```

`register(tool, group=)` 可在注册时覆盖工具自身声明的 `group`，实现同一工具在不同端注册到不同分组。

### 3.2 动态 Top-K 选择

`select(query, k, groups)` 使用零 LLM 开销的关键词重叠计分：

```
1. pool = list(groups) 按 enabled_groups 过滤的候选池
2. q = tokenize(query) 对查询文本分词（英文按词、中文按 bigram）
3. 对 pool 中每个工具：
   score = |q ∩ tokenize(name + description)|
   只保留 score > 0 的工具
4. 按 score 降序取 Top-K
5. 若完全无命中 → 回退全量 pool，保证步骤不因检索空而失去工具
```

分词由 `cagent/utils/text.py` 的 `tokenize()` 提供，与长期记忆召回共享同一分词逻辑。

**回退策略**：无命中时返回全量而非空集，确保即使关键词匹配失效，步骤仍能使用所有启用工具。

## 4. 内置工具

### 4.1 calculator（@tool 装饰器示例）

| 属性 | 值 |
|---|---|
| name | `calculator` |
| group | `math`（CLI 注册时指定） |
| 创建方式 | `@tool` 装饰器 |

最简单的 `@tool` 用法：一个纯函数，无外部依赖，自动生成参数 schema。

### 4.2 FeedbackTool（ask_user）

| 属性 | 值 |
|---|---|
| name | `ask_user` |
| group | `interaction`（CLI 注册时指定） |
| 创建方式 | 继承 `Tool` 子类 |
| 依赖注入 | `emitter`（事件发布）、`input_fn`（输入回调） |

**执行流程**：

```
1. 发布 USER_INPUT_REQUEST 事件（payload: {question}）
2. 调用 input_fn(question) 获取用户输入
3. 输入内容经 sanitize_text() 清洗代理字符
4. 返回 ToolResult(ok=True, content=用户回答)
```

`input_fn` 默认读 stdin；CLI/Web/桌面端可注入自定义实现（如 Web 端等待前端 HTTP 提交）。

异常处理：`EOFError` / `KeyboardInterrupt` → 返回 `ok=False, error="用户未提供输入"`；空输入 → `ok=False, error="用户输入为空"`。

### 4.3 RememberTool（remember）

| 属性 | 值 |
|---|---|
| name | `remember` |
| group | `memory`（CLI 注册时指定） |
| 创建方式 | 继承 `Tool` 子类 |
| 依赖注入 | `MemoryService`（延迟绑定） |

**执行流程**：

```
1. 检查 _memory 是否已绑定（否则返回不可用错误）
2. 校验 content 非空
3. 调用 memory.remember(content, tags) → pinned 直通晋升
4. 返回 ToolResult(ok=True, content="已记住（长期记忆 #id）：内容")
```

**延迟绑定**：

```python
# 构造时可不传 memory
rt = RememberTool()           # _memory = None
# 后续绑定
rt.set_memory(memory_service) # _memory = memory_service
```

Agent.run() 首次调用时会自动检测并补绑：

```python
rt = self.tools.get("remember")
if isinstance(rt, RememberTool) and rt._memory is None:
    rt.set_memory(self.memory)
```

### 4.4 内置工具注册总览

| 工具 | name | group | 注册条件 | 依赖 |
|---|---|---|---|---|
| calculator | `calculator` | `math` | 始终注册 | 无 |
| FeedbackTool | `ask_user` | `interaction` | `emitter` 非空 | emitter + input_fn |
| RememberTool | `remember` | `memory` | `memory.enabled=True` | MemoryService（延迟） |

## 5. 工具在 ReAct 循环中的使用

### 5.1 工具选择（_select_tools）

每个 step 开始时，ReActEngine 根据 `ToolsConfig` 确定本步注入的工具集：

```
步骤 1：读取 ToolsConfig
  ├─ enabled_groups = None → 全量工具
  └─ enabled_groups = ["math", "interaction"] → 只保留 math + interaction + default 组

步骤 2：是否开启动态选择
  ├─ dynamic = False → 返回步骤 1 的结果
  └─ dynamic = True → 以「步骤描述 + 总目标」为 query，
       调用 registry.select(query, k=max_per_step, groups=enabled_groups)
       → 返回 Top-K 工具
```

### 5.2 工具调用循环

```
1. _select_tools(step, goal) → tools: List[Tool]
2. available = {t.name: t for t in tools}   ← 仅本 step 选中的工具可被调用
3. tool_schemas = [t.schema() for t in tools]
4. 构建 messages：
   - system: build_react_system_prompt(step, tools)  ← 含工具目录
   - user: build_react_user_prompt(step, history, goal)

5. 循环（最多 max_iterations 次）：
   a. 调 LLM：llm.complete_with_tools(messages, tool_schemas)
   b. 无 tool_calls → 收敛，返回结论
   c. 有 tool_calls → 对每个调用：
      - name = tc["function"]["name"]
      - tool = available.get(name)
      - tool is None → obs = "错误：工具 xxx 在本步骤不可用"
      - tool 存在 → result = tool.run(**args) → obs = result.content
      - 发布 TOOL_CALL / TOOL_RESULT 事件
      - 构造 Observation(action_ref=name, content=obs, ok=ok)
      - 回写 Message(role=TOOL, content=obs, tool_call_id=...)

6. 超过 max_iterations → _summarize_unconverged() 兜底
```

### 5.3 幻觉防护

模型可能调用不在 `available` 字典中的工具名。处理方式：

```python
tool = available.get(name)
if tool is None:
    obs_content = f"错误：工具 {name} 在本步骤不可用（未启用或未入选）"
    ok = False
```

返回明确错误而非静默忽略，让模型有机会在下一轮纠正。

## 6. Prompt 中的工具目录渲染

### 6.1 目录格式

`format_tool_catalog(tools)` 将工具列表渲染为紧凑目录：

```
- calculator: 对两个数字做加法，返回结果
- [interaction] ask_user: 向用户提问以获取必要的补充信息。当目标任务的信息不完整、存在歧义或需要用户确认时使用；参数 question 为要问的问题。返回用户…
- [memory] remember: 把用户明确要求记住的信息保存为长期记忆（跨会话生效）。仅当用户显式表达「记住/以后记住/帮我记下」等意图时调用；参数 content 为要记住的事…
```

规则：

- 非 default 组显示 `[group]` 前缀，default 组省略
- 描述超 `TOOL_DESC_MAX_CHARS`（120 字符）时截断并加 `…`
- 换行符替换为空格

### 6.2 Prompt 与 Schema 分离的设计考量

| 通道 | 内容 | 作用 |
|---|---|---|
| system prompt | 工具目录（name + 一句话描述） | 让模型了解有哪些工具、各自用途 |
| `tools` 参数 | 完整 JSON Schema（name + description + parameters） | 让模型知道如何调用（参数名、类型、必填） |

两通道互补但**不冗余**：system prompt 提供全局视野（快速浏览工具能力），`tools` 参数提供精确调用信息。若只靠 `tools` 参数，模型在工具数量多时难以全局把握；若 system prompt 也放入完整 schema，则 token 成倍膨胀。

### 6.3 System Prompt 模板

```python
REACT_SYSTEM_TEMPLATE = (
    "你正在执行如下计划步骤：\n步骤：{step}\n\n"
    "可用工具：\n{tools}\n\n"
    "工作方式（ReAct）：\n"
    "1. 若需要更多信息或要执行动作，调用合适的工具。\n"
    "2. 工具返回后，基于观察继续推理。\n"
    "3. 当你已得出该步骤的最终结论时，直接输出结论文本，不要调用任何工具。\n"
)
```

模板支持通过 `prompts.react_system` 配置覆盖，`{step}` 和 `{tools}` 为占位符。

### 6.4 User Prompt 中的工具观察

`build_react_user_prompt` 渲染 history 时完整保留工具问答记录：

```
- 步骤[1]（成功）收集用户偏好 → 结论：用户偏好简洁回答
    · 工具 ask_user 参数 {"question": "你偏好什么风格？"} → 返回：简洁
```

前序 step 的工具结果对当前 step 可见，避免重复询问。

## 7. 工具系统配置

### 7.1 ToolsConfig

```python
class ToolsConfig(BaseModel):
    enabled_groups: Optional[List[str]] = None  # None = 全部；default 始终启用
    dynamic: bool = False                       # 开启动态 Top-K
    max_per_step: int = 8                       # 动态选择时每步最多工具数
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `enabled_groups` | `None` | 启用的分组列表；`None` 表示全部启用 |
| `dynamic` | `False` | 是否按步骤语义动态检索工具 |
| `max_per_step` | `8` | 动态检索时的 Top-K 上限 |

### 7.2 YAML 配置示例

```yaml
tools:
  enabled_groups: [math, interaction, memory]   # 启用分组；省略或 null 表示全部
  dynamic: true                                 # 开启动态选择
  max_per_step: 6                               # 每步最多注入 6 个工具
```

### 7.3 配置热加载

所有 `tools.*` 配置通过 `ConfigProvider` 的 mtime 轮询支持热加载（约 1s 生效）。ReActEngine 每次 `_select_tools` 时实时读取当前配置，无需重启 Agent。

## 8. 事件系统

工具相关的事件类型：

| 事件 | 触发时机 | payload |
|---|---|---|
| `TOOL_CALL` | 模型发起工具调用 | `{name, args, step_id}` |
| `TOOL_RESULT` | 工具返回结果 | `{name, content, ok, step_id}` |
| `USER_INPUT_REQUEST` | FeedbackTool 发出提问 | `{question}` |

CLI 端的 ANSI 颜色渲染策略：
- `TOOL_CALL`：黄色工具名 + 青色参数
- `TOOL_RESULT`：绿色（ok=True）/ 红色（ok=False）
- `USER_INPUT_REQUEST`：蓝色问题文本

事件通过 `EventEmitter` 发布，核心库不关心展示层实现，CLI/Web/桌面端各自订阅渲染。

## 9. 完整调用链路

```
CLI main.py
  └─ _build_agent()
       ├─ ToolRegistry() 创建空注册中心
       ├─ register(calculator, group="math")
       ├─ register(FeedbackTool(emitter=emitter), group="interaction")   [条件: emitter]
       ├─ register(RememberTool(agent.memory), group="memory")           [条件: memory.enabled]
       └─ Agent(tools=registry, config=cfg, emitter=emitter)
            │
            ├─ _build_loop()
            │    └─ ReActEngine(llm, tools=registry, config=cfg, emitter=emitter)
            │
            └─ run(goal, session_id)
                 └─ AgentLoop._run()
                      └─ 对每个 step：
                           ├─ ReActEngine.run(step, history, goal)
                           │    ├─ _select_tools(step, goal)
                           │    │    ├─ 读取 ToolsConfig: enabled_groups / dynamic / max_per_step
                           │    │    ├─ dynamic=True → registry.select(query, k=max_per_step)
                           │    │    └─ dynamic=False → registry.list(groups)
                           │    │
                           │    ├─ build_react_system_prompt(step, tools) ← format_tool_catalog()
                           │    ├─ build_react_user_prompt(step, history, goal)
                           │    ├─ llm.complete_with_tools(messages, tool_schemas)
                           │    │
                           │    └─ 工具调用循环：
                           │         ├─ available[name].run(**args) → ToolResult
                           │         ├─ 幻觉调用 → "工具 xxx 在本步骤不可用"
                           │         ├─ 构造 Observation → 回写 TOOL 消息
                           │         └─ 发布 TOOL_CALL / TOOL_RESULT 事件
                           │
                           └─ 收敛 → StepResult → 写入 history
```

## 10. 扩展新工具的规范

### 10.1 简单函数工具

使用 `@tool` 装饰器，适合无外部依赖的纯函数：

```python
# cagent/tools/builtin/weather.py
from ..base import ToolResult, tool

@tool(name="weather", description="查询指定城市的天气", group="search")
def weather(city: str) -> str:
    # 实际调用天气 API
    return f"{city}: 晴，25°C"
```

### 10.2 需要依赖注入的工具

继承 `Tool` 子类，手动实现 `run()` 和 `schema()`：

```python
class SearchTool(Tool):
    name = "search"
    description = "搜索互联网获取信息"
    group = "search"

    def __init__(self, api_key=None):
        self._api_key = api_key

    def run(self, query: str) -> ToolResult:
        # 调用搜索 API
        ...

    def schema(self) -> dict:
        # 手写参数 schema
        ...
```

### 10.3 注册到 ToolRegistry

在端层（如 CLI）注册：

```python
from cagent.tools.builtin.weather import weather
from cagent.tools.builtin.search import SearchTool

tools = ToolRegistry()
tools.register(weather, group="search")                    # @tool 装饰器生成的 FunctionTool
tools.register(SearchTool(api_key="..."), group="search")  # 子类实例
```

### 10.4 启用分组

在 `agent.yaml` 中配置：

```yaml
tools:
  enabled_groups: [math, interaction, memory, search]
  dynamic: true
  max_per_step: 8
```

## 11. 设计取舍与后续演进

| 项 | 现状 | 演进方向 |
|---|---|---|
| 参数类型推断 | `FunctionTool.schema()` 统一映射为 `string` | 可按 type annotation 映射 `integer`/`number`/`boolean`，提升 LLM 选参精度 |
| 动态选择算法 | 纯关键词重叠（零 LLM 开销） | borderline 可增加 LLM 二次确认；或替换为向量嵌入相似度 |
| 工具版本 | 无版本管理 | 工具签名变更时需版本号，避免旧 schema 导致调用失败 |
| 工具权限 | 无权限控制 | 可增加 `requires_confirm: bool`，危险操作需用户确认 |
| 工具组合 | 单工具调用 | 可支持并行调用（模型一次返回多个 tool_calls） |
| 目录截断 | 固定 120 字符 | 可按 total token budget 动态分配每工具描述长度 |
| 分组嵌套 | 单层分组 | 可支持多级分组（如 `search.web` / `search.local`） |
| 工具卸载 | 注册后不可移除 | 可增加 `unregister(name)` 支持运行时动态装卸 |

## 12. 测试覆盖

相关测试分布：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_react_planner.py` | ReAct 工具调用循环、幻觉调用拦截、未收敛兜底 |
| `tests/test_config.py` | ToolsConfig 解析与默认值 |
| `tests/test_events.py` | TOOL_CALL / TOOL_RESULT / USER_INPUT_REQUEST 事件发布 |
| `tests/test_tool_selection.py` | 分组裁剪、动态 Top-K、无命中回退 |

核心测试场景：

- `@tool` 装饰器生成 FunctionTool，自动提取参数 schema
- 分组裁剪：指定 groups 时 default 组无条件保留
- 动态选择：关键词重叠计分 + Top-K + 无命中回退全量
- 幻觉调用：调用不在 available 中的工具返回错误
- FeedbackTool：input_fn 注入 + sanitize_text 清洗
- RememberTool：延迟绑定 + 记忆服务不可用时的降级
