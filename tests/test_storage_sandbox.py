"""Phase 4：storage 收编 PathSpace 沙箱 + 技能文件解析统一。

覆盖：
- 越界 key / 绝对路径 key 拒绝
- 指向外部目录的 symlink 拒绝
- 注入 path_space 后存储根取 data:// 挂载点
- 正常读写/列举不受影响
- SkillDefinition.resolve_file(..., path_space) 走统一沙箱
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cagent.runtime.paths import PathSpace, Mount  # noqa: E402
from cagent.storage import get_storage, LocalFileStorage  # noqa: E402
from cagent.plugins.skill_loader import SkillDefinition  # noqa: E402
from cagent.plugins.skill_executor import ReadSkillFileTool  # noqa: E402


def _make_space(tmp_path):
    return PathSpace.build_default(tmp_path, work_dir=tmp_path)


def test_escape_key_rejected():
    import tempfile
    d = tempfile.mkdtemp()
    store = LocalFileStorage(d)
    with pytest.raises(PermissionError):
        store.read_text("../escape.txt")
    with pytest.raises(PermissionError):
        store.write_text("../../etc/x", "x")


def test_absolute_key_rejected():
    import tempfile
    store = LocalFileStorage(tempfile.mkdtemp())
    with pytest.raises(PermissionError):
        store.read_text("/etc/passwd")


def test_external_symlink_rejected(tmp_path):
    import tempfile
    root = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    with open(os.path.join(outside, "secret.txt"), "w") as f:
        f.write("topsecret")
    link = os.path.join(root, "evil")
    os.symlink(outside, link)
    store = LocalFileStorage(root)
    with pytest.raises(PermissionError):
        store.read_text("evil/secret.txt")


def test_normal_rw_ok(tmp_path):
    store = LocalFileStorage(str(tmp_path))
    store.write_text("sessions/x/meta.json", '{"a":1}')
    assert store.read_text("sessions/x/meta.json") == '{"a":1}'
    assert store.exists("sessions/x/meta.json")
    keys = store.list_keys("sessions/")
    assert "sessions/x/meta.json" in keys
    store.delete("sessions/x/meta.json")
    assert not store.exists("sessions/x/meta.json")


def test_path_space_root_is_data_mount(tmp_path):
    space = PathSpace.build_default(
        tmp_path, work_dir=tmp_path, plugins_dir=str(tmp_path / "p")
    )
    store = get_storage(".data", path_space=space)
    # get_storage 应把 data:// 挂载点物理路径作为根
    assert store.root == str(space.mounts["data"].physical)
    assert space.mounts["data"].physical != str(tmp_path)


def test_storage_via_path_space_passes_assert_safe(tmp_path):
    space = _make_space(tmp_path)
    # data 挂载点必须真实存在以便写入
    os.makedirs(space.mounts["data"].physical, exist_ok=True)
    store = get_storage(".data", path_space=space)
    store.write_text("run/1.txt", "hello")
    assert store.read_text("run/1.txt") == "hello"


def test_resolve_file_via_path_space(tmp_path):
    skill_dir = tmp_path / "tencent-docx"
    ref = skill_dir / "references"
    ref.mkdir(parents=True)
    (ref / "create.md").write_text("ref-content")
    (skill_dir / "SKILL.md").write_text("x")

    skill = SkillDefinition(
        name="tencent-docx", description="d", skill_dir=str(skill_dir)
    )
    space = _make_space(tmp_path)
    # 正常：在 skill_dir 内，经 assert_safe 通过
    got = skill.resolve_file("references/create.md", path_space=space)
    assert os.path.samefile(got, ref / "create.md")
    # 越界：尝试跳出技能目录
    with pytest.raises(PermissionError):
        skill.resolve_file("../../etc/passwd", path_space=space)
    # 不存在
    with pytest.raises(FileNotFoundError):
        skill.resolve_file("nope.md", path_space=space)


def test_resolve_file_fallback_no_path_space(tmp_path):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("x")
    skill = SkillDefinition(name="s", description="d", skill_dir=str(skill_dir))
    got = skill.resolve_file("SKILL.md")  # 无 path_space 走旧校验
    assert os.path.exists(got)


def test_read_skill_file_tool_with_path_space(tmp_path):
    skill_dir = tmp_path / "tencent-docx"
    ref = skill_dir / "references"
    ref.mkdir(parents=True)
    (ref / "create.md").write_text("ref-content")
    (skill_dir / "SKILL.md").write_text("x")
    skill = SkillDefinition(
        name="tencent-docx", description="d", skill_dir=str(skill_dir)
    )
    space = _make_space(tmp_path)
    tool = ReadSkillFileTool({"tencent-docx": skill}, path_space=space)
    res = tool.run("tencent-docx", "references/create.md")
    assert res.ok and res.content == "ref-content"
    # 越界
    bad = tool.run("tencent-docx", "../../etc/passwd")
    assert not bad.ok
    # 未知技能
    assert not tool.run("nope", "x").ok
