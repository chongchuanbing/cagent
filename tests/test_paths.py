"""PathSpace 单元测试：覆盖解析、relativize、越界、symlink、命令体扫描、
多级技能 scope、URL 分流、per-mount 权限。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cagent.runtime.paths import Mount, PathSpace  # noqa: E402


@pytest.fixture
def space(tmp_path):
    """构造一个最小 PathSpace：workspace=data 根 = tmp_path。"""
    return PathSpace.build_default(tmp_path, work_dir=tmp_path, data_dir=".data")


# ── resolve / relativize 往返 ────────────────────────────────

def test_resolve_relative_defaults_to_workspace(space, tmp_path):
    p = space.resolve("a/b.txt")
    assert p == (tmp_path / "a/b.txt")


def test_relativize_roundtrip(space, tmp_path):
    phys = tmp_path / "x" / "y.md"
    logic = space.relativize(phys)
    assert logic == "workspace://x/y.md"
    assert space.resolve(logic) == phys


def test_relativize_outside_base_returns_abs(space, tmp_path):
    # 受信外挂区之外的路径兜底返回绝对（不泄漏到 workspace）
    other = PathSpace.build_default(
        tmp_path, work_dir=tmp_path, allow_paths=(tmp_path / "ext",)
    )
    ext_file = tmp_path / "ext" / "z.txt"
    assert other.relativize(ext_file) == "workspace://ext/z.txt"


# ── 越界校验 ────────────────────────────────────────────────

def test_assert_safe_rejects_escape(space, tmp_path):
    with pytest.raises(PermissionError):
        space.assert_safe("/etc/passwd")


def test_assert_safe_allows_within_workspace(space, tmp_path):
    p = space.assert_safe(tmp_path / "sub" / "f.txt")
    assert p == (tmp_path / "sub" / "f.txt")


def test_allow_paths_whitelist(space, tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    sp = space.with_mount  # 占位，下面用 build_default
    sp2 = PathSpace.build_default(tmp_path, work_dir=tmp_path, allow_paths=(ext,))
    # 外部目录内的文件应放行
    target = ext / "x.txt"
    assert sp2.assert_safe(target) == target
    # 外部目录外的仍拒绝
    with pytest.raises(PermissionError):
        sp2.assert_safe("/etc/passwd")


def test_allow_symlink_target(space, tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path.parent / "outside_symlink_target"
    outside.mkdir(exist_ok=True)
    link = inside / "link"
    os.symlink(outside, link)
    # 未授权 symlink 目标：拒绝
    with pytest.raises(PermissionError):
        space.assert_safe(link, mode="read")
    # 授权后：放行（返回 realpath 后的物理目标，即 symlink 指向的 outside 目录）
    sp2 = PathSpace.build_default(
        tmp_path, work_dir=tmp_path, allow_symlink_targets=(outside,)
    )
    result = sp2.assert_safe(link, mode="read")
    assert str(result).endswith("outside_symlink_target")


# ── 命令体扫描（收编 shell 沙箱） ────────────────────────────

def test_command_violations_skips_system_paths(space, tmp_path):
    # 系统命令路径应放行，不视为越界
    cmd = "grep -r foo /usr/bin/someprog | cat /etc/hosts"
    # /etc/hosts 是用户越界目标（不在 workspace）
    v = space.command_path_violations(cmd)
    assert "/etc/hosts" in v
    assert not any(x.startswith("/usr/bin") for x in v)


def test_command_violations_detects_escape(space, tmp_path):
    v = space.command_path_violations("cat /Users/chongcb/.workbuddy/skills/x.md")
    assert v == ["/Users/chongcb/.workbuddy/skills/x.md"]


def test_command_violations_dev_null_skipped(space, tmp_path):
    v = space.command_path_violations("echo hi > /dev/null")
    assert v == []


def test_command_violations_relative_ok(space, tmp_path):
    # 相对路径不进绝对路径扫描，无越界
    assert space.command_path_violations("sed -n 1,5p ./cagent/skills/x/SKILL.md") == []


# ── 多级技能 scope ──────────────────────────────────────────

def test_resolve_skill_scope(space, tmp_path):
    # 模拟 skills://tencent-docx 挂载
    skill_root = tmp_path / "cagent" / "skills" / "tencent-docx"
    sp = space.with_mount(
        Mount("skills", tmp_path / "cagent" / "skills", expose_to_llm=True)
    )
    # 裸 references/x.md 在 skill 上下文应解析到该技能根
    p = sp.resolve("references/create-from-scratch.md", scope="skills:tencent-docx")
    assert p == (skill_root / "references" / "create-from-scratch.md")


def test_relativize_skill_mount(space, tmp_path):
    skill_root = tmp_path / "cagent" / "skills" / "tencent-docx"
    sp = space.with_mount(
        Mount("skills", tmp_path / "cagent" / "skills", expose_to_llm=True)
    )
    target = skill_root / "references" / "x.md"
    # expose_to_llm 的 mount 优先匹配
    assert sp.relativize(target) == "skills://tencent-docx/references/x.md"


# ── URL 分流 ────────────────────────────────────────────────

def test_classify_remote(space):
    assert space.classify("https://example.com/a.md") == "remote"
    assert space.classify("s3://bucket/k") == "remote"


def test_classify_local_and_scheme(space):
    assert space.classify("./a/b.txt") == "local"
    assert space.classify("workspace://x") == "scheme"
    assert space.classify("file:///tmp/x") == "local"


def test_resolve_remote_raises(space):
    with pytest.raises(ValueError):
        space.resolve("https://example.com/a.md")


# ── per-mount 权限 ──────────────────────────────────────────

def test_self_mount_readonly(space, tmp_path):
    # self:// 仅 read，写模式应拒绝（即便在 workspace 内也无关，这里测 mount 权限）
    sp = PathSpace.build_default(tmp_path, work_dir=tmp_path)
    self_dir = sp.mounts["self"].physical
    target = self_dir / "schema.py"
    # read 允许
    assert sp.assert_safe(target, mode="read") == target
    # write 拒绝（self mount 不允许 write）
    with pytest.raises(PermissionError):
        sp.assert_safe(target, mode="write")


# ── 冻结语义 ────────────────────────────────────────────────

def test_frozen_with_mount_returns_new(space, tmp_path):
    sp2 = space.with_mount(Mount("extra", tmp_path / "extra"))
    assert "extra" in sp2.mounts
    assert "extra" not in space.mounts  # 原实例不变
