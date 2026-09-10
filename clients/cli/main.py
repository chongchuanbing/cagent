"""cagent 命令行客户端。

子命令：
  run <goal>             运行 Agent（自动加载 config/agent.yaml + config/models.json + storage 落盘）
  run --model <id>       指定本次运行使用的模型（覆盖默认）
  models list            列出已配置模型（id/name/vendor/能力开关/默认）
  config show            打印当前生效配置
  config set <key> <val> 修改 agent.yaml 的某项配置（保存即生效）
  sessions list          列出 .data 下的历史会话
  memory list            列出长期记忆（--status candidate/tenured 过滤）
  memory promote <id>    手动晋升某条候选记忆
  memory forget <id>     删除某条记忆
  memory tag <id> k=v..  修改某条记忆的标签

示例：
  python -m clients.cli run "帮我算 12 * 13"
  python -m clients.cli run "用强模型重做" --model qwen3.7-plus
  python -m clients.cli models list
  python -m clients.cli config set max_steps 10
  python -m clients.cli memory list --status candidate
"""
import argparse
import logging
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
    from cagent.tools import ToolRegistry
    from cagent.tools.builtin import FeedbackTool, RememberTool
    from cagent.plugins import (
        PluginManager, ToolGuideTool, RunToolTool, ShellExecutor,
        McpPluginLoader, McpClientManager,
        SkillsPluginLoader, SkillGuideTool, RunSkillTool, ReadSkillFileTool,
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

    # Shell 执行器（注入 PathSpace 收编命令体路径沙箱）
    shell_config = dict(loaded.get("shell_config", {}))
    shell_config.setdefault("work_dir", work_dir)
    # 读取语义阈值（与 AgentConfig.read_* 对齐）
    shell_config["read_max_lines"] = agent_config.read_max_lines
    shell_config["read_line_max_chars"] = agent_config.read_line_max_chars
    shell_config["read_max_chars"] = agent_config.read_max_chars
    shell_config["block_cat"] = agent_config.block_cat
    # PathSpace 唯一构造入口：由配置 paths.* 驱动，端上只覆盖 plugins/config 目录
    path_space = cfg.build_path_space(
        work_dir, plugins_dir=plugins_dir,
        config_dir=_os.path.dirname(cfg.path) or None,
    )
    # 系统级沙箱后端（bwrap → seatbelt → user → local 降级链）
    from cagent.runtime.sandbox import SandboxSettings, select_backend
    sb = agent_config.sandbox
    sandbox_settings = SandboxSettings(
        mode=sb.mode,
        network_deny=(sb.network == "deny"),
        extra_readable=tuple(sb.extra_readable),
        user=sb.user,
    )
    shell_backend = select_backend(
        sandbox_settings, logger=logging.getLogger("cagent.sandbox"),
    )
    shell_executor = ShellExecutor(shell_config, path_space=path_space, backend=shell_backend)

    # 环境探测（用于工具可用性检查和回退提示）
    from cagent.plugins.capability import CapabilityProbe
    import os as _os
    capability = CapabilityProbe(_os.path.join(cfg.data_dir, "env.json"))

    # 通用披露+执行工具（tools + mcp 统一）
    guide = ToolGuideTool(loaded["tree"], capability=capability)
    runner = RunToolTool(
        loaded["tree"], loaded["executors"],
        shell_executor=shell_executor,
        mcp_manager=mcp_manager,
        emitter=emitter,
    )

    # 加载 Skills（多目录扫描 SKILL.md 标准）
    skills_loader = SkillsPluginLoader()
    skills = skills_loader.load_all(agent_config.skills)
    skill_guide = SkillGuideTool(skills)
    run_skill = RunSkillTool(skills)
    read_skill_file = ReadSkillFileTool(skills, path_space=path_space)

    tools = ToolRegistry()
    tools.register(guide)
    tools.register(runner)
    tools.register(skill_guide)
    tools.register(run_skill)
    tools.register(read_skill_file)
    if emitter is not None:
        tools.register(FeedbackTool(emitter=emitter))
    agent = Agent(
        tools=tools, config=cfg, storage=get_storage(cfg.data_dir, path_space=path_space),
        emitter=emitter, path_space=path_space,
    )
    if agent.memory is not None:
        tools.register(RememberTool(agent.memory))
    return agent, cfg


def cmd_run(args: argparse.Namespace) -> int:
    from cagent.events import EventEmitter

    from .display import create_display_handler

    emitter = EventEmitter()
    emitter.subscribe(create_display_handler())
    agent, _ = _build_agent(args.config, emitter=emitter)
    agent.run(args.goal, session_id=args.session_id, resume=args.resume,
              space_dir=getattr(args, "space", None), model=getattr(args, "model", None))
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
    path_space = cfg.build_path_space()
    storage = get_storage(cfg.data_dir, path_space=path_space)
    metas = [k for k in storage.list_keys("sessions/") if k.endswith("meta.json")]
    if not metas:
        print("（暂无会话）")
        return 0
    for k in metas:
        meta = storage.read_json(k) or {}
        sid = k.split("/")[1]
        status = meta.get('status', '?')
        goal = meta.get('goal', '')
        # 显示步骤统计
        plan = storage.read_json(f"sessions/{sid}/plan.json") or {}
        steps = plan.get('steps', [])
        if steps:
            done = sum(1 for s in steps if s.get('status') == 'done')
            failed = sum(1 for s in steps if s.get('status') == 'failed')
            pending = sum(1 for s in steps if s.get('status') == 'pending')
            stats = f" ({done}✓ {failed}✗ {pending}⏳)" if (done or failed or pending) else ""
            status_display = status
            if status == 'running' and done + failed < len(steps):
                status_display = f"{status} (中断)"
            print(f"{sid}  [{status_display}]{stats}  {goal}")
        else:
            print(f"{sid}  [{status}]  {goal}")
    return 0


def cmd_sessions_show(args: argparse.Namespace) -> int:
    """显示单个会话的详细信息，包括每个步骤的执行状态、依赖关系和结果摘要。"""
    from cagent.config import ConfigProvider
    from cagent.storage import get_storage

    sid = args.session_id
    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    path_space = cfg.build_path_space()
    storage = get_storage(cfg.data_dir, path_space=path_space)

    # 读取元信息
    meta = storage.read_json(f"sessions/{sid}/meta.json") or {}
    if not meta:
        print(f"错误: 会话 {sid} 不存在")
        return 1

    goal = meta.get('goal', '')
    status = meta.get('status', 'unknown')
    created = meta.get('created_at', '')
    finished = meta.get('finished_at', '')

    # 读取计划
    plan = storage.read_json(f"sessions/{sid}/plan.json") or {}
    steps = plan.get('steps', [])

    # 分析步骤状态
    done_steps = [s for s in steps if s.get('status') == 'done']
    failed_steps = [s for s in steps if s.get('status') == 'failed']
    pending_steps = [s for s in steps if s.get('status') == 'pending']

    # 输出头部信息
    print("=" * 70)
    print(f"会话 ID:  {sid}")
    print(f"目标:     {goal}")
    print(f"状态:     {status}")
    print(f"创建时间: {created}")
    if finished:
        print(f"结束时间: {finished}")
    print(f"步骤统计: {len(done_steps)} 完成 / {len(failed_steps)} 失败 / {len(pending_steps)} 待执行 / {len(steps)} 总计")
    print("=" * 70)

    if not steps:
        print("\n（无步骤信息）")
        return 0

    # 输出每个步骤的详细信息
    print("\n步骤详情:")
    for i, step in enumerate(steps, 1):
        step_id = step.get('id', f'step_{i}')
        step_status = step.get('status', 'unknown')
        description = step.get('description', '')
        depends_on = step.get('depends_on', [])

        # 状态图标
        status_icons = {
            'done': '✓',
            'failed': '✗',
            'pending': '⏳',
            'running': '🔄',
            'skipped': '⏭️'
        }
        icon = status_icons.get(step_status, '?')

        # 依赖关系
        dep_str = f" (依赖: {', '.join(depends_on)})" if depends_on else ""

        print(f"\n{i}. {icon} [{step_id}] {description}{dep_str}")
        print(f"   状态: {step_status}")

        # 结果信息
        result = step.get('result')
        if result:
            success = result.get('success')
            output = result.get('output', '')
            error = result.get('error', '')

            if success:
                # 截取输出摘要
                output_preview = output[:200] + ('...' if len(output) > 200 else '')
                print(f"   结果: 成功")
                if output_preview:
                    print(f"   输出: {output_preview}")
            else:
                print(f"   结果: 失败")
                if error:
                    print(f"   错误: {error}")
                if output:
                    output_preview = output[:150] + ('...' if len(output) > 150 else '')
                    print(f"   输出: {output_preview}")
        elif step_status == 'pending':
            # 检查为什么是 pending
            if depends_on:
                done_ids = {s.get('id') for s in steps if s.get('status') == 'done'}
                missing_deps = [d for d in depends_on if d not in done_ids]
                if missing_deps:
                    print(f"   ⚠️  未执行: 依赖步骤未完成 ({', '.join(missing_deps)})")
                elif status == 'running':
                    print(f"   ⚠️  未执行: 会话中断")
                else:
                    print(f"   ⚠️  未执行")
            else:
                if status == 'running':
                    print(f"   ⚠️  未执行: 会话中断")
                else:
                    print(f"   ⚠️  未执行")

    # 输出依赖图（如果有依赖关系）
    has_deps = any(step.get('depends_on') for step in steps)
    if has_deps:
        print("\n" + "=" * 70)
        print("依赖关系图:")
        for step in steps:
            step_id = step.get('id')
            depends_on = step.get('depends_on', [])
            if depends_on:
                for dep in depends_on:
                    print(f"  {dep} → {step_id}")

    print("=" * 70)
    return 0


def cmd_sessions_stats(args: argparse.Namespace) -> int:
    """显示会话执行度量报告（工具调用次数、耗时等）。"""
    from cagent.config import ConfigProvider
    from cagent.metrics import MetricsService, JsonlMetricsBackend
    from cagent.storage import get_storage

    # 支持两种格式：位置参数 或 key=value 格式
    sid = args.session_id
    if "=" in sid:
        # 解析 key=value 格式
        key, value = sid.split("=", 1)
        if key == "session_id":
            sid = value
        else:
            print(f"错误: 不支持的参数格式 '{sid}'，请使用 'session_id=<id>' 或直接提供 ID")
            return 1
    
    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    
    # 直接使用 data_dir 构造存储后端
    storage = get_storage(cfg.data_dir)
    
    # 获取存储根目录
    storage_root = getattr(storage, "root", None)
    if not storage_root:
        print(f"错误: 无法获取存储根目录")
        return 1
    
    # 构造度量服务
    backend = JsonlMetricsBackend(storage_root)
    service = MetricsService(backend)
    
    # 生成并打印报告
    report = service.format_report(sid)
    print(report)
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
    path_space = cfg.build_path_space()
    return LongTermMemory(get_storage(cfg.data_dir, path_space=path_space), namespace=mcfg.namespace)


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


def cmd_models_list(args: argparse.Namespace) -> int:
    """列出 models.json 中已配置的模型。"""
    from cagent.config import ConfigProvider

    cfg = ConfigProvider(args.config, watch=False) if args.config else ConfigProvider(watch=False)
    mf = cfg.get_models_file()
    if not mf.models:
        print("（未配置任何模型，请在 config/models.json 中添加）")
        return 0
    default = cfg.get_default_model()
    print(f"{'ID':<22} {'NAME':<26} {'VENDOR':<10} TOOL VISION REASON DEFAULT")
    for m in mf.models:
        flag = "★" if m.model == default else " "
        print(
            f"{m.model:<22} {(m.name or ''):<26} {m.vendor:<10} "
            f"{'Y' if m.tool_calling else '-'} "
            f"{'Y' if m.vision else '-'} "
            f"{'Y' if m.reasoning.enabled else '-'}      {flag}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cagent", description="cagent 命令行客户端")
    parser.add_argument("--config", default=None, help="指定 agent.yaml 路径（默认 config/agent.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行 Agent")
    p_run.add_argument("goal", help="任务目标")
    p_run.add_argument("--session-id", default=None, help="指定会话 ID（用于回放/续跑）")
    p_run.add_argument("--resume", action="store_true", default=False, help="从指定会话的历史上下文继续执行")
    p_run.add_argument("--space", default=None, help="项目空间目录（如代码仓库根）；未指定时所有生成文件落会话目录")
    p_run.add_argument("--model", default=None, help="指定运行模型 id（覆盖默认）；见 `cagent models list`")
    p_run.set_defaults(func=cmd_run)

    p_chat = sub.add_parser("chat", help="交互式会话模式")
    p_chat.add_argument("--session-id", default=None, help="续接指定会话 ID")
    p_chat.add_argument("--space", default=None, help="项目空间目录（如代码仓库根）；未指定时所有生成文件落会话目录")
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
    p_sess_show = sess_sub.add_parser("show", help="显示会话详情（步骤状态、依赖关系、执行结果）")
    p_sess_show.add_argument("session_id", help="要查看的会话 ID")
    p_sess_show.set_defaults(func=cmd_sessions_show)
    p_sess_stats = sess_sub.add_parser("stats", help="显示会话执行度量报告（工具调用次数、耗时等）")
    p_sess_stats.add_argument("session_id", help="要查看度量的会话 ID")
    p_sess_stats.set_defaults(func=cmd_sessions_stats)

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

    p_models = sub.add_parser("models", help="模型管理（models.json）")
    models_sub = p_models.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("list", help="列出已配置模型（id/name/vendor/能力/默认）").set_defaults(
        func=cmd_models_list
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
