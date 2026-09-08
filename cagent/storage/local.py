"""本地文件存储实现：以 .data 为根目录。"""
import os
import tempfile
from typing import List, Optional

from .base import StorageBackend


class LocalFileStorage(StorageBackend):
    """以本地文件系统为后端的存储，根目录默认项目下 `.data`。

    安全性：所有 key 都必须相对 root 且不得逃出 root（`..`、绝对路径、
    指向外部的 symlink 均拒绝）；注入 `path_space` 时再经 PathSpace.assert_safe
    统一校验，让框架内所有文件访问走同一个安全闸门。
    """

    def __init__(self, root: str = ".data", path_space=None):
        self.root = os.path.realpath(os.path.abspath(root))
        self.path_space = path_space
        os.makedirs(self.root, exist_ok=True)

    def _safe(self, key: str, mode: str = "read") -> str:
        """key → 绝对路径（含越界校验，不创建目录）。"""
        if not isinstance(key, str) or not key:
            raise ValueError("存储 key 必须是非空字符串")
        if os.path.isabs(key) or key.startswith(("/", "~")):
            raise PermissionError(f"存储 key 必须是相对路径: {key}")
        path = os.path.realpath(os.path.join(self.root, key))
        # 基础包含校验：realpath 后必须仍在 root 内（拦住 .. 与逃逸 symlink）
        if not (path == self.root or path.startswith(self.root + os.sep)):
            raise PermissionError(f"存储 key 越界: {key}")
        if self.path_space is not None:
            self.path_space.assert_safe(path, mode=mode)
        return path

    def _resolve(self, key: str) -> str:
        """写路径：校验 + 按需创建父目录。"""
        path = self._safe(key, mode="write")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return path

    def write_text(self, key: str, content: str) -> None:
        """原子写入：先写临时文件再 rename，防止并发写入时数据丢失。"""
        path = self._resolve(key)
        parent = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".tmp_", suffix=".write")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def read_text(self, key: str) -> Optional[str]:
        path = self._safe(key, mode="read")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def append_line(self, key: str, line: str) -> None:
        with open(self._resolve(key), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._safe(key, mode="read"))

    def list_keys(self, prefix: str = "") -> List[str]:
        base = self._safe(prefix, mode="read") if prefix else self.root
        if not os.path.isdir(base):
            return []
        keys: List[str] = []
        for dirpath, _, files in os.walk(base):
            for fn in files:
                full = os.path.join(dirpath, fn)
                keys.append(os.path.relpath(full, self.root))
        return sorted(keys)

    def delete(self, key: str) -> None:
        path = self._safe(key, mode="write")
        if os.path.isfile(path):
            os.remove(path)
