"""ReAct 单步提示词模板。"""
from typing import TYPE_CHECKING, Any, List, Optional

from ..schema.plan import Step

if TYPE_CHECKING:  # 仅类型标注用，避免 config → prompts → tools → memory 的循环导入
    from ..tools.base import Tool

# 工具目录中单条描述的截断长度（完整 schema 走 function calling 的 tools 参数，
# system prompt 只保留目录，避免双份注入导致 token 膨胀）
TOOL_DESC_MAX_CHARS = 120

# 可作为配置模板：支持 {step} 与 {tools} 占位符
REACT_SYSTEM_TEMPLATE = (
    "你正在执行如下计划步骤：\n步骤：{step}\n\n"
    "可用工具：\n{tools}\n\n"
    "工作方式（ReAct）：\n"
    "1. 若需要更多信息或要执行动作，调用合适的工具。\n"
    "2. 工具返回后，基于观察继续推理。\n"
    "3. 当你已得出该步骤的最终结论时，直接输出结论文本，不要调用任何工具。\n"
)

# 未收敛兜底提示：迭代耗尽时要求模型基于已有轨迹给出阶段性结论
REACT_UNCONVERGED_PROMPT = (
    "你已达到本步骤允许的最大推理—行动轮数，但尚未给出最终结论。\n"
    "请基于以上全部思考过程与工具观察，立即给出该步骤的阶段性总结结论。\n"
    "直接输出结论文本，不要再调用任何工具。"
)


def format_tool_catalog(tools: List[Any]) -> str:
    """把工具列表渲染为紧凑目录：[group] name: 描述（截断）。

    只列名称与一句话说明，完整参数 schema 交给 function calling 声明，
    避免 system prompt 与 tools 参数双份冗余。
    """
    lines = []
    for t in tools:
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > TOOL_DESC_MAX_CHARS:
            desc = desc[:TOOL_DESC_MAX_CHARS] + "…"
        prefix = f"[{t.group}] " if t.group != "default" else ""
        lines.append(f"- {prefix}{t.name}: {desc}")
    return "\n".join(lines)


def build_react_system_prompt(
    step: Step, tools: List[Any], template: Optional[str] = None
) -> str:
    """构造单 step 的 ReAct 系统提示，含步骤描述、工具目录与执行规则。

    template 为 None 时使用内置 REACT_SYSTEM_TEMPLATE；若提供（如来自配置），
    则按 {step} / {tools} 占位符渲染（tools 渲染为目录文本）。
    """
    tools_desc = format_tool_catalog(tools)
    base = template or REACT_SYSTEM_TEMPLATE
    try:
        return base.format(step=step.description, tools=tools_desc)
    except (KeyError, IndexError):
        return base


def build_react_user_prompt(step: Step, history: List, goal: Optional[str] = None) -> str:
    """构造单 step 的用户提示，含历史上下文。

    history 条目为 AgentLoop 维护的 dict：
      {"step_id", "description", "success", "output", "observations":
        [{"tool", "args", "result"}]}，其中 observations 完整保留了
    工具问答记录（含 ask_user 收集到的用户反馈）；
    首条可为 {"summary": str} —— 窗口外步骤的滚动摘要；
    首条可为 {"memory": [str]} —— 长期记忆召回（跨会话已确认事实）。
    """
    lines = []
    for h in history:
        if isinstance(h, dict):
            if "memory" in h:
                lines.append("- 【长期记忆】来自过往会话的已确认事实，请直接采信：")
                for m in h["memory"]:
                    lines.append(f"    · {m}")
                continue
            if "summary" in h:
                lines.append(f"- 【更早步骤摘要】{h['summary']}")
                continue
            mark = "成功" if h.get("success", True) else "失败"
            out = h.get("output") or "（无输出）"
            lines.append(
                f"- 步骤[{h.get('step_id', '?')}]（{mark}）{h.get('description', '')} → 结论：{out}"
            )
            for ob in h.get("observations") or []:
                lines.append(
                    f"    · 工具 {ob.get('tool')} 参数 {ob.get('args')} → 返回：{ob.get('result')}"
                )
        else:
            lines.append(f"- {h}")
    hist = "\n".join(lines) if lines else "（无）"
    goal_line = f"总目标：{goal}\n\n" if goal else ""
    return (
        f"{goal_line}请开始执行步骤：「{step.description}」。\n\n"
        f"历史上下文（含用户已提供的反馈与工具结果，请勿重复询问已有信息）：\n{hist}"
    )
