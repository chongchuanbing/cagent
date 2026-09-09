"""cagent 交互式会话模式（类似 OpenCode）。

启动后 Agent 常驻，多轮问答共享同一 session 的 history 和 memory。
每轮自动从 trace.jsonl 重建历史上下文注入新 goal。

用法：
  python -m clients.cli chat              # 启动新会话
  python -m clients.cli chat --session-id abc123  # 续接指定会话
"""
import traceback
import uuid
from typing import Optional

from cagent.utils.logging import get_logger

logger = get_logger(__name__)


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
    # 显式指定 --session-id 视为续接既有会话：首轮即加载其历史上下文；
    # 若该 session 无 trace（新 ID），load_history 返回空，等价于全新会话
    resume = args.session_id is not None

    print(f"cagent 交互式会话已启动")
    print(f"会话 ID: {sid}")
    space_dir = getattr(args, "space", None)
    if space_dir:
        print(f"项目空间: {space_dir}（修改项目文件用 workspace:// 前缀；临时文件落会话目录）")
    if resume:
        from cagent.storage import SessionRecorder

        prior_recorder = SessionRecorder(agent.storage, sid)
        if prior_recorder.is_interrupted():
            # 中断恢复：显示中断状态，用户输入"继续"即可恢复
            meta = prior_recorder.load_meta()
            print(f"检测到上次会话中断，状态：interrupted")
            print(f"输入「继续」恢复执行，输入其他内容将作为新任务开始")
        else:
            prior = prior_recorder.load_history()
            if prior:
                last = prior[-1]
                print(f"已加载该会话历史：目标「{last['description'][:50]}」→ {last['output'][:50]}")
            else:
                print(f"（会话 {sid} 无历史记录，将作为新会话开始）")
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

        # 检查是否触发中断恢复
        if resume and goal.lower() in ["继续", "continue", "恢复", "resume"]:
            from cagent.storage import SessionRecorder
            recorder = SessionRecorder(agent.storage, sid)
            if recorder.is_interrupted():
                print("检测到中断会话，开始恢复执行...")
                try:
                    # 使用原目标继续执行
                    meta = recorder.load_meta()
                    original_goal = meta.get("goal", "")
                    agent.run_interrupted(session_id=sid, space_dir=space_dir)
                except Exception as e:
                    logger.exception(f"恢复执行失败: {type(e).__name__}: {e}")
                    print(f"恢复执行失败: {type(e).__name__}: {e}")
                continue

        # 执行 agent
        try:
            agent.run(goal, session_id=sid, resume=resume, space_dir=space_dir)
        except Exception as e:
            logger.exception(f"执行错误: {type(e).__name__}: {e}")
            print(f"执行错误: {type(e).__name__}: {e}")
            if logger.level == 10:  # DEBUG
                traceback.print_exc()

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
