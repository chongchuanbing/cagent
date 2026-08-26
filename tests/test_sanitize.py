"""sanitize_text 回归测试：代理字符（surrogates not allowed）问题。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cagent.storage import LocalFileStorage
from cagent.utils import sanitize_text


def _make_surrogate(text: str) -> str:
    """模拟非 UTF-8 终端下的 argv/stdin：把 GBK 字节用 surrogateescape 解码。"""
    return text.encode("gbk").decode("utf-8", errors="surrogateescape")


def test_sanitize_recovers_gbk_text():
    dirty = _make_surrogate("帮我计算 12 * 13 的结果")
    assert sanitize_text(dirty) == "帮我计算 12 * 13 的结果"


def test_sanitize_keeps_clean_text():
    assert sanitize_text("正常文本 abc 123") == "正常文本 abc 123"
    assert sanitize_text("") == ""


def test_sanitize_survives_unrecoverable_bytes():
    # 无法还原的字节兜底为替换字符，不抛异常
    dirty = "ok" + "\udcff" * 3
    assert sanitize_text(dirty).startswith("ok")
    assert "\udcff" not in sanitize_text(dirty)


def test_storage_write_json_with_sanitized_goal(tmp_path=None):
    tmp = tmp_path or tempfile.mkdtemp()
    storage = LocalFileStorage(os.path.join(tmp, ".data"))
    dirty = _make_surrogate("帮我计算 12 * 13 的结果")
    # 清洗后写盘不再触发 surrogates not allowed
    storage.write_json("sessions/t1/meta.json", {"goal": sanitize_text(dirty)})
    assert storage.read_json("sessions/t1/meta.json")["goal"] == "帮我计算 12 * 13 的结果"
    if tmp_path is None:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_sanitize_recovers_gbk_text()
    test_sanitize_keeps_clean_text()
    test_sanitize_survives_unrecoverable_bytes()
    test_storage_write_json_with_sanitized_goal()
    print("all sanitize tests passed")
