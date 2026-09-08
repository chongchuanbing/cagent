"""ShellExecutor 读取语义视图：cat 黑名单 + 行号化输出 + 续读契约。"""
import os
import tempfile

import pytest

from cagent.plugins.shell_exec import ShellExecutor


# ───────────────────────────── fixtures ─────────────────────────────


@pytest.fixture
def work_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def make_file(work_dir):
    """工厂：写入指定行数文件，返回文件绝对路径。"""

    def _factory(name: str, lines: int, content_prefix: str = "line") -> str:
        path = os.path.join(work_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for i in range(1, lines + 1):
                f.write(f"{content_prefix} {i}\n")
        return path

    return _factory


# ───────────────────────────── cat 黑名单 ─────────────────────────────


class TestCatBlocked:
    """cat 整读 → 拒绝，引导用 sed / head / tail / grep -n。"""

    def test_cat_rejected(self, work_dir):
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute("cat some_file.py", cwd=work_dir)
        assert not r.ok
        assert r.error_kind == "BLOCKED_COMMAND"
        assert "禁止使用 'cat'" in r.error
        assert "sed" in r.hint
        assert "shell.sed" in r.hint

    def test_cat_with_path_rejected(self, work_dir):
        """带绝对路径的 cat 也被拦截。"""
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute("cat /tmp/foo.py", cwd=work_dir)
        assert not r.ok
        assert r.error_kind == "BLOCKED_COMMAND"

    def test_cat_via_pipe_rejected(self, work_dir):
        """管道链里的 cat 也被拦截（取管道左侧首个命令）。"""
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute("cat file.py | grep pattern", cwd=work_dir)
        assert not r.ok
        assert r.error_kind == "BLOCKED_COMMAND"

    def test_cat_disabled(self, work_dir):
        """block_cat=false 时 cat 允许（向后兼容逃生口）。"""
        se = ShellExecutor({"work_dir": work_dir, "block_cat": False})
        # 用 cat /etc/hostname 这种系统文件做测试
        r = se.execute("cat /etc/hostname", cwd=work_dir)
        # 不再关心内容，只确认不被 BLOCKED_COMMAND 拦截
        assert r.error_kind != "BLOCKED_COMMAND"


# ───────────────────────────── 行号化视图：sed / head / tail / awk ────


class TestReadView:
    """读取类命令 → 行号化输出 + 头部契约 + 续读指令。"""

    def test_sed_basic(self, work_dir, make_file):
        path = make_file("a.txt", 100)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"sed -n '5,10p' {path}", cwd=work_dir)
        assert r.ok
        # 头部契约（在 stdout 块内）
        assert "=== " in r.content
        assert "lines 5-10 of 100" in r.content
        # 续读指令
        assert "sed -n '11," in r.content
        assert "grep -n 'pattern'" in r.content
        # 行号前缀
        assert "    5│" in r.content
        assert "   10│" in r.content

    def test_sed_dollar_end(self, work_dir, make_file):
        path = make_file("a.txt", 50)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"sed -n '45,$p' {path}", cwd=work_dir)
        assert r.ok
        assert "lines 45-50 of 50" in r.content
        # 全部读完，结尾应为 End of file
        assert "[End of file - 50 lines total]" in r.content

    def test_head_n(self, work_dir, make_file):
        path = make_file("a.txt", 1000)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"head -n 5 {path}", cwd=work_dir)
        assert r.ok
        assert "lines 1-5 of 1000" in r.content
        assert "    5│" in r.content

    def test_tail_n(self, work_dir, make_file):
        path = make_file("a.txt", 1000)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"tail -n 5 {path}", cwd=work_dir)
        assert r.ok
        # tail -n 5 → 996-1000
        assert "lines 996-1000 of 1000" in r.content
        assert "  996│" in r.content
        assert "1000│" in r.content

    def test_awk_nr_range(self, work_dir, make_file):
        path = make_file("a.txt", 100)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"awk 'NR>=20 && NR<=25' {path}", cwd=work_dir)
        assert r.ok
        assert "lines 20-25 of 100" in r.content

    def test_awk_unbounded(self, work_dir, make_file):
        """awk 只给 NR<=B，应识别为 1-B。"""
        path = make_file("a.txt", 100)
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"awk 'NR<=10' {path}", cwd=work_dir)
        assert r.ok
        assert "lines 1-10 of 100" in r.content

    def test_nl_default(self, work_dir, make_file):
        """nl 不带参数 → 取前 read_max_lines 行。"""
        path = make_file("a.txt", 50)
        se = ShellExecutor({"work_dir": work_dir, "read_max_lines": 20})
        r = se.execute(f"nl {path}", cwd=work_dir)
        assert r.ok
        assert "lines 1-20 of 50" in r.content

    def test_oversized_file_truncates_with_continue_hint(self, work_dir, make_file):
        """5000 行文件，整读类 sed → 取前 read_max_lines 行 + 续读指令。"""
        path = make_file("big.txt", 5000)
        se = ShellExecutor({"work_dir": work_dir, "read_max_lines": 100})
        r = se.execute(f"sed -n '1,5000p' {path}", cwd=work_dir)
        assert r.ok
        assert "lines 1-100 of 5000" in r.content
        # 续读指令：下一段从 101 开始
        assert "sed -n '101," in r.content

    def test_line_max_chars_truncation(self, work_dir):
        """单行超长 → 截断并加提示。"""
        path = os.path.join(work_dir, "wide.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("a" * 5000 + "\n")
        se = ShellExecutor({"work_dir": work_dir, "read_line_max_chars": 100})
        r = se.execute(f"sed -n '1,1p' {path}", cwd=work_dir)
        assert r.ok
        assert "(本行截断" in r.content

    def test_missing_file_falls_back(self, work_dir):
        """目标文件不存在 → sed 命令以 exit_code=1 失败，非 ok。"""
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(
            f"sed -n '1,10p' {work_dir}/nonexistent_zzz.txt", cwd=work_dir
        )
        assert not r.ok


# ───────────────────────────── 检索类（grep -n） ──────────────────────


class TestGrepView:
    """grep -n / rg → 原样输出（已带行号）+ 头部契约。"""

    def test_grep_n_passes_through(self, work_dir, make_file):
        # 在 50 行写一个特殊词
        path = os.path.join(work_dir, "grep_target.txt")
        with open(path, "w") as f:
            for i in range(1, 101):
                marker = "MATCH" if i == 50 else ""
                f.write(f"line {i} {marker}\n")
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute(f"grep -n MATCH {path}", cwd=work_dir)
        assert r.ok
        assert "=== " in r.content
        assert "[read] grep -n" in r.content
        # grep 输出应该原样透传（带行号 50:line 50 MATCH）
        assert "50:line 50 MATCH" in r.content
        # 给续读指引
        assert "sed -n 'A,Bp'" in r.content


# ───────────────────────────── 普通命令不受影响 ───────────────────────


class TestNormalCommandUnchanged:
    """非读取类命令 → 仍走原 max_output_chars 字符硬切。"""

    def test_echo_unchanged(self, work_dir):
        se = ShellExecutor({"work_dir": work_dir})
        r = se.execute("echo hello", cwd=work_dir)
        assert r.ok
        assert r.content == "[exit_code=0]\nstdout:\nhello\n"

    def test_no_header_prefix_for_non_read(self, work_dir, make_file):
        path = make_file("a.txt", 5)
        se = ShellExecutor({"work_dir": work_dir})
        # 改用 cat 之外的非读取命令（这里用 ls 来确认无读取视图）
        r = se.execute(f"ls -la {path}", cwd=work_dir)
        assert r.ok
        # 没有 === 前缀（不是读取类）
        assert "=== " not in r.content


# ───────────────────────────── loop._clip_observation 联动 ───────────


class TestClipObservationIntegration:
    """loop._clip_observation 对读取类输出 → 大阈值 + 保留头部契约。"""

    def _make_read_output(self, body_len: int = 100):
        """构造读取视图风格的输出：=== <path> | N lines | ... ===\\n[read] ... + body。"""
        body = "x" * body_len
        return (
            "=== /tmp/foo.py | 5432 lines | ~85KB ===\n"
            f"[read] lines 1-100 of 5432 (sed)\n"
            f"{body}"
        )

    def test_short_read_output_untouched(self):
        from cagent.core.loop import AgentLoop

        loop = AgentLoop(planner=None, executor=None)
        text = self._make_read_output(body_len=200)
        out = loop._clip_observation(text)
        # 短于默认 read_max_chars=30000，原样
        assert out == text

    def test_long_read_output_truncated_preserves_header(self):
        from cagent.core.loop import AgentLoop

        loop = AgentLoop(planner=None, executor=None)
        text = self._make_read_output(body_len=50000)
        out = loop._clip_observation(text)
        # 头部契约必须保留
        assert "=== /tmp/foo.py | 5432 lines | ~85KB ===" in out
        assert "[read] lines 1-100 of 5432 (sed)" in out
        # 应有截断提示
        assert "已截断" in out
        # 长度不超过 read_max_chars (默认 30000) + 一定余量
        assert len(out) <= 30500

    def test_normal_observation_still_clipped_at_500(self):
        from cagent.core.loop import AgentLoop

        loop = AgentLoop(planner=None, executor=None)
        text = "x" * 1000
        out = loop._clip_observation(text)
        # 应走 observation_max_chars=500
        assert len(out) < 600
        assert "已截断" in out