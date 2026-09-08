"""失败账本：记录工具调用失败，防止重复踩坑。"""
import json
from typing import Dict, List, Optional

# ===== 错误类型分层 =====
# 硬错误：契约/环境级（工具不存在、参数缺失）——重复失败说明该路径不可行，允许计入熔断。
HARD_ERROR_KINDS = {"TOOL_UNAVAILABLE", "PARAM_MISSING"}
# 软错误：单次用法/瞬时环境问题（路径越界、超时、执行失败）——换参数/换路径即可恢复，
# 只记数提示，不计入总熔断，避免"find 越界失败连坐写文件通道"这类误伤。
SOFT_ERROR_KINDS = {"SANDBOX", "TIMEOUT", "EXEC_ERROR", "SCHEME_PATH_MISUSE"}


class FailureLedger:
    """失败账本：记录工具调用失败，支持重试预算控制。

    核心功能：
    1. 记录每次工具调用的失败原因
    2. 检测重复失败（相同工具 + 相同参数）
    3. 达到重试预算时触发熔断（仅硬错误驱动；软错误给更高重试预算且不计入总熔断）
    """

    def __init__(
        self,
        max_retries_per_tool: int = 2,
        max_total_failures: int = 5,
        soft_max_retries: int = 3,
    ):
        """
        Args:
            max_retries_per_tool: 单个工具的最大重试次数（默认 2，适用于硬错误）
            max_total_failures: 单次 run 的最大失败次数（默认 5，仅由硬错误累计）
            soft_max_retries: 软错误（SANDBOX/TIMEOUT/EXEC_ERROR 等）同工具的重试上限，
                高于硬错误上限，给模型试错空间（默认 3）
        """
        self.max_retries_per_tool = max_retries_per_tool
        self.max_total_failures = max_total_failures
        self.soft_max_retries = soft_max_retries
        # 记录每个工具的失败次数：{tool_name: count}
        self._tool_failures: Dict[str, int] = {}
        # 记录每次失败的原因：{tool_name: error_kind}
        self._failure_reasons: Dict[str, str] = {}
        # 总失败次数（仅硬错误累计）
        self._total_failures = 0

    def record_failure(
        self,
        tool_name: str,
        error_kind: Optional[str] = None,
    ) -> None:
        """记录一次工具调用失败。

        Args:
            tool_name: 工具名称
            error_kind: 错误类型（如 "TOOL_UNAVAILABLE", "SANDBOX", "TIMEOUT" 等）。
                软错误（SANDBOX/TIMEOUT/EXEC_ERROR/SCHEME_PATH_MISUSE 及未知类型）
                只记入单工具计数，不计入总熔断。
        """
        self._tool_failures[tool_name] = self._tool_failures.get(tool_name, 0) + 1
        if error_kind:
            self._failure_reasons[tool_name] = error_kind
        # 只有硬错误才累计总熔断；软错误多为单次用法问题，不应泛化为"工具不可用"
        if error_kind in HARD_ERROR_KINDS:
            self._total_failures += 1

    def get_failure_count(self, tool_name: str) -> int:
        """获取指定工具的失败次数。"""
        return self._tool_failures.get(tool_name, 0)

    def get_last_error_kind(self, tool_name: str) -> Optional[str]:
        """获取指定工具的上次错误类型。"""
        return self._failure_reasons.get(tool_name)

    def _retry_limit(self, tool_name: str) -> int:
        """按最近一次错误类型确定重试上限：软错误用宽松预算。"""
        kind = self._failure_reasons.get(tool_name)
        if kind in SOFT_ERROR_KINDS or kind is None:
            return self.soft_max_retries
        return self.max_retries_per_tool

    def should_retry(self, tool_name: str) -> bool:
        """判断指定工具是否应该重试。

        返回 False 表示已达到重试预算，应触发熔断。
        """
        return self.get_failure_count(tool_name) < self._retry_limit(tool_name)

    def is_circuit_broken(self) -> bool:
        """判断是否应该触发熔断（硬错误总数达到上限）。"""
        return self._total_failures >= self.max_total_failures

    def get_circuit_break_hint(self) -> str:
        """生成熔断提示，告诉 LLM 应该停止重试。"""
        return (
            f"已达到最大失败次数 {self.max_total_failures}，"
            "请停止重试当前策略，改用其他方法或总结当前进度。"
        )

    def get_retry_hint(self, tool_name: str) -> str:
        """生成重试提示，告诉 LLM 该工具已经失败多次。"""
        count = self.get_failure_count(tool_name)
        error_kind = self.get_last_error_kind(tool_name) or "未知错误"
        if error_kind in SOFT_ERROR_KINDS or error_kind == "未知错误":
            advice = (
                "这是单次用法或环境类失败，请根据上方错误详情调整参数/路径后重试，"
                "工具本身仍然可用。"
            )
        else:
            advice = "建议改用其他工具或调整参数。"
        return (
            f"工具 {tool_name} 已失败 {count} 次（错误类型: {error_kind}），{advice}"
        )

    def reset(self) -> None:
        """重置账本（新 run 时调用）。"""
        self._tool_failures.clear()
        self._failure_reasons.clear()
        self._total_failures = 0

    def to_dict(self) -> dict:
        """序列化为字典（用于持久化）。"""
        return {
            "tool_failures": self._tool_failures,
            "failure_reasons": self._failure_reasons,
            "total_failures": self._total_failures,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FailureLedger":
        """从字典反序列化。"""
        ledger = cls()
        ledger._tool_failures = data.get("tool_failures", {})
        ledger._failure_reasons = data.get("failure_reasons", {})
        ledger._total_failures = data.get("total_failures", 0)
        return ledger
