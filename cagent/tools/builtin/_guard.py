"""文件/Shell 工具共享的安全基础设施。"""
import os
from typing import Tuple


class PathGuard:
    """路径沙箱：确保所有文件操作限制在 work_dir 子树内。

    - realpath 归一化后检查 work_dir 前缀，防止 ../ 越界
    - 禁止符号链接遍历越界（防止路径替换攻击）
    """

    def __init__(self, work_dir: str = "."):
        self.work_dir = os.path.realpath(work_dir)

    def safe_path(self, rel: str) -> str:
        """将相对路径转为绝对路径并检查是否在 work_dir 内。

        返回归一化后的绝对路径；越界时抛 PermissionError。
        """
        if os.path.isabs(rel):
            abs_path = os.path.normpath(os.path.realpath(rel))
        else:
            abs_path = os.path.normpath(os.path.realpath(os.path.join(self.work_dir, rel)))
        work_dir = os.path.normpath(self.work_dir)
        if abs_path != work_dir and not abs_path.startswith(work_dir + os.sep):
            raise PermissionError(f"路径越界: {rel} 不在 {self.work_dir} 内")
        return abs_path

    def safe_write_path(self, rel: str) -> str:
        """写入路径检查：与 safe_path 相同，但额外检查符号链接。"""
        path = self.safe_path(rel)
        if os.path.islink(path):
            real = os.path.normpath(os.path.realpath(path))
            work_dir = os.path.normpath(self.work_dir)
            if real != work_dir and not real.startswith(work_dir + os.sep):
                raise PermissionError(f"符号链接越界: {rel}")
        return path


class OutputTruncator:
    """按字符数和行数双重截断输出。"""

    def __init__(self, max_chars: int = 2000, max_lines: int = 50):
        self.max_chars = max_chars
        self.max_lines = max_lines

    def truncate(self, text: str) -> Tuple[str, bool]:
        """截断文本，返回 (截断后的文本, 是否被截断)。"""
        if not text:
            return "", False
        truncated = False
        original_lines = text.split("\n")
        original_len = len(text)
        lines = original_lines
        if len(lines) > self.max_lines:
            lines = lines[: self.max_lines]
            truncated = True
        result = "\n".join(lines)
        if len(result) > self.max_chars:
            result = result[: self.max_chars]
            truncated = True
        if truncated:
            result += f"\n...（已截断，原文共 {len(original_lines)} 行/{original_len} 字符）"
        return result, truncated
