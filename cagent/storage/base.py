"""存储抽象层：解耦底层存储介质。"""
from abc import ABC, abstractmethod
from typing import Any, List, Optional


class StorageBackend(ABC):
    """统一存储接口，key 采用相对路径风格（如 "sessions/<id>/plan.json"）。"""

    @abstractmethod
    def write_text(self, key: str, content: str) -> None:
        """写入文本（覆盖）。"""
        raise NotImplementedError

    @abstractmethod
    def read_text(self, key: str) -> Optional[str]:
        """读取文本，不存在返回 None。"""
        raise NotImplementedError

    @abstractmethod
    def append_line(self, key: str, line: str) -> None:
        """追加一行（用于 jsonl 轨迹 / 消息流）。"""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_keys(self, prefix: str = "") -> List[str]:
        """列出匹配前缀的 key。"""
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        raise NotImplementedError

    # 便捷方法：JSON 读写
    def write_json(self, key: str, obj: Any) -> None:
        import json

        self.write_text(key, json.dumps(obj, ensure_ascii=False, indent=2, default=str))

    def read_json(self, key: str) -> Optional[Any]:
        import json

        text = self.read_text(key)
        return json.loads(text) if text is not None else None
