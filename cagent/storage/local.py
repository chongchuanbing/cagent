"""本地文件存储实现：以 .data 为根目录。"""
import os
import tempfile
from typing import List, Optional

from .base import StorageBackend


class LocalFileStorage(StorageBackend):
    """以本地文件系统为后端的存储，根目录默认项目下 `.data`。"""

    def __init__(self, root: str = ".data"):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, key: str) -> str:
        path = os.path.join(self.root, key)
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
        path = os.path.join(self.root, key)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def append_line(self, key: str, line: str) -> None:
        with open(self._resolve(key), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def exists(self, key: str) -> bool:
        return os.path.isfile(os.path.join(self.root, key))

    def list_keys(self, prefix: str = "") -> List[str]:
        base = os.path.join(self.root, prefix) if prefix else self.root
        if not os.path.isdir(base):
            return []
        keys: List[str] = []
        for dirpath, _, files in os.walk(base):
            for fn in files:
                full = os.path.join(dirpath, fn)
                keys.append(os.path.relpath(full, self.root))
        return sorted(keys)

    def delete(self, key: str) -> None:
        path = os.path.join(self.root, key)
        if os.path.isfile(path):
            os.remove(path)
