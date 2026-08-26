"""存储模块：本地文件存储，默认根目录为项目下 `.data`。"""
from .base import StorageBackend
from .local import LocalFileStorage
from .session import SessionRecorder


def get_storage(data_dir: str = ".data") -> LocalFileStorage:
    """工厂：返回本地文件存储实例。"""
    return LocalFileStorage(data_dir)


__all__ = ["StorageBackend", "LocalFileStorage", "SessionRecorder", "get_storage"]
