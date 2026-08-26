"""总结提示词模板。"""
from typing import List

from ..schema.plan import Plan


SUMMARIZE_SYSTEM_PROMPT = (
    "你是一个任务总结器。请根据目标与各步骤的执行结果，"
    "生成一份简洁、连贯的最终回答。\n"
    "要求：\n"
    "- 直接输出总结内容，不要调用工具。\n"
    "- 如果所有步骤成功，整合结果给出明确结论。\n"
    "- 如果有步骤失败，说明原因并给出可行的后续建议。\n"
    "- 用自然语言组织，不要输出 JSON 或列表格式。\n"
)

HISTORY_SUMMARIZE_SYSTEM_PROMPT = (
    "你是任务执行历史的压缩器。请把给定的历史步骤记录压缩为一段摘要，"
    "供后续步骤作为上下文使用。\n"
    "要求：\n"
    "- 保留关键结论、数据与用户已提供的反馈（最重要，不可丢失）。\n"
    "- 丢弃过程性细节与冗余描述，摘要长度不超过 300 字。\n"
    "- 直接输出摘要文本，不要调用工具。\n"
)


def build_summarize_prompt(plan: Plan) -> str:
    """构造总结用户提示：给出目标与各 step 的状态/输出。"""
    lines: List[str] = [f"目标：{plan.goal}", "", "各步骤执行情况："]
    for s in plan.steps:
        status = s.status.value
        out = s.result.output if s.result else ""
        lines.append(f"- [{status}] {s.description}: {out}")
    lines.append("")
    lines.append("请基于以上信息，给出最终总结。")
    return "\n".join(lines)
