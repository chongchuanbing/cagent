"""MCP 和 Skills 测试：MCP 客户端、Skills 加载器、skill_guide/run_skill。"""
import os
import tempfile

import pytest
import yaml

from cagent.plugins.mcp_client import McpClient, McpClientManager
from cagent.plugins.mcp_loader import McpPluginLoader
from cagent.plugins.skill_loader import SkillsPluginLoader, SkillDefinition
from cagent.plugins.skill_executor import SkillGuideTool, RunSkillTool
from cagent.plugins.tree import OperationTree
from cagent.config.schema import McpConfig, McpServerConfig, McpDefaults, SkillsConfig


# ── Skills 加载测试 ────────────────────────────────────────


@pytest.fixture
def skills_dir():
    """创建临时 skill 目录，含标准 SKILL.md 文件。"""
    with tempfile.TemporaryDirectory() as d:
        # code-review skill
        cr_dir = os.path.join(d, "code-review")
        os.makedirs(os.path.join(cr_dir, "references"))
        os.makedirs(os.path.join(cr_dir, "assets"))
        with open(os.path.join(cr_dir, "SKILL.md"), "w") as f:
            f.write(
                "---\n"
                "name: code-review\n"
                "description: 代码审查技能，对分支做全面审查\n"
                "license: Apache-2.0\n"
                "metadata:\n"
                "  author: cagent\n"
                "  version: \"1.0\"\n"
                "allowed-tools: run_tool(shell.git:*)\n"
                "---\n\n"
                "## 代码审查流程\n\n"
                "1. 获取变更: run_tool(shell.git.diff)\n"
                "2. 分析变更\n"
                "3. 生成报告\n"
            )
        with open(os.path.join(cr_dir, "references", "CHECKLIST.md"), "w") as f:
            f.write("# 审查清单\n- [ ] SQL 注入\n")

        # test-gen skill
        tg_dir = os.path.join(d, "test-gen")
        os.makedirs(tg_dir)
        with open(os.path.join(tg_dir, "SKILL.md"), "w") as f:
            f.write(
                "---\n"
                "name: test-gen\n"
                "description: 生成单元测试\n"
                "---\n\n"
                "## 测试生成\n\n"
                "1. 读取源文件\n"
                "2. 生成测试\n"
            )

        # 无效 skill（name 与目录名不一致）
        bad_dir = os.path.join(d, "wrong-name")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "SKILL.md"), "w") as f:
            f.write(
                "---\n"
                "name: different-name\n"
                "description: 不匹配\n"
                "---\n\nbody\n"
            )

        # 无效 skill（name 大写）
        upper_dir = os.path.join(d, "UPPER")
        os.makedirs(upper_dir)
        with open(os.path.join(upper_dir, "SKILL.md"), "w") as f:
            f.write(
                "---\n"
                "name: UPPER\n"
                "description: 大写不合法\n"
                "---\n\nbody\n"
            )

        yield d


class TestSkillsLoader:
    def test_load_valid_skills(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        assert "code-review" in skills
        assert "test-gen" in skills
        # 无效的 skill 应被跳过
        assert "different-name" not in skills
        assert "UPPER" not in skills

    def test_skill_definition_fields(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        skill = skills["code-review"]
        assert skill.name == "code-review"
        assert "代码审查" in skill.description
        assert skill.license == "Apache-2.0"
        assert skill.metadata.get("author") == "cagent"
        assert "run_tool(shell.git:*)" in skill.allowed_tools
        assert "代码审查流程" in skill.body
        assert "CHECKLIST" not in skill.body  # references 不在 body 中

    def test_skill_resolve_file(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        skill = skills["code-review"]
        path = skill.resolve_file("references/CHECKLIST.md")
        assert os.path.exists(path)

    def test_skill_resolve_file_traversal_blocked(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        skill = skills["code-review"]
        with pytest.raises(PermissionError):
            skill.resolve_file("../../../../etc/passwd")

    def test_multiple_dirs_override(self, skills_dir):
        """后加载的目录中的同名 skill 覆盖前面的。"""
        with tempfile.TemporaryDirectory() as d2:
            # 创建同名的 code-review skill 但内容不同
            cr2 = os.path.join(d2, "code-review")
            os.makedirs(cr2)
            with open(os.path.join(cr2, "SKILL.md"), "w") as f:
                f.write(
                    "---\nname: code-review\ndescription: 覆盖版本\n---\n\n新内容\n"
                )
            loader = SkillsPluginLoader()
            config = SkillsConfig(enabled=True, dirs=[skills_dir, d2])
            skills = loader.load_all(config)
            assert "代码审查" not in skills["code-review"].description
            assert "覆盖版本" in skills["code-review"].description

    def test_load_builtin_skills(self):
        """加载项目内置的 skills 目录。"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        builtin_dir = os.path.join(project_root, "cagent", "skills")
        if not os.path.isdir(builtin_dir):
            pytest.skip("内置 skills 目录不存在")
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[builtin_dir])
        skills = loader.load_all(config)
        assert "code-review" in skills
        assert "test-gen" in skills
        assert "refactor" in skills


# ── SkillGuideTool 测试 ────────────────────────────────────


class TestSkillGuide:
    def test_list_all_skills(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        guide = SkillGuideTool(skills)
        r = guide.run(skill_name="")
        assert r.ok
        assert "code-review" in r.content
        assert "test-gen" in r.content

    def test_get_skill_detail(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        guide = SkillGuideTool(skills)
        r = guide.run(skill_name="code-review")
        assert r.ok
        assert "代码审查流程" in r.content
        assert "run_tool" in r.content

    def test_nonexistent_skill(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        guide = SkillGuideTool(skills)
        r = guide.run(skill_name="nonexistent")
        assert not r.ok
        assert "不存在" in r.error

    def test_empty_skills(self):
        guide = SkillGuideTool({})
        r = guide.run(skill_name="")
        assert r.ok
        assert "暂无" in r.content


# ── RunSkillTool 测试 ──────────────────────────────────────


class TestRunSkill:
    def test_run_skill_returns_instruction(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        runner = RunSkillTool(skills)
        r = runner.run(skill_name="code-review", params={"target": "feature/auth"})
        assert r.ok
        assert "已激活" in r.content
        assert "代码审查流程" in r.content

    def test_run_nonexistent_skill(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        runner = RunSkillTool(skills)
        r = runner.run(skill_name="nonexistent")
        assert not r.ok
        assert "不存在" in r.error

    def test_run_skill_with_allowed_tools(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        runner = RunSkillTool(skills)
        r = runner.run(skill_name="code-review", params={"target": "main"})
        assert r.ok
        # allowed-tools 应在返回内容中
        assert "run_tool(shell.git:*)" in r.content

    def test_run_skill_no_params(self, skills_dir):
        loader = SkillsPluginLoader()
        config = SkillsConfig(enabled=True, dirs=[skills_dir])
        skills = loader.load_all(config)
        runner = RunSkillTool(skills)
        r = runner.run(skill_name="test-gen")
        assert r.ok
        assert "测试生成" in r.content


# ── MCP 配置测试 ──────────────────────────────────────────


class TestMcpConfig:
    def test_default_config(self):
        config = McpConfig()
        assert config.servers == {}
        assert config.defaults.connect_timeout == 30
        assert config.defaults.call_timeout == 60
        assert config.defaults.max_tools_per_server == 30

    def test_server_config(self):
        config = McpConfig(
            servers={
                "github": McpServerConfig(
                    transport="stdio",
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-github"],
                    env={"GITHUB_TOKEN": "xxx"},
                    enabled=True,
                    group="mcp_github",
                ),
                "postgres": McpServerConfig(
                    transport="sse",
                    url="http://localhost:5432",
                    enabled=False,
                ),
            }
        )
        assert config.servers["github"].command == "npx"
        assert config.servers["postgres"].enabled is False

    def test_config_from_yaml(self):
        yaml_data = """
servers:
  github:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"
    enabled: true
    group: mcp_github
    tool_filter:
      exclude: ["delete_repo"]
defaults:
  connect_timeout: 15
  call_timeout: 30
"""
        data = yaml.safe_load(yaml_data)
        config = McpConfig(**data)
        assert config.servers["github"].command == "npx"
        assert config.servers["github"].env["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
        assert config.defaults.connect_timeout == 15
        assert "delete_repo" in config.servers["github"].tool_filter.get("exclude", [])


# ── McpClientManager 测试 ──────────────────────────────────


class TestMcpClientManager:
    def test_connect_all_disabled(self):
        """禁用的服务器不连接。"""
        config = McpConfig(
            servers={
                "disabled": McpServerConfig(
                    transport="stdio",
                    command="echo",
                    enabled=False,
                ),
            }
        )
        manager = McpClientManager()
        clients = manager.connect_all(config)
        assert len(clients) == 0

    def test_connect_all_connection_failure(self):
        """连接失败不阻塞。"""
        config = McpConfig(
            servers={
                "bad": McpServerConfig(
                    transport="stdio",
                    command="nonexistent-command-xyz",
                    enabled=True,
                ),
            }
        )
        manager = McpClientManager()
        clients = manager.connect_all(config)
        assert len(clients) == 0  # 连接失败被跳过

    def test_disconnect_all(self):
        manager = McpClientManager()
        manager.disconnect_all()  # 无客户端也不报错
        assert manager.connected_groups == []


# ── McpPluginLoader 测试 ──────────────────────────────────


class TestMcpPluginLoader:
    def test_load_with_no_servers(self):
        config = McpConfig()
        loader = McpPluginLoader(config)
        tree = OperationTree()
        clients = loader.load_all(tree)
        assert clients == {}

    def test_load_disabled_server(self):
        config = McpConfig(
            servers={
                "disabled": McpServerConfig(
                    transport="stdio",
                    command="echo",
                    enabled=False,
                ),
            }
        )
        loader = McpPluginLoader(config)
        tree = OperationTree()
        clients = loader.load_all(tree)
        assert clients == {}


# ── SkillsConfig 测试 ─────────────────────────────────────


class TestSkillsConfig:
    def test_default_config(self):
        config = SkillsConfig()
        assert config.enabled is True
        assert "cagent/skills" in config.dirs
        assert config.max_recursion == 3

    def test_custom_dirs(self):
        config = SkillsConfig(
            enabled=True,
            dirs=["cagent/skills", ".data/skills", "~/my-skills"],
        )
        assert len(config.dirs) == 3

    def test_disabled(self):
        config = SkillsConfig(enabled=False)
        assert config.enabled is False
