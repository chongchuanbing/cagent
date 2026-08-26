"""统一日志。"""
import logging


def get_logger(name: str) -> logging.Logger:
    """获取分级 logger，便于区分 loop / react / tool 层级。"""
    logger = logging.getLogger(f"cagent.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(name)s] %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
