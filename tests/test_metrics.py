"""度量系统测试。"""
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from cagent.metrics.schema import ToolCallRecord, StepMetrics, TurnMetrics, ToolAggregation
from cagent.metrics.collector import MetricsCollector
from cagent.metrics.backend import JsonlMetricsBackend
from cagent.events.schema import AgentEvent, EventType


def test_tool_call_record():
    """测试 ToolCallRecord 序列化。"""
    record = ToolCallRecord(
        tool_name="shell_exec",
        args={"command": "ls"},
        ok=True,
        duration_ms=100,
        observation_length=500,
        step_id="step_1",
        seq=1,
    )
    json_str = record.model_dump_json()
    restored = ToolCallRecord.model_validate_json(json_str)
    assert restored.tool_name == "shell_exec"
    assert restored.args == {"command": "ls"}
    assert restored.ok is True
    assert restored.duration_ms == 100


def test_step_metrics():
    """测试 StepMetrics 序列化。"""
    step = StepMetrics(
        step_id="step_1",
        description="测试步骤",
        status="done",
        react_iterations=2,
        llm_calls=3,
        duration_ms=5000,
        doom_loop_detected=False,
    )
    step.tool_calls.append(
        ToolCallRecord(
            tool_name="shell_exec",
            args={"command": "ls"},
            ok=True,
            duration_ms=100,
            observation_length=500,
            step_id="step_1",
            seq=1,
        )
    )
    json_str = step.model_dump_json()
    restored = StepMetrics.model_validate_json(json_str)
    assert restored.step_id == "step_1"
    assert restored.status == "done"
    assert len(restored.tool_calls) == 1


def test_turn_metrics_finalize():
    """测试 TurnMetrics.finalize() 聚合逻辑。"""
    turn = TurnMetrics(
        session_id="test_session",
        goal="测试任务",
    )
    # 添加两个 step
    step1 = StepMetrics(
        step_id="step_1",
        description="步骤1",
        status="done",
        llm_calls=2,
    )
    step1.tool_calls.append(
        ToolCallRecord(
            tool_name="shell_exec",
            real_tool_name="ls",
            source="builtin",
            args={"command": "ls"},
            ok=True,
            duration_ms=100,
            observation_length=500,
            step_id="step_1",
            seq=1,
        )
    )
    turn.steps.append(step1)

    step2 = StepMetrics(
        step_id="step_2",
        description="步骤2",
        status="failed",
        llm_calls=1,
    )
    step2.tool_calls.append(
        ToolCallRecord(
            tool_name="read_file",
            source="plugin",
            args={"path": "test.txt"},
            ok=False,
            duration_ms=200,
            observation_length=0,
            step_id="step_2",
            seq=2,
        )
    )
    turn.steps.append(step2)

    # 调用 finalize 计算聚合
    turn.finalize()

    assert turn.total_steps == 2
    assert turn.successful_steps == 1
    assert turn.failed_steps == 1
    assert turn.total_tool_calls == 2
    assert turn.total_llm_calls == 3

    # 检查工具聚合
    assert "ls" in turn.tool_summary
    assert "read_file" in turn.tool_summary
    assert turn.tool_summary["ls"].call_count == 1
    assert turn.tool_summary["read_file"].call_count == 1
    assert turn.tool_summary["read_file"].fail_count == 1


def test_jsonl_backend():
    """测试 JsonlMetricsBackend 读写。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        backend = JsonlMetricsBackend(tmpdir)
        
        # 创建测试数据
        turn1 = TurnMetrics(
            session_id="session_1",
            goal="任务1",
            status="done",
        )
        turn2 = TurnMetrics(
            session_id="session_1",
            goal="任务2",
            status="failed",
        )
        
        # 保存
        backend.save_turn("session_1", turn1)
        backend.save_turn("session_1", turn2)
        
        # 读取
        turns = backend.load_turns("session_1")
        assert len(turns) == 2
        assert turns[0].goal == "任务1"
        assert turns[1].goal == "任务2"
        
        # 列出会话
        sessions = backend.list_sessions()
        assert "session_1" in sessions


def test_metrics_collector_basic():
    """测试 MetricsCollector 基本事件处理。"""
    collector = MetricsCollector()
    
    # 模拟 TURN_STARTED
    collector.handle_event(AgentEvent(
        type=EventType.TURN_STARTED,
        session_id="test_session",
        payload={"goal": "测试任务"},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 STEP_STARTED
    collector.handle_event(AgentEvent(
        type=EventType.STEP_STARTED,
        session_id="test_session",
        step_id="step_1",
        payload={"step": {"id": "step_1", "description": "步骤1"}},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 TOOL_CALL
    collector.handle_event(AgentEvent(
        type=EventType.TOOL_CALL,
        session_id="test_session",
        step_id="step_1",
        payload={"name": "shell_exec", "args": {"command": "ls"}},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 REAL_TOOL_EXEC
    collector.handle_event(AgentEvent(
        type=EventType.REAL_TOOL_EXEC,
        session_id="test_session",
        step_id="step_1",
        payload={"real_tool_name": "ls", "source": "builtin"},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 TOOL_RESULT
    collector.handle_event(AgentEvent(
        type=EventType.TOOL_RESULT,
        session_id="test_session",
        step_id="step_1",
        payload={"name": "shell_exec", "content": "file1\nfile2", "ok": True},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 STEP_FINISHED
    collector.handle_event(AgentEvent(
        type=EventType.STEP_FINISHED,
        session_id="test_session",
        step_id="step_1",
        payload={"step": {"id": "step_1"}, "result": {"success": True}},
        ts=datetime.now(timezone.utc),
    ))
    
    # 模拟 TURN_FINISHED
    collector.handle_event(AgentEvent(
        type=EventType.TURN_FINISHED,
        session_id="test_session",
        payload={"status": "done"},
        ts=datetime.now(timezone.utc),
    ))
    
    # 验证度量数据
    turn = collector.get_current_turn()
    assert turn is not None
    assert turn.session_id == "test_session"
    assert turn.goal == "测试任务"
    assert turn.status == "done"
    assert len(turn.steps) == 1
    assert turn.steps[0].step_id == "step_1"
    assert turn.steps[0].status == "done"
    assert len(turn.steps[0].tool_calls) == 1
    assert turn.steps[0].tool_calls[0].tool_name == "shell_exec"
    assert turn.steps[0].tool_calls[0].real_tool_name == "ls"
    assert turn.steps[0].tool_calls[0].source == "builtin"


if __name__ == "__main__":
    test_tool_call_record()
    print("✓ test_tool_call_record")
    
    test_step_metrics()
    print("✓ test_step_metrics")
    
    test_turn_metrics_finalize()
    print("✓ test_turn_metrics_finalize")
    
    test_jsonl_backend()
    print("✓ test_jsonl_backend")
    
    test_metrics_collector_basic()
    print("✓ test_metrics_collector_basic")
    
    print("\n所有测试通过！")
