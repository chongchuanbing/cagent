"""配置 schema：模型与提示词的可序列化定义。"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """模型连接配置，字段对齐 llm.LLMConfig。"""

    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 2048


class TagCategoryConfig(BaseModel):
    """显式标签分类：枚举合法取值，required 表示提取时必须给出取值。"""

    values: List[str] = Field(default_factory=list)
    required: bool = False


class MemoryTagsConfig(BaseModel):
    """标签生成方式配置。

    mode：
    - implicit：LLM 按提示词自由生成 key:value 标签（提示词可配）；
    - explicit：标签分类与取值在 schema 中枚举，LLM 只能从中选择；
    - hybrid：schema 固定分类 + 允许自由扩展额外标签；
    - off：不打标签。
    """

    mode: str = "implicit"
    categories: Dict[str, TagCategoryConfig] = Field(default_factory=dict)
    implicit_max_tags: int = 5


class MemoryConfig(BaseModel):
    """长期记忆（分代晋升）配置。"""

    enabled: bool = True
    namespace: str = "default"
    tenuring_threshold: int = 3        # 跨 N 个不同会话出现后晋升（JDK MaxTenuringThreshold）
    candidate_ttl_days: int = 14       # 候选长期未复发则淘汰；0 = 永不过期
    recall_k: int = 5                  # run 开始召回条数；0 = 关闭召回
    recall_candidates: bool = False    # 召回时是否包含未晋升候选（默认只召回 tenured）
    match_threshold: float = 0.6       # 去重合并的关键词重叠（Jaccard）阈值
    tags: MemoryTagsConfig = Field(default_factory=MemoryTagsConfig)


class PluginConfig(BaseModel):
    """单个插件的启用与配置。"""

    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    """工具系统配置：分组裁剪 + 动态选择 + 插件加载。

    - enabled_groups：None 表示启用全部分组；指定列表时 default 组始终启用；
    - dynamic：开启后每个 step 按「步骤描述 + 总目标」检索 Top-K 工具再注入，
      避免 tool 数量增长导致 token 膨胀与选择准确率下降；
    - max_per_step：dynamic 开启时注入的工具数上限。
    - plugins：插件级配置，key 为插件名，value 为 {enabled, config}。
    """

    enabled_groups: Optional[List[str]] = None
    dynamic: bool = False
    max_per_step: int = 8
    plugins: Dict[str, PluginConfig] = Field(default_factory=dict)


# ── MCP 配置 ────────────────────────────────────────────────


class McpServerConfig(BaseModel):
    """单个 MCP 服务器配置。"""

    transport: str = "stdio"              # stdio | sse
    command: Optional[str] = None         # stdio: 命令
    args: List[str] = Field(default_factory=list)  # stdio: 参数
    env: Dict[str, str] = Field(default_factory=dict)  # 环境变量
    url: Optional[str] = None              # sse: 端点 URL
    enabled: bool = True                  # 是否启用
    group: Optional[str] = None            # 操作树组名（默认 mcp_<key>）
    tool_filter: Dict[str, Any] = Field(default_factory=dict)  # include/exclude/rename


class McpDefaults(BaseModel):
    """MCP 全局默认配置。"""

    connect_timeout: int = 30
    call_timeout: int = 60
    max_tools_per_server: int = 30
    auto_detail: bool = True


class McpConfig(BaseModel):
    """MCP 配置段：支持多个服务器，端上动态生效。"""

    servers: Dict[str, McpServerConfig] = Field(default_factory=dict)
    defaults: McpDefaults = Field(default_factory=McpDefaults)


# ── Skills 配置 ─────────────────────────────────────────────


class SkillsConfig(BaseModel):
    """技能系统配置：多目录动态加载，对齐 Agent Skills 开放标准。"""

    enabled: bool = True
    dirs: List[str] = Field(default_factory=lambda: ["cagent/skills"])
    max_recursion: int = 3


class AgentConfig(BaseModel):
    """端上配置文件（agent.yaml）对应的顶层结构。"""

    model: ModelConfig = Field(default_factory=ModelConfig)
    # 提示词覆盖：key -> 模板字符串（支持 {step}/{tools} 等占位符）
    prompts: Dict[str, str] = Field(default_factory=dict)
    # 迭代次数控制
    max_steps: int = 20                 # 计划层：最多执行的 Step 数
    react_max_iterations: int = 5       # 单步 ReAct 内最多推理—行动轮数
    # 跨 step 上下文窗口（history）
    history_window: int = 10            # 注入后续步骤的最近 step 条目数；0 = 不限制
    observation_max_chars: int = 500    # 单条工具观察注入上下文时的截断长度；0 = 不截断
    history_summarize: bool = True      # 超窗条目是否用 LLM 滚动摘要（无 LLM 时退化为文本压缩）
    # 长期记忆（分代晋升）
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # 工具系统（分组 / 动态选择 / 插件）
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # MCP 服务器（多服务器配置，端上动态生效）
    mcp: McpConfig = Field(default_factory=McpConfig)
    # 技能系统（多目录动态加载，Agent Skills 开放标准）
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
