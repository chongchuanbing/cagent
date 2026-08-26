"""长期记忆提取与打标的提示词模板。

提取在每次 run 结束时执行一次（相当于一次 Minor GC）：
从本次 run 的素材（ask_user 问答、各 step 结论、最终答案）中提取值得
跨会话保留的事实，并按配置的标签模式（implicit/explicit/hybrid/off）打标。
提示词可用 `prompts.memory_extract` 覆盖（支持 {tags_instruction} 占位符）。
"""
from typing import List, Optional

MEMORY_EXTRACT_SYSTEM_PROMPT = (
    "你是记忆提取器。从一次任务执行的素材中提取值得跨会话长期保留的事实。\n"
    "只提取稳定、可复用的事实（用户偏好、已确认的结论、重要背景），\n"
    "忽略一次性的过程细节（中间计算、临时尝试）。\n"
    "若用户在对话中明确要求记住某事（如「记住我喜欢…」），将该条标记 pinned=true。\n"
    "{tags_instruction}\n"
    "只输出 JSON 数组，不要输出其他内容，格式：\n"
    '[{{"content": "事实内容", "tags": {{"分类": "取值"}}, "pinned": false}}]\n'
    "没有值得提取的内容时输出 []。"
)


def build_tags_instruction(mode: str, schema: dict, implicit_max_tags: int) -> str:
    """按标签模式生成打标指令片段。"""
    if mode == "off":
        return "本任务不需要打标签，tags 一律输出空对象 {}。"
    parts: List[str] = []
    if mode in ("explicit", "hybrid") and schema:
        lines = ["为每条记忆打标签，必须从以下固定分类中选择（取值只能用给定枚举）："]
        for cat, conf in schema.items():
            values = ", ".join(conf.get("values", []))
            req = "必填" if conf.get("required") else "可选"
            lines.append(f"- {cat}（{req}）取值: {values}")
        parts.append("\n".join(lines))
    if mode in ("implicit", "hybrid"):
        if mode == "hybrid":
            parts.append("除固定分类外，可再自由补充少量额外标签。")
        else:
            parts.append(
                f"为每条记忆自由生成不超过 {implicit_max_tags} 个 key:value 标签，"
                "key 为分类（如 主题/偏好/领域），value 为具体取值。"
            )
    return "\n".join(parts)


def build_memory_extract_user_prompt(goal: str, history: List[dict], answer: str) -> str:
    """构造提取的用户提示：总目标 + 各步骤结论与工具问答 + 最终答案。"""
    lines = [f"任务目标：{goal}", "", "执行过程素材："]
    for h in history:
        if not isinstance(h, dict):
            lines.append(f"- {h}")
            continue
        if "summary" in h:
            lines.append(f"- 【更早步骤摘要】{h['summary']}")
            continue
        mark = "成功" if h.get("success", True) else "失败"
        lines.append(f"- 步骤[{h.get('step_id', '?')}]（{mark}）{h.get('description', '')} → {h.get('output', '')}")
        for ob in h.get("observations") or []:
            lines.append(f"    · 工具 {ob.get('tool')} 参数 {ob.get('args')} → 返回：{ob.get('result')}")
    lines.append("")
    lines.append(f"最终答案：{answer}")
    return "\n".join(lines)
