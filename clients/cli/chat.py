"""cagent 交互式会话模式（类似 OpenCode）。

启动后 Agent 常驻，多轮问答共享同一 session 的 history 和 memory。
每轮自动从 trace.jsonl 重建历史上下文注入新 goal。

用法：
  python -m clients.cli chat              # 启动新会话
  python -m clients.cli chat --session-id abc123  # 续接指定会话
"""
import uuid
from typing import Optional


def cmd_chat(args) -> int:
    """交互式 REPL 会话。"""
    import sys

    from cagent.events import EventEmitter
    from cagent.utils import sanitize_text

    from .display import create_display_handler
    from .main import _build_agent

    emitter = EventEmitter()
    emitter.subscribe(create_display_handler())
    agent, cfg = _build_agent(args.config, emitter=emitter)

    sid = args.session_id or uuid.uuid4().hex
    resume = False  # 首轮不 resume，后续轮自动 resume

    print(f"cagent 交互式会话已启动")
    print(f"会话 ID: {sid}")
    print(f"输入 /exit 退出，/history 查看历史上下文，/sessions 列出所有会话")
    print()

    while True:
        try:
            # 兼容非 TTY 环境（管道/重定向）
            sys.stdout.write("cagent> ")
            sys.stdout.flush()
            goal = sys.stdin.readline()
            if not goal:
                # EOF
                break
            goal = goal.strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not goal:
            continue

        # 内置命令
        if goal in ("/exit", "/quit", "/q"):
            break
        if goal in ("/help", "/h"):
            _print_help()
            continue
        if goal == "/history":
            _show_history(agent, sid)
            continue
        if goal == "/sessions":
            _list_sessions(agent)
            continue

        # 执行 agent
        try:
            agent.run(goal, session_id=sid, resume=resume)
        except Exception as e:
            print(f"执行错误: {e}")

        # 首轮执行后，后续轮自动 resume 带历史上下文
        resume = True

    print("\n会话已结束")
    return 0


def _print_help():
    """打印交互命令帮助。"""
    print("可用命令：")
    print("  /exit, /quit, /q  退出会话")
    print("  /history           查看当前会话的历史上下文")
    print("  /sessions          列出所有会话")
    print("  /help, /h          显示此帮助")
    print()


def _show_history(agent, sid: str):
    """显示当前会话的历史上下文。"""
    from cagent.storage import SessionRecorder

    recorder = SessionRecorder(agent.storage, sid)
    history = recorder.load_history()
    if not history:
        print("（暂无历史上下文）")
        return
    for entry in history:
        desc = entry.get("description", "")
        output = entry.get("output", "")
        success = "成功" if entry.get("success") else "失败"
        print(f"  [{success}] {desc} → {output[:100]}")
        for obs in entry.get("observations", []):
            tool = obs.get("tool", "")
            result = obs.get("result", "")[:80]
            print(f"    · {tool} → {result}")
    print()


def _list_sessions(agent):
    """列出所有会话。"""
    metas = [k for k in agent.storage.list_keys("sessions/") if k.endswith("meta.json")]
    if not metas:
        print("（暂无会话）")
        return
    for k in metas:
        meta = agent.storage.read_json(k) or {}
        sid = k.split("/")[1]
        status = meta.get("status", "?")
        goal = meta.get("goal", "")[:60]
        print(f"  {sid}  [{status}]  {goal}")
    print()
