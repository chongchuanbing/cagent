"""度量系统集成测试 - 验证工具调用耗时和观察长度的记录"""
import time
from datetime import datetime, timezone
from cagent.events.schema import AgentEvent, EventType
from cagent.metrics.collector import MetricsCollector


def test_tool_call_duration_and_observation():
    """测试工具调用的耗时和观察长度是否正确记录"""
    collector = MetricsCollector()
    
    # 模拟一个完整的 turn 和 step
    collector.handle_event(AgentEvent(
        type=EventType.TURN_STARTED,
        session_id="test_session",
        payload={"goal": "测试任务"},
        ts=datetime.now(timezone.utc),
    ))
    
    collector.handle_event(AgentEvent(
        type=EventType.STEP_STARTED,
        session_id="test_session",
        step_id="s1",
        payload={"step": {"id": "s1", "description": "测试步骤"}},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟工具调用
    start_time = datetime.now(timezone.utc)
    collector.handle_event(AgentEvent(
        type=EventType.TOOL_CALL,
        session_id="test_session",
        step_id="s1",
        payload={"name": "run_tool", "args": {"path": "shell.run_command"}},
        ts=start_time,
    ))
    
    # 模拟工具执行（真实工具）
    time.sleep(0.05)  # 模拟 50ms 的执行时间
    collector.handle_event(AgentEvent(
        type=EventType.REAL_TOOL_EXEC,
        session_id="test_session",
        step_id="s1",
        payload={"real_tool_name": "shell.run_command", "source": "shell"},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟工具结果
    end_time = datetime.now(timezone.utc)
    collector.handle_event(AgentEvent(
        type=EventType.TOOL_RESULT,
        session_id="test_session",
        step_id="s1",
        payload={
            "name": "run_tool",
            "content": "命令执行结果",
            "ok": True
        },
        ts=end_time,
    ))
    
    collector.handle_event(AgentEvent(
        type=EventType.STEP_FINISHED,
        session_id="test_session",
        step_id="s1",
        payload={"step": {"id": "s1"}, "result": {"success": True}},
        ts=datetime.now(timezone.utc),
    ))
    
    collector.handle_event(AgentEvent(
        type=EventType.TURN_FINISHED,
        session_id="test_session",
        payload={"status": "done"},
        ts=datetime.now(timezone.utc),
    ))
    
    # 验证结果
    turn = collector.get_current_turn()
    assert turn is not None
    assert len(turn.steps) == 1
    
    step = turn.steps[0]
    assert len(step.tool_calls) == 1
    
    tc = step.tool_calls[0]
    assert tc.tool_name == "run_tool"
    assert tc.real_tool_name == "shell.run_command"
    assert tc.source == "shell"
    
    # 关键验证：耗时应该 > 0（至少 40ms）
    assert tc.duration_ms >= 40, f"工具调用耗时应该 >= 40ms，实际为 {tc.duration_ms}ms"
    print(f"✓ 工具调用耗时: {tc.duration_ms}ms")
    
    # 关键验证：观察长度应该 > 0
    assert tc.observation_length == len("命令执行结果"), f"观察长度应该为 6，实际为 {tc.observation_length}"
    print(f"✓ 观察长度: {tc.observation_length}")


if __name__ == "__main__":
    test_tool_call_duration_and_observation()
    print("\n✓ 集成测试通过")
