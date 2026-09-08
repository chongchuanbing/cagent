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
    "运行环境：\n{env}\n\n"
    "工作方式（ReAct）：\n"
    "1. 若需要更多信息或要执行动作，调用合适的工具。\n"
    "2. 工具返回后，基于观察继续推理。\n"
    "3. 当你已得出该步骤的最终结论时，直接输出结论文本，不要调用任何工具。\n\n"
    "行动纪律（先看→再想→后做）：\n"
    "- 【先看】文件路径、文件名、操作名等一切标识符，只能使用工具返回或指令中"
    "实际出现的内容；不确定时先列目录/查询确认，再行动。\n"
    "- 【禁猜】不要凭命名模式、常识或经验推测文件是否存在。指令中出现通配或"
    "模糊引用（如「读取 references/component-*.md 中对应文档」）时，必须先列出"
    "该目录的真实文件再选择，严禁直接拼造一个具体文件名去读。\n"
    "- 【自纠】工具失败时，仔细阅读返回中的错误原因与候选建议并据此修正下一步；"
    "禁止无视错误、换个猜测继续重试。\n\n"
    "读取文件协议（关键）：\n"
    "- 严禁使用 cat（cat 无法指定行范围，大文件会被静默截断 → 你会基于半截代码"
    "乱改；框架已默认拦截 cat 并要求改用下方工具）。\n"
    "- 优先用 shell.sed(file, start_line, end_line) 按行范围读取；\n"
    "- shell 通用通道里：先 `grep -n 'pattern' <file>` 定位目标行号，"
    "再 `sed -n 'A,Bp' <file>` 精确点读，或 `head -n N` / `tail -n N` / "
    "`awk 'NR>=A && NR<=B'`；\n"
    "- 框架对读取类命令自动走行号化视图：输出头部 `=== <file> | N lines | ... ===` "
    "告诉你总行数与当前区段，末尾给出续读指令（grep 定位 / sed 续读）或 "
    "`[End of file - N lines total]`；如未读到 `[End of file]` 说明还有内容，"
    "按尾部指令续读即可。\n"
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
    step: Step, tools: List[Any], template: Optional[str] = None, env_facts: Optional[str] = None,
    path_card: Optional[str] = None,
) -> str:
    """构造单 step 的 ReAct 系统提示，含步骤描述、工具目录、环境事实与执行规则。

    template 为 None 时使用内置 REACT_SYSTEM_TEMPLATE；若提供（如来自配置），
    则按 {step} / {tools} / {env} 占位符渲染（tools 渲染为目录文本）。

    env_facts 为环境事实字符串（如 "fd=可用, rg=不可用"），注入到 system prompt
    以告知 LLM 当前运行环境的工具可用性。
    path_card 为 PathSpace.path_card() 生成的路径速查卡，追加到环境段末尾，
    告知 LLM 可引用的逻辑 scheme（workspace://、skills://<name>/ 等），禁止猜绝对路径。
    """
    tools_desc = format_tool_catalog(tools)
    env_desc = env_facts or "未知（无探测信息）"
    if path_card:
        env_desc = f"{env_desc}\n\n{path_card}"
    base = template or REACT_SYSTEM_TEMPLATE
    try:
        return base.format(step=step.description, tools=tools_desc, env=env_desc)
    except (KeyError, IndexError):
        return base


def build_react_user_prompt(step: Step, history: List, goal: Optional[str] = None) -> str:
    """构造单 step 的用户提示，含历史上下文。

    history 条目为 AgentLoop 维护的 dict：
      {"step_id", "description", "success", "output"}
    只包含步骤的交付结论；ReAct 内部的所有中间过程（工具调用、用户反馈等）
    均不进会话上下文（完整轨迹保存在 trace.jsonl 用于审计/调试）。
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
        else:
            lines.append(f"- {h}")
    hist = "\n".join(lines) if lines else "（无）"
    goal_line = f"总目标：{goal}\n\n" if goal else ""
    return (
        f"{goal_line}请开始执行步骤：「{step.description}」。\n\n"
        f"历史上下文：\n{hist}"
    )
