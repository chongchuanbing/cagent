"""配置 schema：模型与提示词的可序列化定义。"""
from typing import Any, Dict, List, Literal, Optional

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


class PathSpaceConfig(BaseModel):
    """路径空间配置：PathSpace 的唯一事实来源（详见 docs/path-management-design.md）。

    所有目录相对 `base_dir` 解析；留 None 表示取 PathSpace 内置默认
    （work_dir → base_dir，data_dir → ".data"）。
    - base_dir：端上注入的基准目录，留空则用调用方传入的当前工作目录；
    - work_dir：工具操作沙箱根（模型看到的是 workspace://）；
    - data_dir：框架私有数据存储（默认对模型隐藏）；
    - allow_paths / allow_symlink_targets：受信外挂区，用于合法的越界访问。
    """

    base_dir: Optional[str] = None
    work_dir: Optional[str] = None
    data_dir: str = ".data"
    plugins_dir: Optional[str] = None
    config_dir: Optional[str] = None
    # 空间根目录（如项目代码目录）：null = 无空间模式（所有生成文件落会话目录）；
    # 指定后 workspace:// 挂空间根，结构化工具写入需显式 scheme 并登记审计
    space_dir: Optional[str] = None
    allow_paths: List[str] = Field(default_factory=list)
    allow_symlink_targets: List[str] = Field(default_factory=list)
    expose_data_to_llm: bool = False


class SandboxConfig(BaseModel):
    """系统级沙箱配置（详见 docs/design/sandbox-design.md）。

    - mode：off = LocalBackend 裸执行（校验层仍在）；
      auto = 按能力探测降级（bwrap → seatbelt → user → local，落到 local 记警告）；
      strict = 探测失败则拒绝执行 shell（安全优先场景）；
    - network：deny = shell 默认禁网（内置联网工具走主进程 httpx，不受影响）；
    - extra_readable：额外只读前缀（罕见场景，一般用 paths.allow_paths 声明）；
    - user：UserBackend 专用低权限用户（需预先配置 sudoers 免密白名单）。
    """

    mode: Literal["off", "auto", "strict"] = "off"
    network: Literal["deny", "allow"] = "deny"
    extra_readable: List[str] = Field(default_factory=list)
    user: Optional[str] = None


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
    # 读取语义阈值（针对 shell_exec 识别到的「读取类」命令，如 sed -n / head / grep -n）
    # 与 observation_max_chars 的区别：observation_max_chars 是兜底；read_* 是读取视图专用，
    # 行号化 + 头部契约 + 续读指令，按行（而非字符）截断，截断后模型仍能识别与续读。
    read_max_lines: int = 2000          # 读取类输出窗口（行）：单次最多返回的行数
    read_line_max_chars: int = 2000     # 读取类单行字符上限（防 JSON 单行撑爆）
    read_max_chars: int = 30000         # 读取类总渲染字符上限（防 2000 行×80 字符撑爆上下文）
    block_cat: bool = True              # shell.run_command 禁止 cat 整读（无法指定行范围）
    # 长期记忆（分代晋升）
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # 工具系统（分组 / 动态选择 / 插件）
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # MCP 服务器（多服务器配置，端上动态生效）
    mcp: McpConfig = Field(default_factory=McpConfig)
    # 技能系统（多目录动态加载，Agent Skills 开放标准）
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # 路径空间（workspace:// / data:// / skills:// 等挂载点的唯一事实来源）
    paths: PathSpaceConfig = Field(default_factory=PathSpaceConfig)
    # 系统级沙箱（bwrap / seatbelt / 低权限用户，shell 执行的第二道防线）
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
