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
        import logging

        logger = logging.getLogger(__name__)
        obj, had_surrogate = _strip_surrogates(obj)
        if had_surrogate:
            # 输入边界未拦截住的代理字符（\udcXX）落到此处：静默替换为 U+FFFD 防崩，
            # 但必须报警，否则事后无法判断是输入编码损坏还是数据本身如此。
            logger.warning(
                "storage.write_json: 检测到代理字符（疑似输入编码异常），已替换为 U+FFFD 防止崩溃: %s",
                key,
            )
        self.write_text(key, json.dumps(obj, ensure_ascii=False, indent=2, default=str))

    def read_json(self, key: str) -> Optional[Any]:
        import json

        text = self.read_text(key)
        return json.loads(text) if text is not None else None


def _strip_surrogates(obj):
    """递归清理字符串中的代理字符，避免 JSON 落盘时 `surrogates not allowed` 崩溃。

    返回 (清洗后的对象, 是否发生过替换)。Python 标准 utf-8 编解码器对**任何**
    代理码点（含合法代理对）都会报 surrogates not allowed，因此这里必须主动处理：

    - 合法代理对（高+低 surrogate）→ 合并为对应的 astral 字符（emoji 等正常保留）；
    - 孤立代理字符（无法配对）→ 替换为 U+FFFD，保证落盘不崩。

    只处理 str 值；dict / list / tuple 递归。
    """
    found = False
    if isinstance(obj, str):
        out = []
        i = 0
        n = len(obj)
        while i < n:
            cp = ord(obj[i])
            if 0xD800 <= cp <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(obj[i + 1]) <= 0xDFFF:
                # 合法代理对 → 合并为 astral 字符
                hi, lo = cp, ord(obj[i + 1])
                astral = 0x10000 + (hi - 0xD800) * 0x400 + (lo - 0xDC00)
                out.append(chr(astral))
                i += 2
                continue
            if 0xD800 <= cp <= 0xDFFF:
                # 孤立代理字符 → 替换为 U+FFFD
                found = True
                out.append("\ufffd")
                i += 1
                continue
            out.append(obj[i])
            i += 1
        return "".join(out), found
    if isinstance(obj, dict):
        for k, v in obj.items():
            obj[k], f = _strip_surrogates(v)
            found = found or f
        return obj, found
    if isinstance(obj, (list, tuple)):
        cleaned = [_strip_surrogates(v) for v in obj]
        for nv, f in cleaned:
            found = found or f
        out = [nv for nv, _ in cleaned]
        return (out if isinstance(obj, list) else tuple(out)), found
    return obj, found
