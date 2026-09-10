# 多模型配置与运行时切换设计

> 状态：设计定稿（待实现）
> 适用范围：`cagent` 核心库 + `clients/cli`
> 关联文档：`docs/path-management-design.md`、`docs/design/sandbox-design.md`

---

## 1. 背景与目标

当前 `cagent` 只能配置**一个**模型：配置层 `AgentConfig.model` 是单个 `ModelConfig`（`config/schema.py:176`），`ConfigProvider.get_model_config()` 永远返回同一套连接参数（`config/provider.py:132`），`Agent._resolve_llm()` 据此构造**一个** `LLMClient` 并同时塞给 Planner / ReActEngine / AgentLoop（`core/agent.py:103-208`）。整次 run 只能跑同一个模型，且无法表达「工具调用 / 图片输入 / 思考模式」等能力差异。

目标：

1. **多模型配置**：支持在独立配置文件里登记多个模型，指定默认运行模型。
2. **运行时切换**：一次 run 内可指定使用哪个模型（`CLI --model <id>` 或 `Agent.run(model=...)`）。
3. **逐模型能力声明**：每个模型独立声明 `tool_calling` / `vision` / `reasoning` 三项能力，框架据此走不同调用路径。
4. **面向 UI 落盘**：模型配置独立成 `models.json`，字段命名对齐 WebUI / Desktop 端配置界面，由 UI 负责落盘，cagent 只读。

### 决策记录（ADR）

| # | 决策 | 结论 | 影响 |
|---|------|------|------|
| ADR-1 | 协议范围 | **仅支持 OpenAI 兼容协议**（`/chat/completions`） | 砍掉 `useCustomProtocol` 分支，`provider` 退化为常量 `openai`，`ModelRouter` 恒用 `OpenAIClient` |
| ADR-2 | 切换粒度 | **仅按 run 切换**（run 之间切；run 内所有角色共用同一模型） | 砍掉 `model_roles` 角色级分配，装配层几乎不改 |
| ADR-3 | 旧配置兼容 | **不兼容（已推翻）**：`agent.yaml.model` 直接移除，不做迁移 | 用户明确指示「`agent.yaml` 的 `model` 直接移除，不做兼容」。加载层只认 `models.json`；缺失/为空即 `ValueError`，旧 `model:` 字段不再解析 |
| ADR-4 | 思考模式表达 | `supportsReasoning: bool` 能力开关 + 可选 `reasoning` 子对象（effort / budgetTokens / extraBody） | `vendor` 仅驱动思考参数翻译，不参与协议分发 |
| ADR-5 | 配置文件归属 | `models.json` 与 `agent.yaml` 同目录，由 UI 落盘，cagent 只读 | `ConfigProvider` 新增 `models_path`，watch 同时盯两个文件 |

---

## 2. 整体架构

```mermaid
flowchart TD
    UI[WebUI / Desktop 配置界面] -->|落盘| MJ[config/models.json<br/>cagent 只读]
    AY[config/agent.yaml<br/>旧 model: 兼容回退] --> CP

    MJ --> CP[ConfigProvider<br/>加载 + 热加载 + 迁移]
    AY --> CP

    CP -->|解析为| MF[ModelsFile<br/>default + models[]]
    MF -->|构造| MR[ModelRouter<br/>get name / default / names]
    MR -->|懒构建+缓存| OC[OpenAIClient × N<br/>tool_calling / vision / reasoning]

    subgraph RUN["Agent.run(goal, model=None)"]
        AG[Agent] -->|model or default| MR
        MR -->|单个 LLMClient| PL[Planner]
        MR -->|同一 client| RE[ReActEngine]
        MR -->|同一 client| LO[AgentLoop.summary]
    end

    OC -->|思考参数翻译| RA[ReasoningAdapter<br/>openai→reasoning_effort<br/>qwen/deepseek/glm→enable_thinking]
```

关键洞察：一次 run 内有 4 个"消费模型"的角色（Planner、ReActEngine、AgentLoop.summary、MemoryService.extract），按 ADR-2 它们共用同一个 model。因此本方案**不引入角色级路由**，`ModelRouter.get(name)` 只按名字返回一个 client，照旧传给各角色——装配层签名不变。

---

## 3. 配置契约：`models.json`

UI 落盘格式（camelCase）。cagent 只读，字段对齐用户提供的参考结构：

```json
{
  "default": "glm-5.2",
  "models": [
    {
      "id": "glm-5.2",
      "name": "GLM-5.2 (DashScope)",
      "vendor": "Custom",
      "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
      "apiKey": "${DASHSCAPE_API_KEY}",
      "supportsToolCall": true,
      "supportsImages": false,
      "supportsReasoning": true,
      "reasoning": { "effort": "high", "budgetTokens": 8000, "extraBody": {} },
      "useCustomProtocol": false,
      "maxInputTokens": 1024000,
      "maxOutputTokens": 131072
    },
    {
      "id": "qwen-vl",
      "name": "Qwen-VL",
      "vendor": "Qwen",
      "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
      "apiKey": "${DASHSCAPE_API_KEY}",
      "supportsToolCall": true,
      "supportsImages": true,
      "supportsReasoning": false,
      "useCustomProtocol": false
    },
    {
      "id": "qwq-plus",
      "name": "QwQ-Plus (reasoner)",
      "vendor": "Qwen",
      "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
      "apiKey": "${DASHSCAPE_API_KEY}",
      "supportsToolCall": true,
      "supportsImages": false,
      "supportsReasoning": true,
      "reasoning": { "effort": "medium", "extraBody": { "enable_thinking": true } }
    }
  ]
}
```

**字段语义（`models.json` → 内部 `ModelConfig`）**

| `models.json` | 内部 `ModelConfig` | 行为 |
|---|---|---|
| `id` | `model` | 发 API 的模型名 + 路由 key（`--model <id>`） |
| `name` | `name` | 仅展示 |
| `vendor` | `vendor` | 仅驱动 `ReasoningAdapter` 思考参数翻译 |
| `url` | `base_url` | OpenAI 兼容端点 |
| `apiKey` | `api_key` | 支持 `${ENV}` 展开（复用现有 `_expand_env`） |
| `supportsToolCall` | `tool_calling` | ReAct 必须 `true`，否则启动期报错 |
| `supportsImages` | `vision` | 序列化 `Message.images`；非 vision 收到图→丢弃+告警 |
| `supportsReasoning` | `reasoning.enabled` | `true` 启用思考 |
| `reasoning.effort` | `reasoning.effort` | 思考强度（low/medium/high），按 vendor 翻译 |
| `reasoning.budgetTokens` | `reasoning.budget_tokens` | 思考预算（→ `max_completion_tokens`） |
| `reasoning.extraBody` | `reasoning.extra_body` | 厂商原生参数透传 |
| `useCustomProtocol` | — | **被忽略**（ADR-1：协议恒为 OpenAI 兼容），仅作 UI 展示兼容保留 |
| `maxInputTokens` | `max_input_tokens` | 信息/未来窗口裁剪 |
| `maxOutputTokens` | `max_tokens` | 对应现有 `max_tokens` |

`default` 为顶层字段，指向 `models[].id`。

---

## 4. 内部 Schema 扩展

### 4.1 `ReasoningConfig`（`config/schema.py` 新增）

```python
class ReasoningConfig(BaseModel):
    enabled: bool = False
    effort: Optional[Literal["low", "medium", "high"]] = None
    budget_tokens: Optional[int] = None
    extra_body: dict = Field(default_factory=dict)  # 厂商原生参数透传
```

### 4.2 `ModelConfig`（`config/schema.py` 扩展）

```python
class ModelConfig(BaseModel):
    model: str                        # ← UI 的 id
    name: Optional[str] = None       # ← UI 的 name
    vendor: str = "openai"           # ← UI 的 vendor（仅驱动 ReasoningAdapter）
    api_key: Optional[str] = None    # ← UI 的 apiKey（支持 ${ENV}）
    base_url: Optional[str] = None   # ← UI 的 url
    temperature: float = 0.0
    max_tokens: int = 2048           # ← UI 的 maxOutputTokens
    tool_calling: bool = True        # ← UI 的 supportsToolCall
    vision: bool = False             # ← UI 的 supportsImages
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    max_input_tokens: Optional[int] = None   # ← UI 的 maxInputTokens
    max_output_tokens: Optional[int] = None
```

加载时不使用 pydantic alias（`supportsReasoning→reasoning.enabled` 是嵌套映射），改用显式 `ModelConfig.from_ui_dict(d)`：

```python
@classmethod
def from_ui_dict(cls, d: dict) -> "ModelConfig":
    rc = d.get("reasoning") or {}
    reasoning = ReasoningConfig(
        enabled=bool(d.get("supportsReasoning", False)),
        effort=rc.get("effort"),
        budget_tokens=rc.get("budgetTokens"),
        extra_body=rc.get("extraBody") or {},
    )
    return cls(
        model=d["id"], name=d.get("name"),
        vendor=d.get("vendor", "openai"),
        api_key=d.get("apiKey"), base_url=d.get("url"),
        temperature=d.get("temperature", 0.0),
        max_tokens=d.get("maxOutputTokens", 2048),
        tool_calling=d.get("supportsToolCall", True),
        vision=d.get("supportsImages", False),
        reasoning=reasoning,
        max_input_tokens=d.get("maxInputTokens"),
        max_output_tokens=d.get("maxOutputTokens"),
    )
```

### 4.3 信封 `ModelsFile`（`config/schema.py` 新增）

```python
class ModelsFile(BaseModel):
    default: Optional[str] = None
    models: List[ModelConfig] = Field(default_factory=list)
```

### 4.4 `LLMConfig` 扩展（`llm/base.py`）

新增字段并带默认值，保证旧调用零改动：

```python
@dataclass
class LLMConfig:
    model: str = "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 2048
    data_dir: str = ".data"
    # —— 多模型能力声明 ——
    tool_calling: bool = True
    vision: bool = False
    reasoning_enabled: bool = False
    reasoning_effort: Optional[str] = None
    reasoning_budget_tokens: Optional[int] = None
    reasoning_extra_body: dict = field(default_factory=dict)
    vendor: str = "openai"
```

---

## 5. 加载、热加载与迁移

### 5.1 `ConfigProvider` 改造

- 新增构造参数 `models_path`，默认 `os.path.join(os.path.dirname(path), "models.json")`。
- 新增内部 `_cfg_models: ModelsFile`，在 `_load()` 中解析：
  1. 若 `models_path` 存在且解析出 `models` 非空 → 权威来源；
  2. `default` 缺失且有 `models` → 取 `models[0].id`；
  3. `default` 指向不存在的 id 或 `models` 为空 → 加载/调用期抛 `ValueError("models.json 未配置任何模型")`。
- **不兼容旧 `agent.yaml.model`**：ADR-3 已推翻，配置加载层只认 `models.json`，不再解析旧 `model:` 字段。

### 5.2 热加载

`_watch_loop` 同时比对 `agent.yaml` 与 `models.json` 的 mtime，任一变化即调用 `_load()`（复用现有 `_maybe_reload` 逻辑，改为比对两个 mtime 取较大者）。UI 修改 `models.json` 后，下一次 run 自动生效，无需重启。

### 5.3 新增取值接口

```python
def get_models_file(self) -> ModelsFile:
    self._maybe_reload()
    return self._cfg_models

def get_model_names(self) -> List[str]:
    return [m.model for m in self._cfg_models.models]
```

`get_model_config()` 保留，但改为「按 default 取对应 ModelConfig → `LLMConfig`」。

### 5.4 兼容说明

- **无兼容**：旧 `agent.yaml` 的 `model:` 字段已彻底移除，不再解析；`AgentConfig` 不含 `model` 字段。模型配置唯一入口是 `models.json`。
- 因此加载层不再有「旧 model → 单模型 registry」的回退路径。

---

## 6. LLM 层

### 6.1 `ModelRouter`（新增 `llm/registry.py`）

```python
class ModelRouter:
    def __init__(self, models_file: ModelsFile, llm_factory, data_dir=".data"):
        self._mf = models_file
        self._factory = llm_factory
        self._cache: dict[str, LLMClient] = {}
        self._data_dir = data_dir

    @property
    def default(self) -> str:
        if self._mf.default:
            return self._mf.default
        if self._mf.models:
            return self._mf.models[0].model
        raise ValueError("未配置任何模型")

    @property
    def names(self) -> List[str]:
        return [m.model for m in self._mf.models]

    def get(self, name: Optional[str] = None) -> LLMClient:
        name = name or self.default
        if name not in self.names:
            raise ValueError(f"未知模型 {name!r}，可用：{self.names}")
        if name not in self._cache:
            mc = next(m for m in self._mf.models if m.model == name)
            cfg = LLMConfig(
                model=mc.model, api_key=mc.api_key, base_url=mc.base_url,
                temperature=mc.temperature, max_tokens=mc.max_tokens,
                data_dir=self._data_dir,
                tool_calling=mc.tool_calling, vision=mc.vision,
                reasoning_enabled=mc.reasoning.enabled,
                reasoning_effort=mc.reasoning.effort,
                reasoning_budget_tokens=mc.reasoning.budget_tokens,
                reasoning_extra_body=mc.reasoning.extra_body,
                vendor=mc.vendor,
            )
            self._cache[name] = self._factory(cfg)
        return self._cache[name]

    def validate_react_target(self, name: Optional[str] = None) -> None:
        """启动期校验：ReAct 角色目标必须 tool_calling=true。"""
        mc = next(m for m in self._mf.models if m.model == (name or self.default))
        if not mc.tool_calling:
            raise ValueError(f"模型 {mc.model!r} 未开启 tool_calling，无法用于 ReAct 执行")
```

`llm_factory` 默认实现即现有 `_default_llm_factory`（`core/agent.py:26`）。

### 6.2 `ReasoningAdapter`（新增 `llm/reasoning.py`）

按 `vendor` 把 `reasoning_*` 翻译成厂商原生参数，并入 `extra_body`：

| vendor | 翻译 |
|---|---|
| `openai` | `reasoning_effort=<effort>`；若 `budget_tokens` → `max_completion_tokens`；思考模型自动 `temperature=0` |
| `qwen` / `deepseek` / `glm` / `custom` | `enable_thinking=true`（可经 `extra_body` 覆盖）；自动 `temperature=0` |

```python
def build_reasoning_extra_body(cfg: LLMConfig) -> dict:
    if not cfg.reasoning_enabled:
        return {}
    body = dict(cfg.reasoning_extra_body or {})
    v = (cfg.vendor or "openai").lower()
    if v == "openai":
        if cfg.reasoning_effort:
            body["reasoning_effort"] = cfg.reasoning_effort
        if cfg.reasoning_budget_tokens:
            body["max_completion_tokens"] = cfg.reasoning_budget_tokens
    else:
        body.setdefault("enable_thinking", True)
    return body
```

> 注：`temperature=0` 由 `OpenAIClient` 在检测到 `reasoning_enabled` 时强制覆盖，避免 o-series / Qwen-thinking 等拒绝非零温度。

### 6.3 `OpenAIClient` 改造（`llm/openai.py`）

三处改造：

1. **`tool_calling=False` 退化**：`complete_with_tools` 在 `config.tool_calling=False` 时退化为不带 `tools` 的 `complete`（被分给 ReAct 时已在上游 `validate_react_target` 拦截，此处为兜底）。

2. **`vision=True` 多模态序列化**：见 §7。

3. **思考模式**：

```python
def _request_kwargs(self, with_tools: bool) -> dict:
    kwargs = dict(
        model=self.config.model,
        temperature=0.0 if self.config.reasoning_enabled else self.config.temperature,
        max_tokens=self.config.max_tokens,
    )
    rb = build_reasoning_extra_body(self.config)
    if rb:
        kwargs["extra_body"] = rb
    return kwargs
```

`complete` / `complete_with_tools` 统一经 `_request_kwargs` 构造参数。

---

## 7. 消息层（图片输入落地）

### 7.1 `ImageRef` + `Message.images`（`schema/message.py`）

```python
class ImageRef(BaseModel):
    url: Optional[str] = None       # http(s) / data: URI
    path: Optional[str] = None      # 本地文件 → 读成 base64
    detail: str = "auto"            # openai: low/auto/high

class Message(BaseModel):
    role: MessageRole
    content: str = ""
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    images: List[ImageRef] = Field(default_factory=list)  # 新增
```

### 7.2 `OpenAIClient._to_openai_messages` 改造

`vision=True` 时，把 `images` 拼进 `content` 数组（OpenAI 多模态格式）：

```python
def _to_openai_messages(self, messages):
    out = []
    for m in messages:
        if m.images and self.config.vision:
            content = [{"type": "text", "text": m.content}]
            for img in m.images:
                b64 = _resolve_image_b64(img)  # url 直传 / path 读 base64
                content.append({
                    "type": "image_url",
                    "image_url": {"url": b64, "detail": img.detail},
                })
            item = {"role": m.role.value, "content": content}
        elif m.images and not self.config.vision:
            logger.warning("模型 %s 不支持 vision，已忽略 %d 张图片", self.config.model, len(m.images))
            item = {"role": m.role.value, "content": m.content}
        else:
            item = {"role": m.role.value, "content": m.content}
        # tool_calls / tool_call_id 处理同现状
        out.append(item)
    return out
```

非 vision 模型收到图片 → 丢弃 + `logger.warning`，不报错，保证纯文本链路不崩。

---

## 8. 装配层

### 8.1 `Agent` 接入 `ModelRouter`

- `Agent.__init__` 新增参数 `model_router: Optional[ModelRouter] = None`。
- 兼容旧用法：传入 `llm=`（旧路径）时，包成「单模型 router」（仅含该 client，default 即其 id），保持现有 API 不破。
- 两者都传时 `model_router` 优先。

顺序（保持向后兼容）：`model_router` → `llm` → `config`（经 `ModelRouter` 从 `models.json` 构建）。

### 8.2 `Agent.run` 支持 `model=`

```python
def run(self, goal, session_id=None, resume=False, space_dir=None, model: Optional[str] = None):
    ...
    router = self._model_router
    if router is not None:
        router.validate_react_target(model)      # 启动期护栏
        self._active_model = model or router.default
    loop = self._build_loop(path_space=session_space, model=self._active_model)
    ...
```

### 8.3 `_build_loop` 按 model 解析出单个 client

```python
def _build_loop(self, path_space=None, model=None) -> AgentLoop:
    if self._loop is not None:
        return self._loop
    llm = self._resolve_llm(model)               # 新增 model 参数
    react = self._react_engine or ReActEngine(llm, self.tools, config=self.config, ...)
    planner = self._planner or Planner(llm, config=self.config)
    executor = self._executor or Executor(react)
    return AgentLoop(planner, executor, max_steps=..., llm=llm, config=self.config, ...)
```

`_resolve_llm(model=None)`：

```python
def _resolve_llm(self, model=None) -> LLMClient:
    if self._llm is not None:
        return self._llm                              # 旧显式注入
    if self._model_router is not None:
        return self._model_router.get(model)          # 多模型路径
    if self.config is not None:
        return self._llm_factory(self.config.get_model_config())  # 旧 config 单模型
    raise ValueError("必须提供 llm / model_router / config")
```

> Planner / ReActEngine / AgentLoop 的构造签名**不变**——它们本来各吃一个 `llm`，只是这次传的是按 run 选出的那个 client。

---

## 9. CLI

### 9.1 `run` 增加 `--model`

`clients/cli/main.py`：

```python
p_run.add_argument("--model", default=None, help="指定运行模型 id（覆盖默认）；见 `cagent models list`")
```

`cmd_run` 改为 `agent.run(args.goal, ..., model=args.model)`。

### 9.2 新增 `models` 子命令

```text
cagent models list    列出已配置模型（id / name / vendor / tool_calling / vision / reasoning / 默认★）
cagent models default <id>   改写 models.json.default（可选，一期可不做）
```

`cmd_models_list` 从 `ConfigProvider.get_models_file()` 读出，按表格打印，默认项标记 `★`。

### 9.3 `config set` 兼容

现有 `_set_nested` 支持点分键。`models.json` 由 UI 落盘，CLI 不再直接 set 模型字段（避免双写冲突）；保留 `config set` 仅作用于 `agent.yaml`。

---

## 10. 校验护栏

| 护栏 | 触发点 | 行为 |
|---|---|---|
| ReAct 目标必须 tool_calling | `Agent.run` / `ModelRouter.validate_react_target` | `ValueError` 清晰报错，不跑到一半出怪现象 |
| `default` 指向不存在 id | `ConfigProvider._load` | 加载期 `ValueError` |
| 非 vision 模型收到图片 | `OpenAIClient._to_openai_messages` | 丢弃 + `logger.warning`，链路不崩 |
| 思考模型 temperature | `OpenAIClient._request_kwargs` | 强制 `temperature=0` |
| `useCustomProtocol=true` | `from_ui_dict` | 忽略该字段，按 OpenAI 兼容走（ADR-1） |

---

## 11. 分阶段实施计划

| 阶段 | 内容 | 关键文件 |
|---|---|---|
| **P1 契约 + 加载** | `ReasoningConfig` / `ModelConfig.from_ui_dict` / `ModelsFile`；`ConfigProvider` 加载 + 热加载 + 迁移；`LLMConfig` 扩字段 | `config/schema.py`、`config/provider.py`、`llm/base.py` |
| **P2 路由 + 装配** | `ModelRouter`、`Agent` 接 router + `run(model=)`、`_resolve_llm(model)`；CLI `--model` / `models list` | `llm/registry.py`(新)、`core/agent.py`、`clients/cli/main.py` |
| **P3 能力落地** | `OpenAIClient` tool_calling 退化 / vision 序列化 + `Message.images` / `ReasoningAdapter` | `llm/openai.py`、`llm/reasoning.py`(新)、`schema/message.py` |
| **P4 护栏 + 测试 + 示例** | 校验、单测、`agent.yaml.example` 标 deprecated、补 `config/models.json.example`、README 说明 | `tests/`、`config/`、`README.md` |

**建议落地顺序**：P1 → P2（先打通「多模型 + 切换」最小闭环）→ P3（逐项点亮能力）→ P4（收口）。

---

## 12. 影响文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `cagent/config/schema.py` | 改 | 新增 `ReasoningConfig` / `ModelsFile`；扩展 `ModelConfig`；`AgentConfig` 增 `history_token_budget_ratio` |
| `cagent/config/provider.py` | 改 | `models_path`、双文件热加载、迁移、`get_models_file`；`_to_llm_config` 透传 `max_input_tokens` |
| `cagent/llm/base.py` | 改 | `LLMConfig` 扩能力字段（含 `max_input_tokens`） |
| `cagent/llm/registry.py` | 新 | `ModelRouter`（`get` 透传 `max_input_tokens`） |
| `cagent/llm/reasoning.py` | 新 | `ReasoningAdapter`（按 vendor 翻译思考参数） |
| `cagent/llm/openai.py` | 改 | tool_calling 退化 / vision 序列化 / reasoning 参数 |
| `cagent/schema/message.py` | 改 | 新增 `ImageRef` 与 `Message.images` |
| `cagent/core/agent.py` | 改 | 接 `model_router` + `run(model=)` + `_resolve_llm(model)` |
| `cagent/core/loop.py` | 改 | `_apply_window` 增加 token 预算裁剪（消费 `max_input_tokens`） |
| `clients/cli/main.py` | 改 | `--model` / `models list` 子命令 |
| `config/models.json.example` | 新 | UI 落盘格式示例 |
| `config/agent.yaml.example` | 改 | `model:` 标 deprecated |

---

## 13. 风险与开放问题

1. **`vendor` 枚举**：本方案 `vendor` 仅驱动思考翻译，取值未穷举校验（未知 vendor 走 `enable_thinking` 兜底）。若未来引入非 OpenAI 兼容协议（突破 ADR-1），需在此扩展。
2. **MemoryService 模型跟随**：本期 MemoryService 跟随 run 的模型（保持简单）。若后期想省成本，可再加独立 cheap 模型（预留 `model_roles.memory` 入口，但本方案不实现）。
3. **`maxInputTokens` 已接入 history 窗口裁剪** ✅：见 `core/loop.py._apply_window` —— active 模型配置 `maxInputTokens` 后，history 部分的估算 token 不得超过 `max_input_tokens * history_token_budget_ratio`（默认 0.6，其余留给 system/goal/step/response），超出则淘汰最旧 step（与 `history_summarize` 联动滚动摘要）。token 估算为免外部分词器的近似（中文 1 字≈1 token、其他 4 字符≈1 token，乘 1.1 安全系数）。`history_window` 计数裁剪与 token 预算裁剪取更保守者。
4. **并发 run 共享 router 缓存**：`ModelRouter._cache` 是进程内 dict，单线程 CLI 无虞；若 WebUI 多线程，需加锁或 per-request 重建（实现期确认线程模型）。
5. **图片 base64 体积**：本地图片转 base64 可能撑爆上下文，建议实现期对 `vision` 模型加单图大小上限（如 5MB）并 `logger.warning` 超限丢弃。
