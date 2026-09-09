"""失败账本：记录工具调用失败，防止重复踩坑。

熔断粒度设计：
- **细粒度（主）**：按 (tool_name, args_key) 计数。同一工具对不同参数的调用
  独立计数——比如 shell.sed 读 A 文件失败不应连累读 B 文件的预算。
- **粗粒度（兜底）**：按 tool_name 计数。完全相同的 (tool, args) 反复调用
  即 doom loop 场景，由上层 DoomLoopDetector 处理；这里做第二道防线，
  同一工具整体失败太多次时仍然熔断。

查询时优先查细粒度，仅当 args_key 为空（调用方未传 args）时回退到粗粒度。
"""
import json
from typing import Dict, Optional, Tuple

from ..utils.logging import get_logger

logger = get_logger(__name__)

# ===== 错误类型分层 =====
# 硬错误：契约/环境级（工具不存在、参数缺失）——重复失败说明该路径不可行，允许计入熔断。
HARD_ERROR_KINDS = {"TOOL_UNAVAILABLE", "PARAM_MISSING"}
# 软错误：单次用法/瞬时环境问题（路径越界、超时、执行失败）——换参数/换路径即可恢复，
# 只记数提示，不计入总熔断，避免"find 越界失败连坐写文件通道"这类误伤。
SOFT_ERROR_KINDS = {"SANDBOX", "TIMEOUT", "EXEC_ERROR", "SCHEME_PATH_MISUSE"}

# args_key 最大长度，防止超大参数撑爆内存
_ARGS_KEY_MAX_LEN = 256


def make_args_key(args: Optional[dict]) -> str:
    """将工具参数 dict 规范化为字符串 key。

    - args 为 None 或空 → 返回 ""（调用方未传参，回退到 tool_name 级粒度）
    - 序列化后截断到 _ARGS_KEY_MAX_LEN，防止超大参数撑爆内存
    """
    if not args:
        return ""
    try:
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        key = str(sorted(args.items()) if isinstance(args, dict) else args)
    return key[:_ARGS_KEY_MAX_LEN]


class FailureLedger:
    """失败账本：双层粒度的重试预算控制。

    核心功能：
    1. 细粒度：按 (tool_name, args_key) 记录失败——不同参数的调用独立计数
    2. 粗粒度：按 tool_name 汇总——兜底 doom loop 防护
    3. 硬/软错误分层：仅硬错误计入全局熔断
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
        # 细粒度：(tool_name, args_key) → count
        self._fine_failures: Dict[Tuple[str, str], int] = {}
        # 粗粒度：tool_name → count（兜底）
        self._tool_failures: Dict[str, int] = {}
        # 最近一次失败原因（细粒度）：(tool_name, args_key) → error_kind
        self._fine_reasons: Dict[Tuple[str, str], str] = {}
        # 最近一次失败原因（粗粒度）：tool_name → error_kind
        self._failure_reasons: Dict[str, str] = {}
        # 总失败次数（仅硬错误累计）
        self._total_failures = 0

    def record_failure(
        self,
        tool_name: str,
        error_kind: Optional[str] = None,
        args: Optional[dict] = None,
    ) -> None:
        """记录一次工具调用失败。

        Args:
            tool_name: 工具名称
            error_kind: 错误类型（如 "TOOL_UNAVAILABLE", "SANDBOX", "TIMEOUT" 等）。
                软错误只记入单工具计数，不计入总熔断。
            args: 工具调用参数。提供后按 (tool_name, args_key) 细粒度计数；
                不提供则回退到 tool_name 级粒度（向后兼容）。
        """
        args_key = make_args_key(args)

        # 细粒度
        fine_key = (tool_name, args_key)
        self._fine_failures[fine_key] = self._fine_failures.get(fine_key, 0) + 1
        if error_kind:
            self._fine_reasons[fine_key] = error_kind

        # 粗粒度（始终记录，作为兜底）
        self._tool_failures[tool_name] = self._tool_failures.get(tool_name, 0) + 1
        if error_kind:
            self._failure_reasons[tool_name] = error_kind

        # 只有硬错误才累计总熔断
        if error_kind in HARD_ERROR_KINDS:
            self._total_failures += 1
            logger.info(f"硬错误累计: {tool_name} ({error_kind}), 总计 {self._total_failures}/{self.max_total_failures}")

        logger.debug(f"记录失败: {tool_name}, 类型={error_kind}, 细粒度计数={self._fine_failures.get(fine_key, 0)}")

    def get_failure_count(self, tool_name: str, args: Optional[dict] = None) -> int:
        """获取指定工具的失败次数。

        优先查细粒度（有 args_key 时），否则查粗粒度。
        """
        args_key = make_args_key(args)
        if args_key:
            fine = self._fine_failures.get((tool_name, args_key), 0)
            if fine > 0:
                return fine
        return self._tool_failures.get(tool_name, 0)

    def get_last_error_kind(self, tool_name: str, args: Optional[dict] = None) -> Optional[str]:
        """获取指定工具的上次错误类型。优先细粒度。"""
        args_key = make_args_key(args)
        if args_key:
            fine = self._fine_reasons.get((tool_name, args_key))
            if fine is not None:
                return fine
        return self._failure_reasons.get(tool_name)

    def _retry_limit(self, tool_name: str, args: Optional[dict] = None) -> int:
        """按最近一次错误类型确定重试上限：软错误用宽松预算。"""
        kind = self.get_last_error_kind(tool_name, args)
        if kind in SOFT_ERROR_KINDS or kind is None:
            return self.soft_max_retries
        return self.max_retries_per_tool

    def should_retry(self, tool_name: str, args: Optional[dict] = None) -> bool:
        """判断指定工具是否应该重试。

        返回 False 表示已达到重试预算，应触发熔断。

        粒度策略：
        - 有 args → 按 (tool_name, args_key) 细粒度判断（不同参数独立预算）
        - 无 args → 回退到 tool_name 粗粒度（向后兼容）
        """
        args_key = make_args_key(args)
        if args_key:
            # 细粒度：同工具不同参数独立计数，互不影响
            fine_count = self._fine_failures.get((tool_name, args_key), 0)
            fine_limit = self._retry_limit(tool_name, args)
            return fine_count < fine_limit
        # 粗粒度兜底（无 args 时）
        coarse_count = self._tool_failures.get(tool_name, 0)
        coarse_limit = self._retry_limit(tool_name, args)
        return coarse_count < coarse_limit

    def is_circuit_broken(self) -> bool:
        """判断是否应该触发熔断（硬错误总数达到上限）。"""
        return self._total_failures >= self.max_total_failures

    def get_circuit_break_hint(self) -> str:
        """生成熔断提示，告诉 LLM 应该停止重试。"""
        return (
            f"已达到最大失败次数 {self.max_total_failures}，"
            "请停止重试当前策略，改用其他方法或总结当前进度。"
        )

    def get_retry_hint(self, tool_name: str, args: Optional[dict] = None) -> str:
        """生成重试提示，告诉 LLM 该工具已经失败多次。"""
        count = self.get_failure_count(tool_name, args)
        error_kind = self.get_last_error_kind(tool_name, args) or "未知错误"
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
        self._fine_failures.clear()
        self._tool_failures.clear()
        self._fine_reasons.clear()
        self._failure_reasons.clear()
        self._total_failures = 0

    def to_dict(self) -> dict:
        """序列化为字典（用于持久化）。

        细粒度 key 从 tuple 转为 "tool_name\\x00args_key" 字符串以兼容 JSON。
        """
        fine = {f"{k[0]}\x00{k[1]}": v for k, v in self._fine_failures.items()}
        fine_r = {f"{k[0]}\x00{k[1]}": v for k, v in self._fine_reasons.items()}
        return {
            "fine_failures": fine,
            "tool_failures": self._tool_failures,
            "fine_reasons": fine_r,
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
        # 反序列化细粒度
        ledger._fine_failures = {}
        for compound_key, count in data.get("fine_failures", {}).items():
            parts = compound_key.split("\x00", 1)
            if len(parts) == 2:
                ledger._fine_failures[(parts[0], parts[1])] = count
        ledger._fine_reasons = {}
        for compound_key, reason in data.get("fine_reasons", {}).items():
            parts = compound_key.split("\x00", 1)
            if len(parts) == 2:
                ledger._fine_reasons[(parts[0], parts[1])] = reason
        return ledger
