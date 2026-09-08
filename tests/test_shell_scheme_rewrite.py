"""ShellExecutor scheme:// 路径自动改写测试。

覆盖三种核心场景：
1. workspace:// 自动改写为物理路径，shell 透明执行成功
2. 远程 scheme（http://）不被改写，保留原样
3. 改写失败（如 path_space 未注入）+ 命令残留 scheme → 触发 SCHEME_PATH_MISUSE
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cagent.plugins.shell_exec import ShellExecutor  # noqa: E402
from cagent.runtime.paths import Mount, PathSpace  # noqa: E402


@pytest.fixture
def space(tmp_path):
    """最小 PathSpace：workspace = tmp_path，data_dir = .data。"""
    return PathSpace.build_default(tmp_path, work_dir=tmp_path, data_dir=".data")


@pytest.fixture
def workspace_file(tmp_path):
    """在 workspace 根下放一个已知文件，供 ls/cat 等命令验证。"""
    f = tmp_path / "hello.txt"
    f.write_text("hi from workspace\n", encoding="utf-8")
    return f


# ── 1. workspace:// 自动改写为物理路径 ────────────────────────


def test_workspace_scheme_is_transparently_rewritten(space, workspace_file, tmp_path):
    """含 workspace:// 的命令被自动改写为物理路径，shell 透明执行成功。

    注：原测试用 cat，但框架已禁止 cat（无法指定行范围）。改用 wc -l 数文件行数
    来验证"改写后 shell 真的能读到物理文件"，且不依赖文件具体内容。
    """
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    # wc -l 数 hello.txt 的行数（内容 1 行 → 1）—— 若改写没生效，wc 报 No such file
    result = executor.execute(
        f'wc -l "workspace://hello.txt"'
    )
    assert result.ok, f"execute failed: {result.error}\n{result.content}"
    assert "[scheme 自动改写]" in result.content
    # 物理文件只有 1 行
    assert "1" in result.content


def test_workspace_scheme_zero_arg_reflects_physical_path(space, workspace_file, tmp_path):
    """sh -c 'echo "$0"' 打印框架传给 sh 的第 0 个位置参数 —— 应是物理路径。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    result = executor.execute(
        "sh -c 'printf \"%s\" \"$0\"' \"workspace://hello.txt\""
    )
    assert result.ok, f"execute failed: {result.error}\n{result.content}"
    # 提取 stdout（去掉 header 后的内容）
    body = result.content.split("stdout:\n", 1)[-1].strip()
    assert body == str(workspace_file)
    assert "workspace://" not in body


def test_workspace_scheme_in_complex_command(space, workspace_file, tmp_path):
    """管道 / 多 token 命令中的 scheme 也能命中改写。

    注：sed 在管道链中（右侧有 wc）不走行号化视图，保持原字节流让下游处理。
    """
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    result = executor.execute(
        f"sed -n '1,$p' \"workspace://hello.txt\" | wc -c"
    )
    assert result.ok, f"execute failed: {result.error}\n{result.content}"
    # wc -c 计数（"hi from workspace\n" = 18 字节）
    assert "18" in result.content


# ── 2. 远程 scheme 保留原样 ──────────────────────────────────


def test_remote_scheme_not_rewritten(space, tmp_path):
    """http:// 等远程 scheme 不是 PathSpace mount，原样传给 shell。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    # 用一个会快速失败的命令携带 http URL 验证改写没动它
    result = executor.execute(
        'curl -sS --max-time 1 "http://127.0.0.1:1/never"'
    )
    # 注定超时/拒绝（exit != 0），但关键是 content 头部不应出现「scheme 自动改写」
    assert "[scheme 自动改写]" not in result.content
    # 不应该是 SCHEME_PATH_MISUSE（远程 scheme 不在 path_space.mounts）
    assert result.error_kind != "SCHEME_PATH_MISUSE"


def test_unknown_scheme_not_rewritten(space, tmp_path):
    """未注册的 scheme（既非 mount 也非远程）原样保留。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    # 用 echo 把 token 原样打出
    probe = tmp_path / "_probe2.txt"
    result = executor.execute(
        f'echo "weird://foo" > {probe}'
    )
    assert result.ok, f"execute failed: {result.error}\n{result.content}"
    assert "[scheme 自动改写]" not in result.content
    assert probe.read_text(encoding="utf-8").strip() == "weird://foo"


# ── 3. 改写失败 → SCHEME_PATH_MISUSE 错误分类 ──────────────


def test_scheme_path_misuse_when_no_path_space(tmp_path, workspace_file):
    """path_space 未注入时，框架无 mount 概念，错误回到通用 EXEC_ERROR
    （保持向后兼容：不假装识别 framework scheme）。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)})  # 没传 path_space
    # sed 一个含 scheme 的"文件"：shell 报 No such file，走通用 EXEC_ERROR
    result = executor.execute('sed -n \'1,$p\' "workspace://hello.txt"')
    assert not result.ok
    assert result.error_kind == "EXEC_ERROR"
    assert "No such file" in result.content
    # 关键：不要误触发 SCHEME_PATH_MISUSE（path_space 未注入时无法识别 mount）
    assert "SCHEME_PATH_MISUSE" not in (result.error_kind or "")


def test_scheme_path_misuse_when_resolution_fails(tmp_path):
    """_normalize_scheme_paths 改不动（patch 强制返回原 command）+ 含 workspace://
    且 exit != 0 + stderr 不含 "No such file" → 触发 SCHEME_PATH_MISUSE。"""
    from unittest.mock import patch

    space = PathSpace.build_default(tmp_path, work_dir=tmp_path, data_dir=".data")
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)

    # 强制改写失败：返回原 command + 空 rewrites
    with patch.object(
        ShellExecutor,
        "_normalize_scheme_paths",
        return_value=("false \"workspace://x\"", []),
    ):
        result = executor.execute('false "workspace://x"')

    assert not result.ok
    assert result.error_kind == "SCHEME_PATH_MISUSE"
    assert "workspace://" in (result.hint or "")


# ── 4. 边界：path_space 未注入时旧行为不变 ────────────────────


def test_legacy_no_path_space_keeps_old_behavior(tmp_path):
    """没注入 path_space 时，scheme 不会被改写、也不会触发 SCHEME_PATH_MISUSE
    （保持向后兼容：旧端 / 单测直调 ShellExecutor 不受影响）。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)})
    # 远程 scheme 命令（一定不会成功执行）—— 关键是 error_kind 不是 SCHEME_PATH_MISUSE
    result = executor.execute(
        'curl -sS --max-time 1 "http://127.0.0.1:1/never"'
    )
    assert result.error_kind != "SCHEME_PATH_MISUSE"
    assert "[scheme 自动改写]" not in result.content


# ── 5. 改写 header 在成功 / 失败路径都可见 ────────────────────


def test_rewrite_header_visible_on_success(space, workspace_file, tmp_path):
    """改写 header 在成功路径可见（改用 sed -n 1,$p，独立使用触发读取视图）。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    result = executor.execute(f'sed -n \'1,$p\' "workspace://hello.txt"')
    assert result.ok
    assert "[scheme 自动改写]" in result.content
    assert "workspace://hello.txt" in result.content
    assert str(workspace_file) in result.content


def test_rewrite_header_visible_on_failure(space, tmp_path):
    """改写后命令仍失败（如 sed 一个不存在的文件）时，header 也应保留。"""
    executor = ShellExecutor(config={"work_dir": str(tmp_path)}, path_space=space)
    result = executor.execute(f'sed -n \'1,$p\' "workspace://does_not_exist.txt"')
    assert not result.ok
    assert "[scheme 自动改写]" in result.content
