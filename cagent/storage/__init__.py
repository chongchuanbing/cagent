"""存储模块：本地文件存储，默认根目录为项目下 `.data`。"""
from .base import StorageBackend
from .local import LocalFileStorage
from .session import SessionRecorder


def get_storage(data_dir: str = ".data", path_space=None) -> LocalFileStorage:
    """工厂：返回本地文件存储实例。

    注入 `path_space` 时，存储根固定取 `data://` 挂载点——PathSpace 是路径的
    唯一事实来源，避免 `data_dir` 与 `data://` 两处配置漂移导致写入位置不一致。
    """
    if path_space is not None:
        dm = path_space.mounts.get("data")
        if dm is not None:
            data_dir = str(dm.physical)
    return LocalFileStorage(data_dir, path_space=path_space)


__all__ = ["StorageBackend", "LocalFileStorage", "SessionRecorder", "get_storage"]
