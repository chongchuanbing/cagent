"""最小可运行示例（骨架阶段仅验证组装，未接真实 LLM 逻辑）。"""
from cagent.llm import OpenAIClient, LLMConfig
from cagent.tools import ToolRegistry, calculator
from cagent.core import Agent


def main():
    llm = OpenAIClient(LLMConfig(model="gpt-4o", api_key="<YOUR_KEY>"))
    registry = ToolRegistry()
    registry.register(calculator)

    agent = Agent(llm=llm, tools=registry)
    # answer = agent.run("计算 3 + 5 并解释结果")
    # print(answer)
    print("Agent 组装成功，调用 agent.run(goal) 即可执行。")


if __name__ == "__main__":
    main()
