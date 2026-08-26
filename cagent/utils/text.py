"""文本清洗与分词工具。"""
from __future__ import annotations

import re


def tokenize(text: str) -> set:
    """中英文混合分词：英文按词，中文按 bigram（CJK 无空格，单字区分度不足）。

    供长期记忆召回 / 工具动态选择等关键词匹配场景复用。
    """
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]+", text))
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.add(seg)
        else:
            tokens.update(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def sanitize_text(text: str) -> str:
    """清洗 surrogateescape 产生的孤立代理字符。

    终端在非 UTF-8 环境下粘贴内容（如 GBK）时，sys.argv / stdin /
    os.environ 会把无法解码的字节变成 \\udcXX 代理字符；这类字符串一旦
    写文件或 JSON 序列化就会报：

        UnicodeEncodeError: 'utf-8' codec can't encode character
        '\\udce5': surrogates not allowed

    处理策略：先还原为原始字节，依次尝试 utf-8 / gb18030 解码，
    均失败则用替换字符兜底，保证不再抛异常。
    """
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text  # 本来就是合法 UTF-8 文本，直接返回
    except UnicodeEncodeError:
        pass
    raw = text.encode("utf-8", "surrogateescape")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
