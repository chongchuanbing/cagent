"""工具注册与检索：分组命名空间 + 动态 Top-K 选择。"""
import logging
from typing import Dict, List, Optional

from ..utils import tokenize
from .base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """保存已注册工具，支持按分组裁剪与按查询动态选择。"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool, group: Optional[str] = None) -> None:
        """注册工具；group 参数可覆盖工具自身声明的分组。"""
        if not tool.name:
            raise ValueError("Tool must have a non-empty name")
        if group is not None:
            tool.group = group
        if tool.name in self._tools:
            logger.warning("工具 '%s' 已存在，将被覆盖", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self, groups: Optional[List[str]] = None) -> List[Tool]:
        """列出工具；groups 非 None 时只保留启用组（default 组始终启用）。"""
        if groups is None:
            return list(self._tools.values())
        enabled = set(groups) | {"default"}
        return [t for t in self._tools.values() if t.group in enabled]

    def groups(self) -> Dict[str, List[str]]:
        """分组视图：group -> [tool names]。"""
        out: Dict[str, List[str]] = {}
        for t in self._tools.values():
            out.setdefault(t.group, []).append(t.name)
        return out

    def schemas(self, groups: Optional[List[str]] = None) -> List[dict]:
        """工具的 JSON Schema（可按组过滤），直接喂给 LLM。"""
        return [t.schema() for t in self.list(groups)]

    def select(self, query: str, k: int = 8, groups: Optional[List[str]] = None) -> List[Tool]:
        """按查询文本动态选择最相关的 Top-K 工具。

        计分：query 分词与「工具名 + 描述」分词的重叠数（零 LLM 开销）；
        无任何命中时回退为全量启用工具，保证步骤不因检索空手而失去工具。
        """
        pool = self.list(groups)
        q = tokenize(query)
        if not q or k <= 0 or len(pool) <= k:
            return pool
        scored = []
        for t in pool:
            score = len(q & tokenize(f"{t.name} {t.description}"))
            if score > 0:
                scored.append((score, t.name, t))
        if not scored:
            return pool
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, _, t in scored[:k]]

    def __contains__(self, name: str) -> bool:
        return name in self._tools
