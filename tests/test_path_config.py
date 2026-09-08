"""Phase 1：配置收敛 —— PathSpaceConfig / build_path_space / data_dir 唯一来源。"""
import os
import textwrap

import yaml

from cagent.config.provider import ConfigProvider
from cagent.config.schema import AgentConfig, PathSpaceConfig


def _write_cfg(tmp_path, text):
    p = tmp_path / "agent.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_paths_config_defaults():
    p = PathSpaceConfig()
    assert p.data_dir == ".data"
    assert p.base_dir is None and p.work_dir is None
    assert p.allow_paths == [] and p.allow_symlink_targets == []
    assert p.expose_data_to_llm is False
    # AgentConfig 默认带 paths 段
    assert AgentConfig().paths.data_dir == ".data"


def test_data_dir_default_from_config(tmp_path):
    cfg = ConfigProvider(_write_cfg(tmp_path, "model:\n  model: x\n"), watch=False)
    assert cfg.data_dir == ".data"


def test_data_dir_from_yaml_paths(tmp_path):
    path = _write_cfg(
        tmp_path,
        """
        paths:
          data_dir: mydata
        """,
    )
    cfg = ConfigProvider(path, watch=False)
    assert cfg.data_dir == "mydata"


def test_data_dir_explicit_override_wins(tmp_path):
    path = _write_cfg(
        tmp_path,
        """
        paths:
          data_dir: from_yaml
        """,
    )
    cfg = ConfigProvider(path, watch=False, data_dir="override")
    assert cfg.data_dir == "override"


def test_build_path_space_defaults(tmp_path):
    cfg = ConfigProvider(_write_cfg(tmp_path, "model:\n  model: x\n"), watch=False)
    space = cfg.build_path_space(str(tmp_path))
    assert space.mounts["workspace"].physical == tmp_path.resolve()
    assert space.mounts["workspace"].expose_to_llm is True
    # data:// 默认对模型隐藏
    assert space.mounts["data"].expose_to_llm is False
    assert space.mounts["data"].physical == (tmp_path / ".data").resolve()


def test_build_path_space_from_yaml(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _write_cfg(
        tmp_path,
        f"""
        paths:
          data_dir: .custom_data
          allow_paths:
            - {outside}
        """,
    )
    cfg = ConfigProvider(path, watch=False)
    space = cfg.build_path_space(str(tmp_path))
    assert space.mounts["data"].physical == (tmp_path / ".custom_data").resolve()
    # 受信外挂区生效：外部路径可读
    assert space.assert_safe(outside / "f.txt", mode="read") is not None


def test_build_path_space_expose_data(tmp_path):
    path = _write_cfg(
        tmp_path,
        """
        paths:
          expose_data_to_llm: true
        """,
    )
    cfg = ConfigProvider(path, watch=False)
    space = cfg.build_path_space(str(tmp_path))
    assert space.mounts["data"].expose_to_llm is True


def test_build_path_space_plugins_and_config_dirs(tmp_path):
    cfg = ConfigProvider(_write_cfg(tmp_path, "model:\n  model: x\n"), watch=False)
    pd = tmp_path / "plugins"
    cd = tmp_path / "cfgs"
    pd.mkdir()
    cd.mkdir()
    space = cfg.build_path_space(str(tmp_path), plugins_dir=str(pd), config_dir=str(cd))
    assert space.mounts["plugins"].physical == pd.resolve()
    assert space.mounts["config"].physical == cd.resolve()
    # 框架目录默认不对模型暴露（只读/隐藏）
    assert space.mounts["plugins"].expose_to_llm is False
    assert space.mounts["self"].modes == frozenset({"read"})


def test_work_dir_separate_from_base(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    path = _write_cfg(
        tmp_path,
        """
        paths:
          work_dir: sub
        """,
    )
    cfg = ConfigProvider(path, watch=False)
    space = cfg.build_path_space(str(tmp_path))
    assert space.mounts["workspace"].physical == sub.resolve()


def test_old_runtime_config_removed():
    """旧 utils.config.Config 应已删除，路径配置唯一来源是 config.schema.PathSpaceConfig。"""
    import cagent.utils as u

    assert not hasattr(u, "Config")
    assert not hasattr(u, "load_config")
    assert not os.path.exists(
        os.path.join(os.path.dirname(u.__file__), "config.py")
    )


def test_agent_yaml_paths_section_documented():
    """项目配置文件应包含 paths 段说明（文档性约束）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "config", "agent.yaml"), encoding="utf-8").read()
    assert "paths:" in text and "data_dir" in text
    # 确认是注释形态，不改变运行时默认
    parsed = yaml.safe_load(text)
    assert "paths" not in parsed or parsed["paths"] is None
