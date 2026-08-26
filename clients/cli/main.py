"""cagent 命令行客户端。

子命令：
  run <goal>             运行 Agent（自动加载 config/agent.yaml + storage 落盘）
  config show            打印当前生效配置
  config set <key> <val> 修改 agent.yaml 的某项配置（保存即生效）
  sessions list          列出 .data 下的历史会话
  memory list            列出长期记忆（--status candidate/tenured 过滤）
  memory promote <id>    手动晋升某条候选记忆
  memory forget <id>     删除某条记忆
  memory tag <id> k=v..  修改某条记忆的标签

示例：
  python -m clients.cli run "帮我算 12 * 13"
  python -m clients.cli config set model.temperature 0.7
  python -m clients.cli config set max_steps 10
  python -m clients.cli memory list --status candidate
"""
import argparse
import os
from typing import List, Optional

from .chat import cmd_chat


def _coerce(value: str):
    """把命令行字符串尽量还原为 int / float / bool，否则保留 str。"""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _set_nested(d: dict, dotted_key: str, value) -> None:
    """支持 `model.temperature` 这类点分键写入。"""
    keys = dotted_key.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
        if not isinstance(cur, dict):
            raise ValueError(f"键路径冲突：{k} 不是对象")
    cur[keys[-1]] = value


def _build_agent(config_path: Optional[str] = None, emitter=None):
    """按配置构造 Agent。

    加载：
    - 内置直接执行工具（calculator/ask_user/remember）
    - tools 插件（filesystem/shell）→ tool_guide + run_tool
    - MCP 服务器（多服务器配置）→ 注册到操作树，走 tool_guide + run_tool（mcp 模式）
    - Skills（多目录 SKILL.md 标准）→ skill_guide + run_skill
    """
    import os as _os

    from cagent.config import ConfigProvider
    from cagent.core import Agent
    from cagent.storage import get_storage
    from cagent.tools import ToolRegistry, add
    from cagent.tools.builtin import FeedbackTool, RememberTool
    from cagent.plugins import (
        PluginManager, ToolGuideTool, RunToolTool, ShellExecutor,
        McpPluginLoader, McpClientManager,
        SkillsPluginLoader, SkillGuideTool, RunSkillTool,
    )

    cfg = ConfigProvider(config_path) if config_path else ConfigProvider()
    agent_config = cfg.get_config()
    tools_cfg = agent_config.tools

    # work_dir 是工具操作的沙箱根目录（用户当前工作目录），
    # data_dir 是 agent 自身数据存储目录（.data），两者不能混用
    work_dir = os.getcwd()

    # 将 PluginConfig 对象转为 PluginManager 需要的 plain dict
    plugins_config = {}
    for name, pc in tools_cfg.plugins.items():
        plugin_cfg = dict(pc.config)
        plugin_cfg.setdefault("work_dir", work_dir)
        plugins_config[name] = {"enabled": pc.enabled, "config": plugin_cfg}

    # 加载 tools 插件
    plugins_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))),
        "cagent", "plugins",
    )
    pm = PluginManager(plugins_dir, plugins_config)
    loaded = pm.load_all()

    # 加载 MCP 服务器（注册到同一操作树）
    mcp_loader = McpPluginLoader(agent_config.mcp)
    mcp_clients = mcp_loader.load_all(loaded["tree"])
    mcp_manager = McpClientManager()
    mcp_manager._clients = mcp_clients

    # Shell 执行器
    shell_config = dict(loaded.get("shell_config", {}))
    shell_config.setdefault("work_dir", work_dir)
    shell_executor = ShellExecutor(shell_config)

    # 通用披露+执行工具（tools + mcp 统一）
    guide = ToolGuideTool(loaded["tree"])
    runner = RunToolTool(
        loaded["tree"], loaded["executors"],
        shell_executor=shell_executor,
        mcp_manager=mcp_manager,
    )

    # 加载 Skills（多目录扫描 SKILL.md 标准）
    skills_loader = SkillsPluginLoader()
    skills = skills_loader.load_all(agent_config.skills)
    skill_guide = SkillGuideTool(skills)
    run_skill = RunSkillTool(skills)

    tools = ToolRegistry()
    tools.register(add)
    tools.register(guide)
    tools.register(runner)
    tools.register(skill_guide)
    tools.register(run_skill)
    if emitter is not None:
        tools.register(FeedbackTool(emitter=emitter))
    agent = Agent(tools=tools, config=cfg, storage=get_storage(cfg.data_dir), emitter=emitter)
    if agent.memory is not None:
        tools.register(RememberTool(agent.memory))
    return agent, cfg


def cmd_run(args: argparse.Namespace) -> int:
    from cagent.events import EventEmitter

    from .display import create_display_handler

    emitter = EventEmitter()
    emitter.subscribe(create_display_handler())
    agent, _ = _build_agent(args.config, emitter=emitter)
    agent.run(args.goal, session_id=args.session_id, resume=args.resume)
    # 最终答案已由 FINAL_ANSWER 事件渲染，无需重复 print
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    from cagent.config import ConfigProvider

    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    print(cfg.get_config().model_dump_json(indent=2))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    import yaml

    from cagent.config import ConfigProvider

    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    path = cfg.path
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    value = _coerce(args.value)
    _set_nested(data, args.key, value)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    print(f"已更新 {args.key} = {value!r}（下次运行自动生效）")
    return 0


def cmd_sessions_list(args: argparse.Namespace) -> int:
    from cagent.config import ConfigProvider
    from cagent.storage import get_storage

    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    storage = get_storage(cfg.data_dir)
    metas = [k for k in storage.list_keys("sessions/") if k.endswith("meta.json")]
    if not metas:
        print("（暂无会话）")
        return 0
    for k in metas:
        meta = storage.read_json(k) or {}
        sid = k.split("/")[1]
        print(f"{sid}  [{meta.get('status', '?')}]  {meta.get('goal', '')}")
    return 0


# ---------- memory 管理子命令 ----------

def _memory_store(args: argparse.Namespace):
    """按配置构造 LongTermMemory（读当前 namespace）。"""
    from cagent.config import ConfigProvider
    from cagent.memory import LongTermMemory
    from cagent.storage import get_storage

    data_dir = getattr(args, "data_dir", None)
    cfg = ConfigProvider(args.config, watch=False, data_dir=data_dir) if args.config else ConfigProvider(watch=False, data_dir=data_dir)
    mcfg = cfg.get_config().memory
    return LongTermMemory(get_storage(cfg.data_dir), namespace=mcfg.namespace)


def cmd_memory_list(args: argparse.Namespace) -> int:
    from cagent.memory import MemoryStatus

    store = _memory_store(args)
    status = MemoryStatus(args.status) if args.status else None
    records = store.list_all(status)
    if not records:
        print("（暂无记忆）")
        return 0
    for r in records:
        tags = " ".join(f"{k}={v}" for k, v in r.tags.items())
        pin = " [pinned]" if r.pinned else ""
        print(f"{r.id}  [{r.status.value}] age={r.age}{pin}  {tags}\n    {r.content}")
    return 0


def cmd_memory_promote(args: argparse.Namespace) -> int:
    from cagent.memory import MemoryStatus
    from datetime import datetime

    store = _memory_store(args)
    record = store.get(args.id)
    if record is None:
        print(f"未找到记忆 {args.id}")
        return 1
    if record.status != MemoryStatus.TENURED:
        record.pinned = True
        record.updated_at = datetime.now()
        store.migrate(record, MemoryStatus.TENURED)
    print(f"已晋升 {args.id} 为长期记忆")
    return 0


def cmd_memory_forget(args: argparse.Namespace) -> int:
    store = _memory_store(args)
    if store.remove(args.id):
        print(f"已删除记忆 {args.id}")
        return 0
    print(f"未找到记忆 {args.id}")
    return 1


def cmd_memory_tag(args: argparse.Namespace) -> int:
    store = _memory_store(args)
    record = store.get(args.id)
    if record is None:
        print(f"未找到记忆 {args.id}")
        return 1
    for kv in args.tags:
        if "=" not in kv:
            print(f"标签格式错误（应为 k=v）: {kv}")
            return 1
        k, v = kv.split("=", 1)
        record.tags[k.strip()] = v.strip()
    store.upsert(record)
    print(f"已更新标签: {' '.join(f'{k}={v}' for k, v in record.tags.items())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cagent", description="cagent 命令行客户端")
    parser.add_argument("--config", default=None, help="指定 agent.yaml 路径（默认 config/agent.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行 Agent")
    p_run.add_argument("goal", help="任务目标")
    p_run.add_argument("--session-id", default=None, help="指定会话 ID（用于回放/续跑）")
    p_run.add_argument("--resume", action="store_true", default=False, help="从指定会话的历史上下文继续执行")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互式会话模式")
    p_chat.add_argument("--session-id", default=None, help="续接指定会话 ID")
    p_chat.set_defaults(func=cmd_chat)

    p_cfg = sub.add_parser("config", help="查看/修改配置")
    cfg_sub = p_cfg.add_subparsers(dest="cfg_command", required=True)
    cfg_sub.add_parser("show", help="打印当前配置").set_defaults(func=cmd_config_show)
    p_set = cfg_sub.add_parser("set", help="修改某配置项（点分键，如 model.temperature）")
    p_set.add_argument("key", help="配置键，如 model.temperature / max_steps / prompts.react_system")
    p_set.add_argument("value", help="新值")
    p_set.set_defaults(func=cmd_config_set)

    p_sess = sub.add_parser("sessions", help="会话管理")
    sess_sub = p_sess.add_subparsers(dest="sess_command", required=True)
    sess_sub.add_parser("list", help="列出历史会话").set_defaults(func=cmd_sessions_list)

    p_mem = sub.add_parser("memory", help="长期记忆管理")
    p_mem.add_argument("--data-dir", default=None, help="指定数据目录（默认 .data）")
    mem_sub = p_mem.add_subparsers(dest="mem_command", required=True)
    p_list = mem_sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--status", choices=["candidate", "tenured"], default=None,
                        help="按分代过滤（默认全部）")
    p_list.set_defaults(func=cmd_memory_list)
    p_promote = mem_sub.add_parser("promote", help="手动晋升某条候选记忆")
    p_promote.add_argument("id", help="记忆 ID")
    p_promote.set_defaults(func=cmd_memory_promote)
    p_forget = mem_sub.add_parser("forget", help="删除某条记忆")
    p_forget.add_argument("id", help="记忆 ID")
    p_forget.set_defaults(func=cmd_memory_forget)
    p_tag = mem_sub.add_parser("tag", help="修改记忆标签（k=v 可多个）")
    p_tag.add_argument("id", help="记忆 ID")
    p_tag.add_argument("tags", nargs="+", help="标签键值对，如 主题=偏好")
    p_tag.set_defaults(func=cmd_memory_tag)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
