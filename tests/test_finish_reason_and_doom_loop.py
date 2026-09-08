"""Tests for finish_reason-based termination and doom loop detection.

Covers:
- LLMResponse.finish_reason field propagation
- ReActEngine._should_terminate_loop() logic
- DoomLoopDetector behavior
- Integration: finish_reason affects StepResult
"""
import pytest
from unittest.mock import Mock, MagicMock
from cagent.core.doom_loop import DoomLoopDetector
from cagent.core.react import ReActEngine
from cagent.llm.base import LLMResponse
from cagent.schema.plan import Step, StepStatus
from cagent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# DoomLoopDetector tests
# ---------------------------------------------------------------------------

class TestDoomLoopDetector:
    """Test doom loop detection logic."""

    def test_no_warning_below_threshold(self):
        """Detector should not warn when calls < threshold."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        result1 = detector.record("read_file", {"path": "/test.py"})
        assert result1 is None
        
        result2 = detector.record("read_file", {"path": "/test.py"})
        assert result2 is None
        # Only 2 calls, threshold is 3, no warning yet

    def test_warning_at_threshold(self):
        """Detector should warn when reaching threshold identical calls."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        detector.record("read_file", {"path": "/test.py"})
        detector.record("read_file", {"path": "/test.py"})
        # 3rd call reaches threshold
        result = detector.record("read_file", {"path": "/test.py"})
        
        assert result is not None
        assert "死循环检测" in result
        assert "read_file" in result

    def test_warning_only_once(self):
        """Detector should only warn once per doom loop pattern."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        detector.record("read_file", {"path": "/test.py"})
        detector.record("read_file", {"path": "/test.py"})
        result1 = detector.record("read_file", {"path": "/test.py"})
        result2 = detector.record("read_file", {"path": "/test.py"})
        
        assert result1 is not None
        assert result2 is None  # No repeated warning

    def test_different_args_reset_warning(self):
        """Different arguments should reset the warning state."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        detector.record("read_file", {"path": "/test1.py"})
        detector.record("read_file", {"path": "/test1.py"})
        detector.record("read_file", {"path": "/test1.py"})  # Warning here
        
        # Different args - breaks the pattern, warning resets
        detector.record("read_file", {"path": "/test2.py"})
        detector.record("read_file", {"path": "/test2.py"})
        result = detector.record("read_file", {"path": "/test2.py"})
        
        # Warning fires again for the new pattern
        assert result is not None
        assert "死循环检测" in result
        assert "read_file" in result

    def test_reset_clears_state(self):
        """Reset should clear all history and warning state."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        detector.record("read_file", {"path": "/test.py"})
        detector.record("read_file", {"path": "/test.py"})
        detector.record("read_file", {"path": "/test.py"})  # Warning
        
        detector.reset()
        
        # After reset, should not warn on first call
        result = detector.record("read_file", {"path": "/test.py"})
        assert result is None

    def test_window_size_limits_history(self):
        """History should not exceed window_size."""
        detector = DoomLoopDetector(window_size=3, threshold=3)
        
        # Fill beyond window
        detector.record("tool_a", {"x": 1})
        detector.record("tool_b", {"x": 2})
        detector.record("tool_c", {"x": 3})
        detector.record("tool_d", {"x": 4})  # Should evict tool_a
        
        # Now only tool_b, tool_c, tool_d in history
        # tool_b + tool_c + tool_d are different, no warning
        result = detector.record("tool_b", {"x": 2})
        assert result is None

    def test_custom_threshold(self):
        """Custom threshold should be respected."""
        detector = DoomLoopDetector(window_size=10, threshold=5)
        
        # 4 calls - no warning
        for _ in range(4):
            result = detector.record("tool", {"x": 1})
            assert result is None
        
        # 5th call - warning
        result = detector.record("tool", {"x": 1})
        assert result is not None

    def test_different_tools_no_warning(self):
        """Different tools should not trigger doom loop."""
        detector = DoomLoopDetector(window_size=5, threshold=3)
        
        detector.record("tool_a", {"x": 1})
        detector.record("tool_b", {"x": 1})
        detector.record("tool_c", {"x": 1})
        
        result = detector.record("tool_a", {"x": 1})
        assert result is None


# ---------------------------------------------------------------------------
# finish_reason termination tests
# ---------------------------------------------------------------------------

class TestFinishReasonTermination:
    """Test ReActEngine._should_terminate_loop() logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.llm = Mock()
        self.tools = ToolRegistry()
        self.engine = ReActEngine(llm=self.llm, tools=self.tools)

    def test_stop_means_model_done(self):
        """finish_reason='stop' should terminate with model_done."""
        resp = LLMResponse(content="Done", tool_calls=[], finish_reason="stop")
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "model_done"

    def test_end_turn_means_model_done(self):
        """finish_reason='end_turn' should terminate with model_done."""
        resp = LLMResponse(content="Done", tool_calls=[], finish_reason="end_turn")
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "model_done"

    def test_length_means_truncated(self):
        """finish_reason='length' should terminate with truncated."""
        resp = LLMResponse(content="Partial", tool_calls=[], finish_reason="length")
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "truncated"

    def test_content_filter_means_filtered(self):
        """finish_reason='content_filter' should terminate with filtered."""
        resp = LLMResponse(content="", tool_calls=[], finish_reason="content_filter")
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "filtered"

    def test_none_with_no_tool_calls_means_compat_done(self):
        """finish_reason=None with no tool_calls should terminate with compat_done."""
        resp = LLMResponse(content="Done", tool_calls=[], finish_reason=None)
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "compat_done"

    def test_tool_calls_override_finish_reason(self):
        """tool_calls present should continue loop regardless of finish_reason."""
        resp = LLMResponse(
            content="",
            tool_calls=[{"function": {"name": "read", "arguments": "{}"}}],
            finish_reason="stop"
        )
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is False
        assert reason == "continue"

    def test_empty_finish_reason_string_means_compat(self):
        """Empty finish_reason string should be treated as compat_done."""
        resp = LLMResponse(content="Done", tool_calls=[], finish_reason="")
        should_term, reason = self.engine._should_terminate_loop(resp)
        
        assert should_term is True
        assert reason == "compat_done"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestFinishReasonIntegration:
    """Test that finish_reason affects StepResult."""

    def setup_method(self):
        """Set up test fixtures."""
        self.llm = Mock()
        self.tools = ToolRegistry()
        self.engine = ReActEngine(llm=self.llm, tools=self.tools)

    def test_truncated_returns_failure(self):
        """Truncated response should return success=False."""
        step = Step(id="test", description="Test step")
        
        # Mock LLM to return truncated response
        self.llm.complete_with_tools.return_value = LLMResponse(
            content="Partial content",
            tool_calls=[],
            finish_reason="length"
        )
        
        # Mock the summarize fallback
        self.llm.complete.return_value = LLMResponse(
            content="Summarized partial content",
            tool_calls=[],
            finish_reason="stop"
        )
        
        result = self.engine.run(step)
        
        assert result.success is False
        assert "截" in result.error or "token" in result.error.lower()

    def test_model_done_returns_success(self):
        """finish_reason='stop' should return success=True."""
        step = Step(id="test", description="Test step")
        
        # Mock LLM to return completed response
        self.llm.complete_with_tools.return_value = LLMResponse(
            content="Task completed",
            tool_calls=[],
            finish_reason="stop"
        )
        
        result = self.engine.run(step)
        
        assert result.success is True
        assert result.output == "Task completed"

    def test_compat_done_returns_success(self):
        """finish_reason=None should return success=True (backward compat)."""
        step = Step(id="test", description="Test step")
        
        # Mock LLM to return response without finish_reason
        self.llm.complete_with_tools.return_value = LLMResponse(
            content="Task completed",
            tool_calls=[],
            finish_reason=None
        )
        
        result = self.engine.run(step)
        
        assert result.success is True
        assert result.output == "Task completed"
