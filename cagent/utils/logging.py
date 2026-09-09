"""统一日志 + 会话级文件日志。

两级输出：
- 控制台（stderr）：INFO 级别，保持原有终端输出简洁
- 文件（sessions/<sid>/agent.log）：DEBUG 级别，含完整堆栈

会话生命周期由 SessionLogger 管理：
- setup_session_logger(session_id, log_dir) 开始写文件
- teardown_session_logger() 关闭文件 handler
- get_logger(name) 获取分级 logger
"""
import logging
import os
from typing import Optional

# 会话级文件 handler 的引用（同一时刻只有一个活跃会话）
_session_file_handler: Optional[logging.FileHandler] = None
_session_log_path: Optional[str] = None

# 根 logger 名称
_ROOT_LOGGER_NAME = "cagent"


def get_logger(name: str) -> logging.Logger:
    """获取分级 logger（loop / react / tool 等）。

    每个 logger 只初始化一次（通过 handler 数量判断），
    handler 统一挂到根 logger `cagent` 上，避免重复输出。
    """
    root = logging.getLogger(_ROOT_LOGGER_NAME)
    # 根 logger 默认初始化一个 stderr handler（INFO 级别）
    if not root.handlers:
        _setup_root_console_handler(root)
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")


def _setup_root_console_handler(root: logging.Logger) -> None:
    """控制台 handler：INFO 级别到 stderr，简洁格式。"""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(name)s] %(levelname)s %(message)s")
    )
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)  # 根 logger 接受 DEBUG，由 handler 过滤


def setup_session_logger(session_id: str, log_dir: str) -> str:
    """为当前会话启用文件日志。

    Args:
        session_id: 会话 ID
        log_dir: 会话目录（如 .data/sessions/<sid>）

    Returns:
        日志文件绝对路径

    Side effect:
        在根 cagent logger 上追加一个 FileHandler（DEBUG 级别），
        并把之前的会话文件 handler 关闭（避免跨会话串）。
    """
    global _session_file_handler, _session_log_path

    # 清理上一次
    teardown_session_logger()

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "agent.log")
    _session_log_path = log_path

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    # 确保根 handler 就绪
    if not root.handlers:
        _setup_root_console_handler(root)

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    _session_file_handler = handler

    logger = get_logger("session")
    logger.info(f"=== 会话日志开始 session_id={session_id} log_path={log_path} ===")
    return log_path


def teardown_session_logger() -> None:
    """关闭并移除当前会话的文件 handler。"""
    global _session_file_handler, _session_log_path
    if _session_file_handler is None:
        return
    root = logging.getLogger(_ROOT_LOGGER_NAME)
    try:
        _session_file_handler.flush()
        _session_file_handler.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        root.removeHandler(_session_file_handler)
    except Exception:  # noqa: BLE001
        pass
    _session_file_handler = None
    _session_log_path = None


def current_log_path() -> Optional[str]:
    """返回当前活跃会话的日志路径（无会话则 None）。"""
    return _session_log_path
