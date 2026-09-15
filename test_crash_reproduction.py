"""复现原始崩溃场景：LLM 传入错误格式的参数给 run_tool"""
import json
import tempfile
from cagent.core import Agent
from cagent.tools.registry import ToolRegistry
from cagent.tools.builtin import add
from cagent.plugins.tree import OperationTree
from cagent.plugins.guide import ToolGuideTool, RunToolTool
from cagent.storage import get_storage
from cagent.llm.base import LLMClient, LLMResponse


class MockLLM(LLMClient):
    """模拟 LLM，返回错误格式的工具调用参数"""
    
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
    
    def complete(self, messages, **kwargs):
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        return LLMResponse(content="完成")
    
    def complete_with_tools(self, messages, tool_schemas=None):
        return self.complete(messages)


def test_crash_reproduction():
    """测试原始崩溃场景：LLM 将 file 参数到顶层而非 params 字典内"""
    
    # 1. Plan 响应
    plan_resp = LLMResponse(content=json.dumps({
        "steps": [
            {"id": "s1", "description": "使用 run_tool 查看文件", "depends_on": []}
        ]
    }))
    
    # 2. ReAct 响应：LLM 错误地将 file 参数放在顶层（原始崩溃场景）
    react_tool_call = LLMResponse(
        content="我需要查看文件内容",
        tool_calls=[{
            "id": "call_1",
            "function": {
                "name": "run_tool",
                # 错误格式：file 应该在 params 内，而非顶层
                "arguments": json.dumps({
                    "path": "shell.sed",
                    "file": "test.py",  # ❌ 错误：应该在 params 内
                    "start_line": 1,
                    "end_line": 10
                })
            }
        }]
    )
    
    # 3. 最终响应
    react_final = LLMResponse(content="任务完成")
    
    # 4. 总结响应
    summarize_resp = LLMResponse(content="最终结果：任务已完成")
    
    # 构造 LLM
    llm = MockLLM([plan_resp, react_tool_call, react_final, summarize_resp])
    
    # 构造工具注册表
    reg = ToolRegistry()
    reg.register(add)
    
    # 构造操作树和工具
    tree = OperationTree()
    reg.register(ToolGuideTool(tree))
    reg.register(RunToolTool(tree))
    
    # 构造 Agent
    storage = get_storage(tempfile.mkdtemp())
    agent = Agent(llm=llm, tools=reg, storage=storage, max_steps=10)
    
    # 执行：应该触发参数过滤逻辑，而不是 TypeError
    print("=" * 60)
    print("开始测试：复现原始崩溃场景")
    print("=" * 60)
    print("\n场景：LLM 将 file 参数放在顶层而非 params 字典内")
    print("预期：参数过滤逻辑应该过滤掉多余参数，记录警告日志")
    print("\n" + "=" * 60)
    
    try:
        result = agent.run("查看 test.py 文件的前 10 行")
        print("\n" + "=" * 60)
        print("✅ 测试通过：未抛出 TypeError")
        print(f"结果类型: {type(result).__name__}")
        print(f"答案: {result.answer[:100]}")
        print(f"计划步骤数: {len(result.plan.steps)}")
        print("=" * 60)
        return True
    except TypeError as e:
        print("\n" + "=" * 60)
        print(f"❌ 测试失败：仍然抛出 TypeError")
        print(f"错误: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return False
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"⚠️  测试异常：抛出其他异常")
        print(f"错误类型: {type(e).__name__}")
        print(f"错误: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s %(name)s:%(filename)s:%(lineno)d %(funcName)s %(message)s'
    )
    
    success = test_crash_reproduction()
    exit(0 if success else 1)
