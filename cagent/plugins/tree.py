"""操作树：从 YAML 加载，支持多层按需披露的路径查询。"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml


@dataclass
class OperationNode:
    """操作树节点。

    - 中间节点（branch）：有 children，无 execute，只路由不执行
    - 叶子节点（leaf）：有 execute，无 children，可执行
    """

    name: str
    path: str                        # 完整路径，如 "shell.git.commit"
    summary: str                     # 一句话简介（上层披露用）
    detail: Optional[str] = None     # 完整说明（叶子层披露用）
    children: Dict[str, "OperationNode"] = field(default_factory=dict)
    params: Dict[str, dict] = field(default_factory=dict)
    execute: Optional[dict] = None   # {mode: "executor"|"shell", handler/template}

    @property
    def is_leaf(self) -> bool:
        return self.execute is not None

    @property
    def is_branch(self) -> bool:
        return bool(self.children)


class OperationTree:
    """操作树：从 tool.yaml 加载，支持按路径查询和披露。"""

    def __init__(self):
        self._roots: Dict[str, OperationNode] = {}

    def load_plugin(self, yaml_path: str) -> str:
        """从 tool.yaml 加载一个插件的操作树。

        返回加载的 group 名。
        """
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        group = data.get("group") or data.get("plugin")
        if not group:
            raise ValueError(f"tool.yaml 缺少 group/plugin 字段: {yaml_path}")
        root = OperationNode(
            name=group,
            path=group,
            summary=data.get("summary", ""),
            detail=data.get("detail"),
        )
        for item in data.get("tree", []):
            child = self._build_node(item, parent_path=group)
            root.children[child.name] = child
        self._roots[group] = root
        return group

    def _build_node(self, data: dict, parent_path: str) -> OperationNode:
        """递归构建操作树节点。"""
        name = data["name"]
        path = f"{parent_path}.{name}"
        children = {}
        for child_data in data.get("children", []):
            child = self._build_node(child_data, parent_path=path)
            children[child.name] = child
        return OperationNode(
            name=name,
            path=path,
            summary=data.get("summary", ""),
            detail=data.get("detail"),
            children=children,
            params=data.get("params", {}),
            execute=data.get("execute"),
        )

    def query(self, path: str) -> Optional[OperationNode]:
        """按路径查询节点（如 'shell.git.commit'）。"""
        if not path:
            return None
        parts = path.split(".")
        node = self._roots.get(parts[0])
        for part in parts[1:]:
            if node is None:
                return None
            node = node.children.get(part)
        return node

    def list_groups(self) -> List[str]:
        """列出所有根组名。"""
        return list(self._roots.keys())

    def list_leaf_paths(self) -> List[str]:
        """列出所有叶子路径。"""
        result = []
        for root in self._roots.values():
            self._collect_leaves(root, result)
        return result

    def _collect_leaves(self, node: OperationNode, out: List[str]) -> None:
        if node.is_leaf:
            out.append(node.path)
        for child in node.children.values():
            self._collect_leaves(child, out)

    def __contains__(self, path: str) -> bool:
        return self.query(path) is not None
