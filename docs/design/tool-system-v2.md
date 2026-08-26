# 通用按需披露工具体系设计文档（v2）

> 版本：v2（2026-08-25）
> 模块位置：`cagent/tools/`、`cagent/plugins/`
> 相关配置：`config/agent.yaml` 的 `tools:` 段 + 各插件包内的 `tool.yaml`
> 前置文档：`docs/design/tool-system.md`（v1 基础设计）

## 1. 设计目标

1. **多层按需披露**：工具操作体系可分层路由，简单操作两层到底（查格式→执行），复杂操作（如 shell 子命令、浏览器操作）多层逐级展开，避免 system prompt 一次性注入全部格式说明。
2. **配置化操作树**：整个工具目录树（组→操作→子操作→参数）用 YAML 定义，不改代码就能增减操作节点。
3. **区分执行方式**：叶子节点声明执行方式——框架代码执行（框架做参数校验/路径沙箱/结构化输出）或 shell 驱动（模型生成完整命令，框架只负责安全执行）。
4. **插件分类**：插件包声明自身类型（tools/mcp/browser/connector），不同类型有不同加载器；本设计聚焦 tools 类型，其他类型预留扩展位。
5. **自定义业务工具**：用户按插件规范编写自己的 `tool.yaml` + `executor.py`，注册自定义操作，与内置工具统一管理。

## 2. 两种执行方式

### 2.1 核心区分

| 执行方式 | 标识 | 代表工具 | 模型传什么 | 框架做什么 |
|---|---|---|---|---|
| **框架代码执行** | `executor` | apply_patch, read_file, list_dir | 结构化参数（patch 文本、path、start_line） | 参数校验、路径沙箱、补丁解析、结构化输出 |
| **shell 驱动** | `shell` | rg, fd, git, cat, 任意命令 | 完整命令字符串（`rg --line-number "def main" src/`） | 超时控制、路径沙箱（cwd 限制）、输出截断、黑名单拦截 |

### 2.2 设计考量

**为什么 search_content 不应该是框架代码工具？**

`rg` / `fd` 等工具功能强大且参数丰富（`--type`, `--glob`, `--ignore`, `--hidden`, `--multiline` 等），用框架代码封装参数 schema 会丢失灵活性——模型知道怎么写 `rg` 命令，让它直接写比让它填结构化参数更自然。

但 shell 驱动需要框架做安全执行（超时、cwd 限制、截断），而非"裸执行"。所以 shell 体系的渐进式披露不是披露"参数 schema"，而是披露"命令用法 + 关键参数提示"——帮助模型回忆命令语法，但最终让模型生成完整命令。

### 2.3 两种执行方式在操作树中的声明

```yaml
# 框架代码执行
- name: apply_patch
  summary: "应用补丁修改文件（增删改移）"
  execute:                          # 执行声明
    mode: executor                  # 框架代码执行
    handler: apply_patch            # executor.py 中的函数名
  params:
    patch:
      type: string
      required: true
      description: "补丁文本"

# shell 驱动
- name: rg
  summary: "按正则搜索文件内容（ripgrep）"
  execute:
    mode: shell                     # shell 驱动
    command_template: null          # null = 模型生成完整命令
    # command_template 非空时为模板，模型只需填参数
  params:
    command:
      type: string
      required: true
      description: "完整的 rg 命令（如 rg --line-number -e 'pattern' --include='*.py' src/）"

# shell 模板模式（可选，用于简单命令）
- name: git_commit
  summary: "Git 提交"
  execute:
    mode: shell
    command_template: "git commit -m '{message}'"
    # 模型传 message 参数，框架拼成完整命令
  params:
    message:
      type: string
      required: true
      description: "提交消息"
```

**shell 模板的两种模式**：
- `command_template: null` — 模型生成完整命令，框架只执行（适合 rg/fd/cat 等通用命令）
- `command_template: "git commit -m '{message}'` — 模型填参数，框架拼命令（适合固定格式的业务命令）

## 3. 插件包分类

### 3.1 插件类型

| 类型 | 标识 | 结构 | 加载器 | 说明 |
|---|---|---|---|---|
| **tools** | `tools` | tool.yaml + executor.py | `ToolsPluginLoader` | 工具插件，含操作树和执行函数 |
| **mcp** | `mcp` | mcp.yaml | `McpPluginLoader` | MCP 服务器连接（二期） |
| **browser** | `browser` | tool.yaml + driver.py | `BrowserPluginLoader` | 浏览器驱动插件（二期） |
| **connector** | `connector` | connector.yaml | `ConnectorPluginLoader` | 外部 API 连接器（二期） |

### 3.2 插件包结构

```
cagent/plugins/
  ├── __init__.py
  │
  ├── filesystem/               # tools 插件
  │   ├── plugin.yaml           # 插件元信息（声明类型）
  │   ├── tool.yaml             # 操作树定义
  │   ├── executor.py           # 执行函数
  │   └── __init__.py
  │
  ├── shell/                    # tools 插件（纯 shell 驱动）
  │   ├── plugin.yaml
  │   ├── tool.yaml             # 操作树（叶子节点都是 mode: shell）
  │   ├── executor.py           # 可选（shell 驱动不需要执行函数，但路径沙箱/截断等逻辑在这里）
  │   └── __init__.py
  │
  ├── browser/                  # browser 插件（二期）
  │   ├── plugin.yaml           # type: browser
  │   ├── tool.yaml
  │   ├── driver.py             # Playwright/Selenium 驱动
  │   └── __init__.py
  │
  └── custom/                   # 用户自定义 tools 插件
      ├── plugin.yaml
      ├── tool.yaml
      ├── executor.py
      └── __init__.py
```

### 3.3 plugin.yaml 格式

```yaml
# 插件元信息
plugin: filesystem              # 插件名（唯一标识）
type: tools                     # 插件类型
version: "1.0"
group: filesystem               # 对应 ToolRegistry 的 group
summary: "文件操作工具组"
detail: "提供文件读写、补丁修改等操作"
author: "cagent"                # 作者
dependencies:                   # 依赖的其他插件或包
  - cagent.plugins._guard       # 框架内依赖
  # - playwright                 # 外部 pip 包（二期自动安装）
```

### 3.4 加载流程

```python
class PluginManager:
    """插件管理器：根据类型分发给对应加载器。"""

    LOADERS = {
        "tools": ToolsPluginLoader,
        # "mcp": McpPluginLoader,         # 二期
        # "browser": BrowserPluginLoader, # 二期
        # "connector": ConnectorPluginLoader, # 二期
    }

    def load_all(self, plugins_dir: str, config: dict) -> PluginLoadResult:
        result = PluginLoadResult()
        for plugin_dir in self._scan_plugin_dirs(plugins_dir):
            meta = self._read_plugin_meta(plugin_dir)
            plugin_type = meta.get("type", "tools")
            loader_cls = self.LOADERS.get(plugin_type)
            if loader_cls is None:
                continue  # 未知类型跳过
            plugin_config = config.get("plugins", {}).get(meta["plugin"], {})
            if not plugin_config.get("enabled", True):
                continue
            loader = loader_cls(plugin_dir, meta, plugin_config)
            loaded = loader.load()
            result.merge(loaded)
        return result
```

### 3.5 tools 类型加载器

```python
class ToolsPluginLoader:
    """tools 类型插件加载器。"""

    def __init__(self, plugin_dir: str, meta: dict, config: dict):
        self.dir = plugin_dir
        self.meta = meta
        self.config = config

    def load(self) -> PluginLoadResult:
        result = PluginLoadResult()
        yaml_path = os.path.join(self.dir, "tool.yaml")
        tree = OperationTree()
        tree.load_plugin(yaml_path)
        result.operation_tree = tree
        # 加载 executor（框架代码执行模式需要）
        executor_path = os.path.join(self.dir, "executor.py")
        if os.path.exists(executor_path):
            result.executors[self.meta["plugin"]] = PluginExecutor(
                self.meta["plugin"], executor_path, self.config.get("config", {})
            )
        return result
```

## 4. 核心概念

### 4.1 操作树（Operation Tree）

```
根: ToolGroup（如 "filesystem", "shell"）
 ├─ Operation: "apply_patch"        ← 叶子：execute.mode=executor
 ├─ Operation: "read_file"          ← 叶子：execute.mode=executor
 ├─ OperationGroup: "search"       ← 中间节点：只路由
 │   ├─ Operation: "rg"            ← 叶子：execute.mode=shell
 │   └─ Operation: "fd"            ← 叶子：execute.mode=shell
 └─ OperationGroup: "git"          ← 中间节点
     ├─ Operation: "commit"       ← 叶子：execute.mode=shell（模板）
     └─ Operation: "push"          ← 叶子：execute.mode=shell（模板）
```

每个节点有：
- `name`：节点标识
- `summary`：一句话简介（给上层披露，≤120 字符）
- `detail`：完整说明（给叶子层披露，含格式/参数/示例）
- `children`：子节点（中间节点才有）
- `params`：参数 schema（叶子节点才有）
- `execute`：执行声明（叶子节点才有）
  - `mode`: `executor` | `shell`
  - `handler`: executor 模式的函数名
  - `command_template`: shell 模式的命令模板（null = 模型生成完整命令）

### 4.2 披露层级

| 层级 | 模型行为 | 示例 |
|---|---|---|
| L0：工具目录 | 模型看到 `tool_guide` 的简介 | "查询工具操作的格式说明" |
| L1：组级披露 | `tool_guide(path="shell")` | "shell 下有：run_command、rg、fd、git/（子组）" |
| L2：操作级披露 | `tool_guide(path="shell.rg")` | 返回 rg 的用法 + 关键参数提示 + 示例命令 |
| L3：子操作级披露 | `tool_guide(path="shell.git")` | "git 下有：commit、push" |
| L4：子操作格式 | `tool_guide(path="shell.git.commit")` | 返回 commit 的参数 + 模板 |
| 执行 | `run_tool(path="shell.rg", params={"command": "rg -e 'def main' src/"})` | 执行并返回结果 |

**自适应**：`tool_guide` 自动判断节点类型——中间节点返回子节点列表，叶子节点返回格式说明。

### 4.3 双工具模式

| 工具 | 职责 | 参数 |
|---|---|---|
| `tool_guide` | 查询操作树任意节点的说明 | `path: str` |
| `run_tool` | 执行叶子操作 | `path: str`, `params: object` |

## 5. 配置化操作树

### 5.1 tool.yaml — filesystem 插件

```yaml
# tool.yaml（filesystem 插件，框架代码执行）
tree:
  - name: apply_patch
    summary: "应用补丁修改文件（增删改移）"
    detail: |
      应用补丁修改文件。支持四种操作：Add（新增）、Update（修改）、Delete（删除）、Move（移动）。
      补丁格式：
      *** Begin Patch
      *** Add File: <路径>
      +<内容行>
      *** End Patch
      *** Update File: <路径>
      <上下文行>
      -<删除行>
      +<新增行>
      *** End Patch
    execute:
      mode: executor
      handler: apply_patch
    params:
      patch:
        type: string
        required: true
        description: "补丁文本"

  - name: read_file
    summary: "读取文件内容（带行号）"
    detail: |
      读取文件内容，输出带行号格式。
      参数：path（文件路径）、start_line（起始行，默认0）、end_line（结束行，默认2000）
      示例：read_file(path="src/agent.py", start_line=10, end_line=30)
    execute:
      mode: executor
      handler: read_file
    params:
      path:
        type: string
        required: true
        description: "文件相对路径"
      start_line:
        type: integer
        default: 0
        description: "起始行号（0-based）"
      end_line:
        type: integer
        default: 2000
        description: "结束行号"

  - name: list_dir
    summary: "列出目录树"
    detail: |
      列出目录结构，树状输出。
      参数：path（目录路径，默认.）、max_depth（最大深度，默认2）、include_hidden（是否含隐藏文件，默认false）
    execute:
      mode: executor
      handler: list_dir
    params:
      path:
        type: string
        default: "."
        description: "目录路径"
      max_depth:
        type: integer
        default: 2
        description: "最大递归深度"
      include_hidden:
        type: boolean
        default: false
        description: "是否包含隐藏文件"
```

### 5.2 tool.yaml — shell 插件

```yaml
# tool.yaml（shell 插件，shell 驱动 + 模板）
tree:
  - name: run_command
    summary: "执行任意 shell 命令"
    detail: |
      执行 shell 命令，返回 stdout/stderr/exit_code。
      命令在工作目录下执行，受超时和黑名单限制。
      参数：command（完整命令）、timeout（超时秒，默认30）、cwd（工作目录，默认项目根）
      示例：run_command(command="npm test", timeout=60)
    execute:
      mode: shell
      command_template: null       # 模型生成完整命令
    params:
      command:
        type: string
        required: true
        description: "完整的 shell 命令"
      timeout:
        type: integer
        default: 30
        description: "超时秒数"
      cwd:
        type: string
        default: null
        description: "工作目录（默认项目根）"

  - name: rg
    summary: "按正则搜索文件内容（ripgrep）"
    detail: |
      使用 ripgrep 搜索文件内容。模型生成完整的 rg 命令。
      常用参数：--line-number（显示行号）、-e（正则）、-g（文件名过滤）、--type（文件类型）
      示例：rg --line-number -e "def main" -g "*.py" src/
      注意：命令在工作目录下执行，输出自动截断至 max_results 条。
    execute:
      mode: shell
      command_template: null
    params:
      command:
        type: string
        required: true
        description: "完整的 rg 命令"

  - name: fd
    summary: "按文件名模式查找文件（fd）"
    detail: |
      使用 fd 查找文件。模型生成完整的 fd 命令。
      常用参数：--type（f/d/l）、--extension（扩展名过滤）
      示例：fd --type f --extension py src/
    execute:
      mode: shell
      command_template: null
    params:
      command:
        type: string
        required: true
        description: "完整的 fd 命令"

  - name: git
    summary: "Git 操作子组"
    children:
      - name: commit
        summary: "提交代码"
        detail: |
          执行 git commit。
          参数：message（提交消息）、add_all（是否先 git add .，默认true）
        execute:
          mode: shell
          command_template: "git {add_all_opt} commit -m '{message}'"
          # 模板变量：{message}, {add_all_opt}
          # {add_all_opt} 由框架根据 add_all 参数生成 "add . &&" 或 ""
        params:
          message:
            type: string
            required: true
            description: "提交消息"
          add_all:
            type: boolean
            default: true
            description: "是否先 git add ."

      - name: push
        summary: "推送到远程"
        detail: |
          执行 git push。
          参数：remote（远程名，默认origin）、branch（分支名，默认当前分支）
        execute:
          mode: shell
          command_template: "git push {remote} {branch}"
        params:
          remote:
            type: string
            default: "origin"
            description: "远程名称"
          branch:
            type: string
            default: null
            description: "分支名（默认当前分支）"
```

### 5.3 tool.yaml — custom 业务插件

```yaml
# tool.yaml（自定义业务插件，框架代码执行）
tree:
  - name: query_order
    summary: "查询订单信息"
    detail: |
      调用内部 API 查询订单。
      参数：order_id（订单号）
    execute:
      mode: executor
      handler: query_order
    params:
      order_id:
        type: string
        required: true
        description: "订单号"

  - name: deploy
    summary: "部署子组"
    children:
      - name: staging
        summary: "部署到预发环境"
        detail: "执行部署脚本，推送指定版本到预发环境。"
        execute:
          mode: executor
          handler: deploy_staging
        params:
          version:
            type: string
            required: true
            description: "版本号"

      - name: production
        summary: "部署到生产环境"
        detail: "执行部署脚本，推送指定版本到生产环境。需要 confirm=true 确认。"
        execute:
          mode: executor
          handler: deploy_production
        params:
          version:
            type: string
            required: true
            description: "版本号"
          confirm:
            type: boolean
            required: true
            description: "必须确认为 true 才执行"
```

## 6. 插件规范

### 6.1 executor.py 规范（框架代码执行模式）

框架代码执行模式的插件需要提供 `executor.py`，导出与 `tool.yaml` 中 `handler` 同名的函数：

```python
# cagent/plugins/filesystem/executor.py
from cagent.tools.base import ToolResult
from cagent.tools.builtin._guard import PathGuard

# 插件配置（由 PluginExecutor 在加载时注入）
_config = {}
_guard = None

def configure(config: dict) -> None:
    """插件加载时调用，注入配置。"""
    global _config, _guard
    _config = config
    _guard = PathGuard(config.get("work_dir", "."))

def apply_patch(patch: str, **kwargs) -> ToolResult:
    """对应 tool.yaml 中 handler: apply_patch"""
    ...

def read_file(path: str, start_line: int = 0, end_line: int = 2000, **kwargs) -> ToolResult:
    """对应 tool.yaml 中 handler: read_file"""
    ...

def list_dir(path: str = ".", max_depth: int = 2, include_hidden: bool = False, **kwargs) -> ToolResult:
    """对应 tool.yaml 中 handler: list_dir"""
    ...
```

### 6.2 shell 驱动的执行逻辑

shell 驱动模式不需要 `executor.py` 中的处理函数，但框架的 `RunToolTool` 内部有统一的 shell 执行器：

```python
class ShellExecutor:
    """统一 shell 执行器：超时、路径沙箱、输出截断、黑名单。"""

    def __init__(self, config: dict):
        self.work_dir = config.get("work_dir", ".")
        self.default_timeout = config.get("timeout", 30)
        self.max_timeout = config.get("max_timeout", 300)
        self.max_output_chars = config.get("max_output_chars", 2000)
        self.blocked_patterns = config.get("blocked_patterns", [])
        self.blocked_env = config.get("blocked_env", [])

    def execute(self, command: str, timeout: int = None, cwd: str = None) -> ToolResult:
        """执行 shell 命令。"""
        # 1. 黑名单检查
        for pattern in self.blocked_patterns:
            if pattern in command:
                return ToolResult(ok=False, content="", error=f"命令被黑名单拦截: {pattern}")
        # 2. 超时限制
        timeout = min(timeout or self.default_timeout, self.max_timeout)
        # 3. 工作目录限制
        cwd = cwd or self.work_dir
        cwd = os.path.realpath(cwd)
        if not cwd.startswith(os.path.realpath(self.work_dir)):
            return ToolResult(ok=False, content="", error="工作目录越界")
        # 4. 环境变量过滤
        env = dict(os.environ)
        for key in self.blocked_env:
            env.pop(key, None)
        # 5. 执行
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd, env=env,
        )
        # 6. 截断
        stdout = self._truncate(result.stdout)
        stderr = self._truncate(result.stderr)
        content = f"[exit_code={result.returncode}]\nstdout:\n{stdout}"
        if stderr:
            content += f"\nstderr:\n{stderr}"
        return ToolResult(ok=result.returncode == 0, content=content)
```

### 6.3 shell 模板渲染

对于 `command_template` 非空的 shell 操作，框架按模板拼命令：

```python
def render_command(template: str, params: dict) -> str:
    """将参数填入命令模板。"""
    # 简单变量替换：{message} → params["message"]
    # 条件变量：{add_all_opt} → "add . &&" if params.get("add_all") else ""
    # 这是简化实现，可扩展为更复杂的模板引擎
    rendered = template
    for key, value in params.items():
        if key.endswith("_opt"):
            # 条件变量：布尔参数生成 shell 片段
            base_key = key[:-4]
            if params.get(base_key):
                rendered = rendered.replace(f"{{{key}}}", value)
            else:
                rendered = rendered.replace(f"{{{key}}}", "")
        elif value is not None:
            rendered = rendered.replace(f"{{{key}}}", str(value))
    return rendered
```

## 7. 操作树运行时

### 7.1 OperationNode — 树节点

```python
@dataclass
class OperationNode:
    """操作树节点。"""
    name: str
    path: str                       # 如 "shell.git.commit"
    summary: str
    detail: Optional[str]
    children: Dict[str, "OperationNode"]
    params: Dict[str, dict]
    execute: Optional[dict]         # {mode: "executor"|"shell", handler/template}

    @property
    def is_leaf(self) -> bool:
        return self.execute is not None

    @property
    def is_branch(self) -> bool:
        return bool(self.children)
```

### 7.2 OperationTree

```python
class OperationTree:
    """从 YAML 加载操作树，支持按路径查询。"""

    def __init__(self):
        self._roots: Dict[str, OperationNode] = {}

    def load_plugin(self, yaml_path: str) -> None:
        """从 tool.yaml 加载一个插件的操作树。"""
        ...

    def query(self, path: str) -> Optional[OperationNode]:
        """按路径查询节点（如 'shell.git.commit'）。"""
        ...

    def list_leaf_paths(self) -> List[str]:
        """列出所有叶子路径（用于 run_tool 的 description）。"""
        ...
```

### 7.3 ToolGuideTool + RunToolTool

```python
class ToolGuideTool(Tool):
    """查询操作树任意节点的说明。"""
    name = "tool_guide"
    description = "查询工具操作的格式说明..."
    group = "default"

    def run(self, path: str = "") -> ToolResult:
        node = self._tree.query(path)
        if node is None:
            return ToolResult(ok=False, content="", error=f"路径 '{path}' 不存在")
        if node.is_branch:
            # 中间节点：列出子节点
            lines = [f"「{node.name}」下有以下操作："]
            for name, child in node.children.items():
                lines.append(f"  - {name}: {child.summary}")
            lines.append(f"\n调用 tool_guide(path='{node.path}.子操作名') 查看具体格式。")
            return ToolResult(ok=True, content="\n".join(lines))
        else:
            # 叶子节点：返回完整格式
            return ToolResult(ok=True, content=node.detail)

class RunToolTool(Tool):
    """执行工具操作。"""
    name = "run_tool"
    description = "执行工具操作..."
    group = "default"

    def run(self, path: str, params: dict = None) -> ToolResult:
        node = self._tree.query(path)
        if node is None or not node.is_leaf:
            return ToolResult(ok=False, content="", error="...")
        # 参数校验
        params = self._validate_params(node, params or {})
        # 按 execute.mode 分发
        if node.execute["mode"] == "executor":
            return self._exec_executor(node, params)
        elif node.execute["mode"] == "shell":
            return self._exec_shell(node, params)
```

## 8. 与现有体系的关系

### 8.1 三种工具模式共存

| 模式 | 工具 | 披露 | 执行 |
|---|---|---|---|
| 直接执行（现有） | calculator, ask_user, remember | 无（schema 直接给参数） | tool.run(**args) |
| 按需披露-框架代码 | filesystem (apply_patch, read_file) | tool_guide → run_tool | executor.py 函数 |
| 按需披露-shell 驱动 | shell (rg, fd, git) | tool_guide → run_tool | ShellExecutor |

三种模式在同一 ToolRegistry 中共存，ReAct 引擎不需要改。

### 8.2 apply_patch 迁移

- `tool.yaml` 定义 apply_patch/read_file/list_dir 操作（mode=executor）
- `executor.py` 搬移现有补丁解析和执行逻辑
- `PatchUsageTool` 被 `ToolGuideTool` 替代
- `ApplyPatchTool` 被 `RunToolTool` 替代

### 8.3 search_content/search_files 变更

之前设计中的 `search_content`（框架代码封装 rg）改为 shell 驱动：
- 模型直接生成 `rg --line-number -e "pattern" src/` 完整命令
- 框架的 `ShellExecutor` 负责执行 + 超时 + 截断
- `tool_guide(path="shell.rg")` 披露的是 rg 的用法提示，不是参数 schema

## 9. 配置设计

### 9.1 agent.yaml 扩展

```yaml
tools:
  enabled_groups: [math, interaction, memory, filesystem, shell]
  dynamic: true
  max_per_step: 8

  # 插件加载配置
  plugins:
    filesystem:
      enabled: true
      config:
        work_dir: "."
        allow_overwrite: true
        allow_delete: false
        max_read_lines: 2000
        max_output_chars: 2000

    shell:
      enabled: true
      config:
        work_dir: "."
        timeout: 30
        max_timeout: 300
        max_output_chars: 2000
        blocked_patterns:
          - "rm -rf /"
          - "sudo"
        blocked_env:
          - "OPENAI_API_KEY"

    custom:
      enabled: true
      config:
        api_base_url: "https://internal-api.example.com"
        api_key_env: "INTERNAL_API_KEY"
```

## 10. 端到端工作流示例

### 10.1 文件修改（框架代码执行）

```
模型 → tool_guide(path="filesystem")
  ← "filesystem 下有：apply_patch（应用补丁）、read_file（读取文件）、list_dir"

模型 → tool_guide(path="filesystem.apply_patch")
  ← 完整补丁格式 + 示例

模型 → run_tool(path="filesystem.apply_patch", params={"patch": "*** Begin Patch..."})
  ← "已应用补丁：修改 src/core.py（2 处变更）"
```

### 10.2 代码搜索（shell 驱动）

```
模型 → tool_guide(path="shell")
  ← "shell 下有：run_command（执行命令）、rg（搜索内容）、fd（查找文件）、git/（子组）"

模型 → tool_guide(path="shell.rg")
  ← rg 用法提示 + 常用参数 + 示例命令

模型 → run_tool(path="shell.rg", params={"command": "rg --line-number -e 'def main' -g '*.py' src/"})
  ← "src/core/agent.py:15:def main():\n..."
```

### 10.3 Git 提交（shell 模板）

```
模型 → tool_guide(path="shell.git")
  ← "git 下有：commit（提交）、push（推送）"

模型 → tool_guide(path="shell.git.commit")
  ← 参数：message/add_all + 模板说明

模型 → run_tool(path="shell.git.commit", params={"message": "fix: 修复登录bug", "add_all": true})
  ← 框架拼命令 "git add . && git commit -m 'fix: 修复登录bug'" → 执行
  ← "已提交：[main abc123] fix: 修复登录bug"
```

## 11. 目录结构

```
cagent/
  ├── cagent/
  │   ├── tools/
  │   │   ├── base.py            # Tool 基类（不变）
  │   │   ├── registry.py        # ToolRegistry（不变）
  │   │   ├── builtin/           # 内置直接执行工具
  │   │   │   ├── calculator.py
  │   │   │   ├── feedback.py
  │   │   │   ├── remember.py
  │   │   │   └── _guard.py
  │   │   └── __init__.py
  │   ├── plugins/              # 插件目录
  │   │   ├── __init__.py
  │   │   ├── manager.py        # PluginManager + 加载器
  │   │   ├── guide.py          # OperationTree + ToolGuideTool + RunToolTool + ShellExecutor
  │   │   ├── filesystem/       # tools 插件（框架代码执行）
  │   │   │   ├── plugin.yaml
  │   │   │   ├── tool.yaml
  │   │   │   ├── executor.py
  │   │   │   └── __init__.py
  │   │   ├── shell/            # tools 插件（shell 驱动）
  │   │   │   ├── plugin.yaml
  │   │   │   ├── tool.yaml
  │   │   │   └── __init__.py
  │   │   └── custom/           # 用户自定义 tools 插件
  │   │       ├── plugin.yaml
  │   │       ├── tool.yaml
  │   │       ├── executor.py
  │   │       └── __init__.py
  │   └── ...
  ├── config/
  │   └── agent.yaml
  └── tests/
      ├── test_apply_patch.py
      ├── test_tool_guide.py    # 新增
      └── ...
```

## 12. 迁移计划

### 阶段 1：基础设施

- [ ] `OperationNode` / `OperationTree` 数据结构
- [ ] `ToolGuideTool` / `RunToolTool` 通用披露+执行工具
- [ ] `ShellExecutor` 统一 shell 执行器
- [ ] `PluginManager` + `ToolsPluginLoader`
- [ ] `PluginExecutor` 执行函数加载

### 阶段 2：apply_patch 迁移

- [ ] 创建 `cagent/plugins/filesystem/` 插件包
- [ ] `tool.yaml` 定义 apply_patch + read_file + list_dir（mode=executor）
- [ ] `executor.py` 搬移补丁解析/读取/列目录逻辑
- [ ] 删除旧的 PatchUsageTool / ApplyPatchTool

### 阶段 3：shell 插件

- [ ] `cagent/plugins/shell/` 插件包
- [ ] `tool.yaml` 定义 run_command + rg + fd + git 子组（mode=shell）
- [ ] `ShellExecutor` 实现超时/路径沙箱/截断/黑名单

### 阶段 4：browser 插件（二期）

- [ ] 插件类型扩展 `type: browser`
- [ ] Playwright/Selenium 集成

### 阶段 5：自定义业务插件

- [ ] 插件开发指南
- [ ] custom 插件模板

## 13. 设计取舍

| 项 | 现状 | 演进方向 |
|---|---|---|
| shell 命令生成 | 模型生成完整命令（command_template=null） | 可加模板模式辅助简单命令 |
| 插件类型 | tools 类型 | 预留 mcp/browser/connector 类型 |
| 参数类型 | YAML 声明 + 运行时校验 | 可生成 JSON Schema 给 function calling |
| 插件隔离 | 同进程执行 | 子进程沙箱（危险操作） |
| 操作权限 | 配置 enabled/disabled | 细粒度 RBAC |
| 模板引擎 | 简单变量替换 | 可扩展为 Jinja2 |
