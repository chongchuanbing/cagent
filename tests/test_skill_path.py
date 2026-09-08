"""Phase 3：技能链接重写 + read_skill_file + 路径速查卡 测试。"""
import os
import textwrap

import pytest

from cagent.plugins.skill_executor import (
    ReadSkillFileTool,
    RunSkillTool,
    SkillGuideTool,
    _rewrite_skill_links,
)
from cagent.plugins.skill_loader import SkillDefinition
from cagent.prompts.react_prompt import build_react_system_prompt
from cagent.runtime.paths import PathSpace


def _make_skill(tmp_path, name="tencent-docx", body=""):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: %s\ndescription: d\n---\n\n%s" % (name, body), encoding="utf-8"
    )
    return SkillDefinition(name=name, description="d", body=body, skill_dir=str(skill_dir))


def test_rewrite_relative_references():
    body = textwrap.dedent(
        """
        - [从零开始](references/create-from-scratch.md)
        - [编排器](./skills/tdoc-orchestrator/SKILL.md)
        - [官网](https://example.com/x) 不变
        - [绝对](/etc/passwd) 不变
        - [已改写](skills://tencent-docx/y.md) 不变
        """
    )
    out = _rewrite_skill_links(body, "tencent-docx")
    assert "](skills://tencent-docx/references/create-from-scratch.md)" in out
    assert "](skills://tencent-docx/skills/tdoc-orchestrator/SKILL.md)" in out
    # 远程/绝对/已带 scheme 的不动
    assert "](https://example.com/x)" in out
    assert "](/etc/passwd)" in out
    assert "](skills://tencent-docx/y.md)" in out


def test_skill_guide_injects_note_and_rewrite(tmp_path):
    skill = _make_skill(
        tmp_path, "tencent-pptx", "阅读 [create-from-scratch](references/create-from-scratch.md)"
    )
    tool = SkillGuideTool({"tencent-pptx": skill})
    res = tool.run(skill_name="tencent-pptx")
    assert res.ok
    assert "read_skill_file" in res.content
    assert ".workbuddy/skills/" in res.content  # 警示不要猜平台路径
    assert "](skills://tencent-pptx/references/create-from-scratch.md)" in res.content


def test_run_skill_injects_note(tmp_path):
    skill = _make_skill(tmp_path, "tencent-docx", "见 [编排](./skills/tdoc-orchestrator/SKILL.md)")
    tool = RunSkillTool({"tencent-docx": skill})
    res = tool.run(skill_name="tencent-docx")
    assert res.ok
    assert "read_skill_file" in res.content
    assert "](skills://tencent-docx/skills/tdoc-orchestrator/SKILL.md)" in res.content


def test_read_skill_file_success(tmp_path):
    skill = _make_skill(tmp_path, "tencent-docx")
    ref = tmp_path / "tencent-docx" / "references"
    ref.mkdir()
    (ref / "x.md").write_text("hello-ref", encoding="utf-8")
    tool = ReadSkillFileTool({"tencent-docx": skill})
    res = tool.run(skill_name="tencent-docx", relative_path="references/x.md")
    assert res.ok
    assert res.content == "hello-ref"


def test_read_skill_file_missing(tmp_path):
    skill = _make_skill(tmp_path, "tencent-docx")
    tool = ReadSkillFileTool({"tencent-docx": skill})
    res = tool.run(skill_name="tencent-docx", relative_path="references/nope.md")
    assert not res.ok
    assert res.error_kind == "SANDBOX"
    assert res.hint


def test_read_skill_file_missing_returns_candidates(tmp_path):
    """文件不存在时，错误信息应附带技能目录内相近文件候选（防止瞎猜重试）。"""
    skill = _make_skill(tmp_path, "tencent-docx")
    ref = tmp_path / "tencent-docx" / "references"
    ref.mkdir()
    for name in ("component-svg.md", "component-box.md", "create-from-scratch.md"):
        (ref / name).write_text("x", encoding="utf-8")
    tool = ReadSkillFileTool({"tencent-docx": skill})
    res = tool.run(
        skill_name="tencent-docx", relative_path="references/component-shape.md"
    )
    assert not res.ok
    assert "component-svg.md" in res.error  # 相近候选回传
    assert "references/" in res.error  # 引导先列目录
    assert res.hint and "不要" in res.hint


def test_read_skill_file_list_dir(tmp_path):
    """relative_path 传目录应返回文件清单（先看后读的『看』）。"""
    skill = _make_skill(tmp_path, "tencent-docx")
    ref = tmp_path / "tencent-docx" / "references"
    ref.mkdir()
    (ref / "x.md").write_text("hello-ref", encoding="utf-8")
    (ref / "scripts").mkdir()
    tool = ReadSkillFileTool({"tencent-docx": skill})
    # 传目录名（不带斜杠）
    res = tool.run(skill_name="tencent-docx", relative_path="references")
    assert res.ok
    assert "x.md" in res.content
    assert "scripts/" in res.content  # 子目录带 / 标记
    # 传目录名（带斜杠）
    res2 = tool.run(skill_name="tencent-docx", relative_path="references/")
    assert res2.ok
    assert "x.md" in res2.content
    # 空路径 → 列出技能根目录
    res3 = tool.run(skill_name="tencent-docx", relative_path="")
    assert res3.ok
    assert "references/" in res3.content


def test_react_template_has_observe_first_discipline():
    """系统提示词应包含先看→再想→后做的行动纪律。"""
    from cagent.prompts.react_prompt import REACT_SYSTEM_TEMPLATE

    assert "先看" in REACT_SYSTEM_TEMPLATE
    assert "禁猜" in REACT_SYSTEM_TEMPLATE
    assert "自纠" in REACT_SYSTEM_TEMPLATE


def test_read_skill_file_traversal_rejected(tmp_path):
    # skill_dir 下没有 ../secrets，resolve_file 越界应拒绝
    skill = _make_skill(tmp_path, "tencent-docx")
    tool = ReadSkillFileTool({"tencent-docx": skill})
    res = tool.run(skill_name="tencent-docx", relative_path="../secrets.txt")
    assert not res.ok
    assert res.error_kind == "SANDBOX"


def test_read_skill_file_unknown_skill(tmp_path):
    tool = ReadSkillFileTool({})
    res = tool.run(skill_name="ghost", relative_path="x.md")
    assert not res.ok
    assert "不存在" in res.error


def test_path_card_in_prompt(tmp_path):
    ps = PathSpace.build_default(tmp_path, work_dir=tmp_path)
    card = ps.path_card()
    assert "workspace://" in card
    assert "skills://<name>/... : 技能资源，读取请用 read_skill_file" in card

    class _Step:
        description = "步骤A"

    prompt = build_react_system_prompt(
        _Step(), tools=[], env_facts="fd=可用", path_card=card
    )
    assert card in prompt
    assert "步骤A" in prompt
