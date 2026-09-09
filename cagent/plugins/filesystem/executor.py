"""filesystem 插件执行器：apply_patch。"""
import os
import shutil
from typing import List, Optional

from cagent.tools.base import ToolResult
from cagent.tools.builtin._guard import PathGuard

# ── 插件配置（由 PluginExecutor 在加载时注入）────────────────
_config = {}
_guard = None


def configure(config: dict) -> None:
    """插件加载时调用，注入配置。"""
    global _config, _guard
    _config = config
    _guard = PathGuard(config.get("work_dir", "."))


# ── 补丁解析基础设施 ──────────────────────────────────────


class PatchError(Exception):
    """补丁格式错误。"""


class _Hunk:
    def __init__(self):
        self.context: List[str] = []
        self.removed: List[str] = []
        self.added: List[str] = []


class _PatchAction:
    def __init__(self, kind: str):
        self.kind: str = kind
        self.path: Optional[str] = None
        self.new_path: Optional[str] = None
        self.hunks: List[_Hunk] = []
        self.added_lines: List[str] = []


def _parse_patch(patch_text: str) -> List[_PatchAction]:
    lines = patch_text.split("\n")
    actions: List[_PatchAction] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if line == "":
            i += 1
            continue
        if line == "*** Begin Patch" or line == "*** End Patch":
            i += 1
            continue
        if line.startswith("*** Add File:"):
            action = _PatchAction("add")
            action.path = line[len("*** Add File:"):].strip()
            i += 1
            i = _collect_added_lines(lines, i, action)
            actions.append(action)
            continue
        if line.startswith("*** Update File:"):
            action = _PatchAction("update")
            action.path = line[len("*** Update File:"):].strip()
            i += 1
            i = _collect_hunks(lines, i, action)
            actions.append(action)
            continue
        if line.startswith("*** Delete File:"):
            action = _PatchAction("delete")
            action.path = line[len("*** Delete File:"):].strip()
            i += 1
            actions.append(action)
            continue
        if line.startswith("*** Move File:"):
            action = _PatchAction("move")
            rest = line[len("*** Move File:"):].strip()
            if "->" not in rest:
                raise PatchError(f"Move 格式错误，缺少 -> : {line}")
            parts = rest.split("->", 1)
            action.path = parts[0].strip()
            action.new_path = parts[1].strip()
            i += 1
            actions.append(action)
            continue
        raise PatchError(f"无法解析补丁第 {i + 1} 行: {line}")
    return actions


def _collect_added_lines(lines: List[str], i: int, action: _PatchAction) -> int:
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "*** End Patch":
            return i + 1
        if line.startswith("*** "):
            return i
        if line.startswith("+"):
            action.added_lines.append(line[1:])
        elif line == "":
            action.added_lines.append("")
        else:
            raise PatchError(
                f"Add File 中的行应以 + 开头，第 {i + 1} 行: {line}"
            )
        i += 1
    return i


def _is_unified_diff_metadata(line: str) -> bool:
    """检测 unified diff 格式的元数据行（---、+++、@@ ... @@）。

    LLM 生成的补丁有时会混入 unified diff 头，这些行不是实际内容，
    应该被跳过，避免被误当作上下文行。
    """
    # @@ ... @@ 是 hunk 范围头
    stripped = line.strip()
    if stripped.startswith("@@") and stripped.endswith("@@"):
        return True
    # --- / +++ 是文件路径头（--- 原文件、+++ 新文件）
    # 前缀直接检查前 3 字符，第 4 字符是空格或行尾
    if len(line) >= 3:
        if line[:3] in ("---", "+++"):
            return True
    return False


def _collect_hunks(lines: List[str], i: int, action: _PatchAction) -> int:
    n = len(lines)
    hunk = _Hunk()
    has_content = False
    while i < n:
        line = lines[i]
        if line.strip() == "*** End Patch":
            if has_content:
                action.hunks.append(hunk)
            return i + 1
        if line.startswith("*** "):
            if has_content:
                action.hunks.append(hunk)
            return i
        # 跳过 unified diff 元数据行（---、+++、@@ ... @@）
        if _is_unified_diff_metadata(line):
            i += 1
            continue
        if line.startswith("+"):
            hunk.added.append(line[1:])
            has_content = True
        elif line.startswith("-"):
            hunk.removed.append(line[1:])
            has_content = True
        elif line.startswith(" "):
            if has_content and (hunk.added or hunk.removed):
                action.hunks.append(hunk)
                hunk = _Hunk()
                has_content = False
            hunk.context.append(line[1:])
        else:
            if has_content and (hunk.added or hunk.removed):
                action.hunks.append(hunk)
                hunk = _Hunk()
                has_content = False
            hunk.context.append(line)
        i += 1
    if has_content:
        action.hunks.append(hunk)
    return i


def _apply_hunks(original: List[str], hunks: List[_Hunk]) -> List[str]:
    result = list(original)
    offset = 0
    for hunk in hunks:
        search_block = hunk.context + hunk.removed
        if not search_block:
            insert_at = len(result) + offset
            result[insert_at:insert_at] = hunk.added
            offset += len(hunk.added)
            continue
        pos = _find_block(result, search_block, offset)
        if pos < 0:
            raise PatchError(
                f"无法在文件中定位补丁上下文：期望找到 {' | '.join(search_block[:3])}..."
            )
        ctx_len = len(hunk.context)
        del_start = pos + ctx_len
        del_end = del_start + len(hunk.removed)
        result[del_start:del_end] = hunk.added
        offset += len(hunk.added) - len(hunk.removed)
    return result


def _find_block(lines: List[str], block: List[str], start: int = 0) -> int:
    if not block:
        return -1
    n = len(lines)
    blen = len(block)
    if blen > n:
        return -1
    for i in range(start, n - blen + 1):
        match = True
        for j in range(blen):
            if lines[i + j].lstrip() != block[j].lstrip():
                match = False
                break
        if match:
            return i
    if start > 0:
        for i in range(0, min(start, n - blen + 1)):
            match = True
            for j in range(blen):
                if lines[i + j].lstrip() != block[j].lstrip():
                    match = False
                    break
            if match:
                return i
    return -1


# ── 执行函数 ──────────────────────────────────────────────


def _scoped_resolve(rel: str) -> Optional[str]:
    """会话作用域内的写路径解析；无作用域时返回 None（走原 PathGuard）。

    规则（会话路径隔离）：
    - scheme://（session:// / workspace://）→ 经会话 PathSpace 解析 + assert_safe(write)
    - 裸相对路径 → scratch（临时/中间文件落会话目录）
    - 绝对路径 → 必须位于 scratch 或空间根内，否则 PermissionError
    """
    from cagent.runtime.session_scope import current_scope

    scope = current_scope()
    if scope is None:
        return None
    s = rel.strip()
    if "://" in s:
        if scope.path_space is None:
            raise PermissionError(f"会话作用域缺少 PathSpace，无法解析: {s}")
        phys = scope.path_space.resolve(s, mode="write")
        scope.path_space.assert_safe(phys, mode="write")
        return str(phys)
    if os.path.isabs(s) or s.startswith("~"):
        p = os.path.realpath(os.path.expanduser(s))
    else:
        p = os.path.realpath(os.path.join(str(scope.scratch), s))
    if not (scope.in_scratch(p) or scope.in_space(p)):
        raise PermissionError(
            f"路径越界: {rel} 不在会话目录或项目空间内；"
            f"临时文件请用相对路径（落会话目录），项目文件请用 workspace:// 前缀"
        )
    return str(p)


def _audit_workspace_write(phys_path: str, op: str) -> None:
    """空间写入审计：路径位于空间根内时登记（无空间模式不登记）。"""
    from cagent.runtime.session_scope import current_scope

    scope = current_scope()
    if scope is None or scope.space_root is None:
        return
    if not scope.in_space(phys_path):
        return
    if scope.record_write is None:
        return
    rel = os.path.relpath(phys_path, str(scope.space_root))
    scope.record_write(rel, op)


def _safe_write(rel: str) -> str:
    """统一写路径闸门：优先会话作用域解析，无作用域回退 PathGuard。"""
    phys = _scoped_resolve(rel)
    if phys is not None:
        return phys
    return _guard.safe_write_path(rel)


def apply_patch(patch: str, **kwargs) -> ToolResult:
    """应用补丁修改文件（Add/Update/Delete/Move）。"""
    if not patch or not patch.strip():
        return ToolResult(ok=False, content="", error="补丁内容为空")
    try:
        actions = _parse_patch(patch)
    except PatchError as e:
        return ToolResult(
            ok=False, content="",
            error=f"{e}\n提示：如需查看操作格式，请调用 tool_guide(path='filesystem.apply_patch')。",
        )
    if not actions:
        return ToolResult(ok=False, content="", error="补丁中未包含任何操作")
    results: List[str] = []
    allow_overwrite = _config.get("allow_overwrite", True)
    allow_delete = _config.get("allow_delete", False)
    for action in actions:
        try:
            msg = _apply_action(action, allow_overwrite, allow_delete)
            results.append(msg)
        except (PermissionError, FileNotFoundError, FileExistsError, PatchError) as e:
            return ToolResult(
                ok=False,
                content="\n".join(results),
                error=f"操作失败（{action.kind} {action.path or action.new_path}）: {e}",
            )
    return ToolResult(ok=True, content=f"已应用补丁：{'；'.join(results)}")


def _apply_action(action: _PatchAction, allow_overwrite: bool, allow_delete: bool) -> str:
    if action.kind == "add":
        return _do_add(action, allow_overwrite)
    if action.kind == "update":
        return _do_update(action)
    if action.kind == "delete":
        return _do_delete(action, allow_delete)
    if action.kind == "move":
        return _do_move(action, allow_overwrite)
    raise PatchError(f"未知操作类型: {action.kind}")


def _do_add(action: _PatchAction, allow_overwrite: bool) -> str:
    path = _safe_write(action.path)
    if os.path.exists(path) and not allow_overwrite:
        raise FileExistsError(f"文件已存在且不允许覆盖: {action.path}")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    content = "\n".join(action.added_lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _audit_workspace_write(path, "add")
    return f"新增 {action.path}（{len(action.added_lines)} 行）"


def _do_update(action: _PatchAction) -> str:
    path = _safe_write(action.path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {action.path}")
    with open(path, "r", encoding="utf-8") as f:
        original_lines = f.read().split("\n")
    new_lines = _apply_hunks(original_lines, action.hunks)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))
    change_count = sum(len(h.removed) + len(h.added) for h in action.hunks)
    _audit_workspace_write(path, "update")
    return f"修改 {action.path}（{change_count} 处变更）"


def _do_delete(action: _PatchAction, allow_delete: bool) -> str:
    if not allow_delete:
        raise PermissionError("配置不允许删除文件（allow_delete=False）")
    path = _safe_write(action.path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {action.path}")
    os.remove(path)
    _audit_workspace_write(path, "delete")
    return f"删除 {action.path}"


def _do_move(action: _PatchAction, allow_overwrite: bool) -> str:
    old_path = _safe_write(action.path)
    new_path = _safe_write(action.new_path)
    if not os.path.exists(old_path):
        raise FileNotFoundError(f"源文件不存在: {action.path}")
    if os.path.exists(new_path) and not allow_overwrite:
        raise FileExistsError(f"目标文件已存在且不允许覆盖: {action.new_path}")
    parent = os.path.dirname(new_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.move(old_path, new_path)
    _audit_workspace_write(new_path, "move")
    return f"移动 {action.path} -> {action.new_path}"
