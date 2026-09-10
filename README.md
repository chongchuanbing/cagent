# cagent

一个轻量级 Python Agent 运行框架，将 **Plan-Executor** 与 **ReAct** 两种范式结合：

- 上层 `Planner` 生成结构化 `Plan`（若干 `Step`），`Executor` 按依赖调度执行；
- 每个 `Step` 内部由 `ReActEngine` 运行 `Thought → Action → Observation` 多轮循环，直到收敛；
- `AgentLoop` 负责整体编排与重规划。

详见 [AGENTS.md](./AGENTS.md)。多模型能力的设计细节见 [docs/design/multi-model-design.md](./docs/design/multi-model-design.md)。

## 安装

```bash
pip install -e .
```

## 最小用法

```python
from cagent.llm import OpenAIClient, LLMConfig
from cagent.tools import ToolRegistry, calculator
from cagent.core import Agent

llm = OpenAIClient(LLMConfig(model="gpt-4o", api_key="<YOUR_KEY>"))
registry = ToolRegistry()
registry.register(calculator)

agent = Agent(llm=llm, tools=registry)
# answer = agent.run("计算 3 + 5 并解释结果")
```

> 直接传入 `llm=` 是「显式注入单模型」路径，优先级最高，会跳过 `models.json` 路由。

## 多模型配置

cagent 支持在 `config/models.json` 中登记**多个模型**、指定**默认运行模型**，并在运行时**按 run 切换**。模型配置是 **UI 落盘格式（camelCase）**，由 WebUI / Desktop 端负责写盘，cagent 只读。`agent.yaml` **不再包含 `model` 字段**。

### 配置文件 `config/models.json`

```json
{
  "default": "qwen3.7-plus",
  "models": [
    {
      "id": "qwen3.7-plus",
      "name": "Qwen3.7 Plus (DashScope)",
      "vendor": "Qwen",
      "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "apiKey": "${DASHSCOPE_API_KEY}",
      "supportsToolCall": true,
      "supportsImages": false,
      "supportsReasoning": true,
      "reasoning": { "effort": "high" },
      "maxInputTokens": 131072,
      "maxOutputTokens": 131072
    },
    {
      "id": "qwen-vl",
      "name": "Qwen-VL",
      "vendor": "Qwen",
      "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "apiKey": "${DASHSCOPE_API_KEY}",
      "supportsToolCall": true,
      "supportsImages": true,
      "supportsReasoning": false
    }
  ]
}
```

**字段说明**

| 字段 | 含义 |
|------|------|
| `default` | 默认模型 id（指向 `models[].id`）；缺失时取首项 |
| `id` | 发给 API 的模型名，也是路由 key（`--model <id>`） |
| `name` | 仅展示用 |
| `vendor` | 仅驱动思考参数翻译（`openai` / `qwen` / `deepseek` / `glm` / `custom`） |
| `url` | OpenAI 兼容端点 |
| `apiKey` | 支持 `${ENV}` 环境变量展开 |
| `supportsToolCall` | ReAct 必须 `true`，否则启动期报错 |
| `supportsImages` | 是否支持多模态图片输入 |
| `supportsReasoning` | 是否启用思考模式 |
| `reasoning.effort` | 思考强度 `low` / `medium` / `high` |
| `reasoning.budgetTokens` | 思考预算 token |
| `reasoning.extraBody` | 厂商原生参数透传 |
| `maxInputTokens` / `maxOutputTokens` | 上下文 / 输出上限；`maxInputTokens` 驱动 history 窗口的 token 预算裁剪（见下） |

> `useCustomProtocol` 字段被忽略：接口协议**仅支持 OpenAI 兼容**（`/chat/completions`）。

模板见 [`config/models.json.example`](./config/models.json.example)。

### 运行时切换

**CLI**

```bash
# 用默认模型运行
python -m clients.cli run "帮我算 12 * 13"

# 指定本次运行使用的模型
python -m clients.cli run "用强模型重做" --model qwen3.7-plus

# 列出已配置模型（id / name / vendor / 能力开关 / 默认★）
python -m clients.cli models list
```

**Python API**

```python
agent = Agent(config=cfg, tools=registry)   # 从 models.json 构建路由
agent.run("任务目标")                         # 用 default 模型
agent.run("任务目标", model="qwen3.7-plus")   # 指定模型
```

### 能力落地规则

- **工具调用**：`supportsToolCall=false` 的模型不能用于 ReAct（启动期 `ValueError` 拦截）。
- **图片输入**：`supportsImages=true` 时，`Message.images` 会序列化为 OpenAI 多模态 `content`；非 vision 模型收到图片会**丢弃并告警**，纯文本链路不崩。
- **思考模式**：`supportsReasoning=true` 时按 `vendor` 翻译为 `reasoning_effort`（openai）或 `enable_thinking`（qwen/deepseek/glm/custom），并强制 `temperature=0`（o-series / 思考类模型拒绝非零温度）。

### 热加载

`agent.yaml` 与 `models.json` 任意一个被修改后，下一次 run 自动生效（守护线程每秒轮询 mtime），无需重启。

### history token 预算裁剪

当 active 模型在 `models.json` 配置了 `maxInputTokens` 时，跨 step 的 history 上下文会按 **token 预算** 自动裁剪：history 部分占用的估算 token 不超过 `maxInputTokens × history_token_budget_ratio`（默认 0.6，其余留给 system / goal / 当前 step / 回复），超出则淘汰最旧 step；被淘汰的条目按 `history_summarize` 配置滚成一条摘要置顶保留（无 LLM 时退化为文本压缩）。

- 计数裁剪（`history_window`）与 token 预算裁剪**取更保守者**（任一约束都能触发淘汰）。
- `history_token_budget_ratio` 可在 `agent.yaml` 调整（设为 `0` 即关闭 token 预算，仅按 `history_window` 计数裁剪）。
- token 估算为免外部分词器的近似：中文约 1 字≈1 token、其他约 4 字符≈1 token，并乘 1.1 安全系数（略微高估以降低真超限风险）。

## 校验护栏

| 护栏 | 触发点 | 行为 |
|------|--------|------|
| ReAct 目标必须 `tool_calling` | `Agent.run` / `ModelRouter.validate_react_target` | `ValueError` 清晰报错 |
| `default` 指向不存在的 id | `ConfigProvider` 加载 | `ValueError` |
| 非 vision 模型收到图片 | `OpenAIClient` 序列化 | 丢弃 + `logger.warning`，链路不崩 |
| 思考模型 temperature | `OpenAIClient._request_kwargs` | 强制 `temperature=0` |
