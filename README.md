# cagent

一个轻量级 Python Agent 运行框架，将 **Plan-Executor** 与 **ReAct** 两种范式结合：

- 上层 `Planner` 生成结构化 `Plan`（若干 `Step`），`Executor` 按依赖调度执行；
- 每个 `Step` 内部由 `ReActEngine` 运行 `Thought → Action → Observation` 多轮循环，直到收敛；
- `AgentLoop` 负责整体编排与重规划。

详见 [AGENTS.md](./AGENTS.md)。

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

> 核心 loop 已实现：Planner（LLM→Plan 解析）、ReActEngine（Thought→Action→Observation 工具调用闭环）、AgentLoop 编排均已可用；配置 yaml 加载、执行摘要 LLM 精炼等为后续增强点。
