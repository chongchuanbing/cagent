"""统一 shell 执行器：超时、路径沙箱、输出截断、黑名单。"""
import os
import re
import subprocess
from typing import Dict, List, Optional

from ..runtime.paths import SYSTEM_PREFIXES, URI_RE, PathSpace
from ..runtime.sandbox import ExecutionBackend, LocalBackend, run_argv

# scheme 改写记录前缀（让 trace 可见「框架把 workspace:// 换成了物理路径」）
_SCHEME_REWRITE_PREFIX = "[scheme 自动改写] "

# shell 重定向目标：> / >> 后的绝对路径 token
REDIRECT_TARGET_RE = re.compile(r">{1,2}\s*((?:/[^/\s|;&><'\"]+)+)")

# 提取 token 内的 scheme 路径（不带 URI_RE 的 ^ 锚定，因为它只匹配整串）
# 形如 workspace://xxx 必须以字母起头，后续 [A-Za-z0-9+.\-] 允许 scheme 中的 + . -
# 路径部分到空白 / shell 元字符 / 引号 / 反引号为止
_TOKEN_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://[^\s|;&><'\"`]*")

# cd 目标：分段开头的 cd <path>（会影响后续分段的相对路径基准）
CD_TARGET_RE = re.compile(r"(?:^|&&|;|\|)\s*cd\s+(?:--\S+\s+)?([^\s;&|]+)")

# 相对重定向目标（不含绝对路径）
REL_REDIRECT_RE = re.compile(r">{1,2}\s*([^\s|;&><][^\s|;&><]*)")

# 常见输出参数中的相对路径目标（--out / --output / --output-file）
REL_OUTPUT_PARAM_RE = re.compile(
    r"--out(?:put)?(?:-file)?\s*=?\s*([^\s;&|][^\s;&|]*)"
)

# ── 读取语义识别 ──────────────────────────────────────────────
# 视图类命令：模型指定了行范围（如 sed -n 'A,Bp' / head -n N / tail -n N / awk NR / nl）
_READ_VIEW_COMMANDS = frozenset({"sed", "head", "tail", "awk", "nl"})
# 检索类命令：自带行号（grep -n / rg / bat），不再加行号化
_READ_GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "ripgrep", "bat"})
# 整读禁止命令（无法指定行范围 → 必然被截断 → 静默失败风险）
_BLOCKED_COMMANDS = frozenset({"cat"})

# sed 行范围提取：sed -n 'A,Bp' | sed -n 'A,B'p | sed -n "A,$p"
_SED_RANGE_RE = re.compile(
    r"""-n\s+['"]?(\d+)\s*,\s*(\d+|\$)p?['"]?"""
)
# head -n N | head -N
_HEAD_N_RE = re.compile(r"(?:-n\s+|-)(\d+)")
# tail -n N | tail -N
_TAIL_N_RE = re.compile(r"(?:-n\s+|-)(\d+)")
# awk NR>=A & NR<=B / NR==A / NR>A
_AWK_NR_GTE_RE = re.compile(r"NR\s*>=\s*(\d+)")
_AWK_NR_LTE_RE = re.compile(r"NR\s*<=\s*(\d+)")

# 解析命令时跳过短 flag 后续值的 flag 集合
_FLAGS_TAKING_VALUE = frozenset({
    "-n", "-A", "-B", "-C", "-e", "-f", "-i", "-v", "-r", "-E",
    "--type", "--glob", "--extension", "--max-depth",
    "--since", "--author", "--line-number", "-c", "--regexp",
})


def _extract_command_name(command: str) -> Optional[str]:
    """从命令字符串提取首个命令名（处理管道 / 链式 / 变量展开前边界）。

    用于读取语义识别；不做安全校验，安全校验仍由 L4 path_sandbox 完成。
    """
    s = command.strip()
    if not s:
        return None
    # 引号感知分割链式：先把引号包裹的内容换成占位符，避免 awk/sed 脚本里的
    # `&&` / `|` 被误当 shell 分隔符切掉
    s_hidden, placeholders = _hide_quoted(s)
    for sep in ("|", "&&", "||", ";"):
        if sep in s_hidden:
            s_hidden = s_hidden.split(sep, 1)[0]
            break
    s = _restore_quoted(s_hidden.strip(), placeholders)
    if not s:
        return None
    s = s.lstrip("(")
    first = s.split()[0]
    first = os.path.basename(first)
    first = re.sub(r"\.(sh|py|pl|rb)$", "", first)
    return first or None


def _strip_quotes(s: str) -> str:
    """去掉字符串两端匹配的引号。"""
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def _hide_quoted(s: str) -> "tuple[str, dict]":
    """把引号包裹的字符串整体换成占位符，返回 (替换后字符串, {占位符: 原字符串})。

    链式运算符（| / && / || / ;）只可能在引号外出现，避免 awk 脚本里的 `&&` /
    sed 脚本里的 `|` 被误当分隔符切割。
    """
    placeholders: Dict[str, str] = {}
    out: List[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < len(s) and s[j] != quote:
                j += 1
            placeholder = f"__QUOTED_{len(placeholders)}__"
            placeholders[placeholder] = s[i : j + 1]
            out.append(placeholder)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), placeholders


def _restore_quoted(s: str, placeholders: Dict[str, str]) -> str:
    """把占位符还原为原引号包裹的字符串。"""
    for k, v in placeholders.items():
        s = s.replace(k, v)
    return s


def _redirect_violation(command: str, scratch: str) -> Optional[str]:
    """会话作用域下扫描重定向目标：绝对路径必须落在 scratch 内。

    相对路径目标落 cwd（已被钉在 scratch），天然安全；
    /dev/null 与系统只读前缀跳过。返回第一个违规目标，无则 None。
    """
    for raw in REDIRECT_TARGET_RE.findall(command):
        path = raw.strip()
        if not path or path == "/dev/null":
            continue
        if path.startswith(SYSTEM_PREFIXES):
            continue
        try:
            resolved = os.path.realpath(path)
        except (OSError, ValueError):
            continue
        if resolved == scratch or resolved.startswith(scratch + os.sep):
            continue
        return path
    return None


def _cd_into_space(command: str, space_root) -> bool:
    """命令中是否存在 cd 进空间子树（此后相对路径基准漂移到空间内）。"""
    import pathlib

    if space_root is None:
        return False
    for tok in CD_TARGET_RE.findall(command):
        t = tok.strip("\"'")
        if not t or t.startswith("-") or t.startswith("$"):
            continue
        if not t.startswith("/"):
            continue  # 相对 cd 基于 scratch，基准仍在会话内
        try:
            resolved = pathlib.Path(os.path.realpath(t))
        except (OSError, ValueError):
            continue
        sr = pathlib.Path(str(space_root))
        if resolved == sr or sr in resolved.parents:
            return True
    return False


def _relative_output_targets(command: str) -> list:
    """收集命令中的相对路径输出目标（重定向 / --out 类参数）。

    仅在「cd 进空间后」用于拦截：此时相对输出会落到空间内（cwd 漂移）。
    """
    targets = []
    for raw in REL_REDIRECT_RE.findall(command):
        t = raw.strip("\"'")
        if t and not t.startswith("/") and t not in ("&", "/dev/null"):
            targets.append(t)
    for raw in REL_OUTPUT_PARAM_RE.findall(command):
        t = raw.strip("\"'")
        if t and not t.startswith("/"):
            targets.append(t)
    return targets


class ShellExecutor:
    """安全执行 shell 命令。

    安全层级：
    1. 命令黑名单拦截（rm -rf /, sudo 等）
    2. 工作目录限制（cwd 必须在 work_dir 子树内）
    3. 命令体绝对路径沙箱（L4）
    4. 超时控制（杀进程组，防孤儿）
    5. 输出截断（stdout/stderr 各截断至 max_output_chars）
    6. 环境变量过滤（清除敏感变量）
    7. 结构化错误回流（L6：error_kind + hint）
    8. 系统级沙箱兜底（ExecutionBackend：seatbelt / bwrap / 低权限用户，
       校验层被绕过时的第二道防线，详见 docs/design/sandbox-design.md）
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        path_space: Optional[PathSpace] = None,
        backend: Optional[ExecutionBackend] = None,
    ):
        config = config or {}
        self.work_dir = os.path.realpath(config.get("work_dir", "."))
        # 若注入了 PathSpace，则命令体路径沙箱统一走 PathSpace（收编原 _check_command_paths）；
        # 未注入时回退到内置 _check_command_paths，保持向后兼容。
        self.path_space = path_space
        # 执行后端：默认 local（裸 sh）；系统级沙箱由端上按配置选择注入
        self.backend = backend or LocalBackend()
        self.default_timeout = config.get("timeout", 30)
        self.max_timeout = config.get("max_timeout", 300)
        self.max_output_chars = config.get("max_output_chars", 2000)
        self.blocked_patterns: List[str] = config.get("blocked_patterns", [])
        self.blocked_env: List[str] = config.get("blocked_env", [])
        # L4: 命令体路径沙箱开关
        self.enable_path_sandbox = config.get("enable_path_sandbox", True)
        # 读取语义阈值（见 config.schema.AgentConfig.* 注释）：仅对识别为读取类的命令生效
        self.read_max_lines = int(config.get("read_max_lines", 2000))
        self.read_line_max_chars = int(config.get("read_line_max_chars", 2000))
        self.read_max_chars = int(config.get("read_max_chars", 30000))
        # cat 整读黑名单开关（默认开；用户可显式关掉，但默认安全)
        self.block_cat = bool(config.get("block_cat", True))
        # 读取视图输出前缀（loop / _guard 据此判定是否为读取类输出，豁免通用截断）
        self.read_header_marker = "=== "

    def _normalize_scheme_paths(self, command: str) -> tuple[str, list[str]]:
        """命令体中的 scheme:// 路径 → 物理路径（透明改写）。

        仅处理 PathSpace 已注册的 mount scheme：未知 scheme / 远程 scheme
        (http/https/...) / 越界 / 解析失败一律原样返回，留给后续错误分流。
        返回 (改写后命令, 改写清单)，改写清单附到结果 content 让用户可见。

        实现：分词后对每个 token 用 _TOKEN_SCHEME_RE 搜内部 scheme 段。
        URI_RE 自身带 ^ 锚定只匹配整串；引号包裹（"workspace://..."）会失败。
        改用 search 后只替换 scheme 段，保留引号 / 标点。
        """
        if self.path_space is None:
            return command, []

        rewrites: List[str] = []
        tokens = re.split(r"(\s+)", command)  # 保留分隔符以便原样拼回
        for i, tok in enumerate(tokens):
            m = _TOKEN_SCHEME_RE.search(tok)
            if m is None:
                continue
            scheme = m.group(1).lower()
            if scheme not in self.path_space.mounts:
                continue
            full_token = m.group(0)
            try:
                physical = self.path_space.resolve(full_token)
                self.path_space.assert_safe(physical, mode="read")
            except (ValueError, PermissionError):
                continue
            rewrites.append(f"{full_token} → {physical}")
            # 只替换 token 内的 scheme 段，保留可能的外层引号
            tokens[i] = tok.replace(full_token, str(physical), 1)

        return "".join(tokens), rewrites

    @staticmethod
    def _format_rewrite_header(rewrites: List[str]) -> str:
        """把改写清单拼成可读 header（多行）。"""
        if not rewrites:
            return ""
        lines = "\n".join(f"  {r}" for r in rewrites)
        return f"{_SCHEME_REWRITE_PREFIX}\n{lines}\n"

    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> "ToolResult":
        """执行 shell 命令，返回结构化 ToolResult（含 error_kind + hint）。"""
        from ..tools.base import ToolResult

        if not command or not command.strip():
            return ToolResult(
                ok=False, content="", error="命令为空",
                error_kind="PARAM_MISSING",
                hint="命令不能为空",
            )

        # 0. scheme:// 路径透明改写为物理路径
        # 框架逻辑路径（workspace:// 等）经 PathSpace.resolve() 换成物理路径，
        # 让 shell 永远拿到真路径。改写清单附到结果 content 头部供 trace 可见。
        command, scheme_rewrites = self._normalize_scheme_paths(command)
        rewrite_header = self._format_rewrite_header(scheme_rewrites)

        # 1. 黑名单检查
        for pattern in self.blocked_patterns:
            if pattern in command:
                return ToolResult(
                    ok=False, content="",
                    error=f"命令被黑名单拦截: 包含 '{pattern}'",
                    error_kind="SANDBOX",
                    hint=f"命令包含危险模式 '{pattern}'，请改用安全方式",
                )

        # 1.5 整读类黑名单（cat）：cat 无法指定行范围，大文件必然被静默截断。
        # 拦截并引导用 sed / head / tail / awk / grep -n 等带行号的读取方式。
        if self._is_blocked_command(command):
            cmd = _extract_command_name(command)
            return ToolResult(
                ok=False, content="",
                error=f"禁止使用 '{cmd}' 整读（大文件会被静默截断）",
                error_kind="BLOCKED_COMMAND",
                hint=(
                    f"'{cmd}' 无法指定行范围，禁止使用。读取文件请用以下任一方式：\n"
                    "  - shell.sed(file=<f>, start_line=<A>, end_line=<B>) — 精确行范围\n"
                    "  - shell.run_command(command=\"sed -n 'A,Bp' <file>\")\n"
                    "  - shell.run_command(command=\"head -n N <file>\")  # 取前 N 行\n"
                    "  - shell.run_command(command=\"tail -n N <file>\")  # 取后 N 行\n"
                    "  - shell.run_command(command=\"grep -n 'pattern' <file>\")  # 检索定位\n"
                    "  - shell.rg(pattern=..., path=...)  # ripgrep 搜索\n"
                    "  - shell.fd(pattern=...)  # fd 查找文件名"
                ),
            )

        # 2. 超时限制
        effective_timeout = min(timeout or self.default_timeout, self.max_timeout)

        # 会话作用域：cwd 默认钉在会话 scratch（相对路径输出天然落会话目录）；
        # 显式 cwd 允许 scratch 或空间根（构建/测试需 cd 到项目目录）
        from ..runtime.session_scope import current_scope

        scope = current_scope()
        work_dir = self.work_dir
        allowed_cwd_roots = [self.work_dir]
        if scope is not None:
            work_dir = str(scope.scratch)
            allowed_cwd_roots = [str(scope.scratch)]
            if scope.space_root is not None:
                allowed_cwd_roots.append(str(scope.space_root))

        # 3. 工作目录限制
        effective_cwd = os.path.realpath(cwd or work_dir)
        if not any(
            effective_cwd == r or effective_cwd.startswith(r + os.sep)
            for r in allowed_cwd_roots
        ):
            roots_desc = " 或 ".join(allowed_cwd_roots)
            return ToolResult(
                ok=False, content="",
                error=f"工作目录越界: {cwd} 不在 {roots_desc} 内",
                error_kind="SANDBOX",
                hint=f"工作目录 {cwd} 超出允许范围，请在 {roots_desc} 内操作",
            )

        # 会话作用域下的重定向写拦截：绝对路径重定向目标必须落在 scratch 内，
        # 写项目空间（workspace://）必须走文件工具（显式 scheme + 审计）
        if scope is not None:
            bad_redirect = _redirect_violation(command, str(scope.scratch))
            if bad_redirect is not None:
                ws = self.path_space.mounts.get("workspace") if self.path_space else None
                ws_root = ws.physical if ws is not None else None
                if (
                    scope.space_root is not None
                    and ws_root is not None
                    and bad_redirect.startswith(str(ws_root))
                ):
                    hint = (
                        f"shell 不能直接写项目空间 ({bad_redirect})；"
                        "修改项目文件请用 filesystem.apply_patch（workspace:// 前缀），"
                        "临时文件请用相对路径（落会话目录）"
                    )
                else:
                    hint = f"重定向目标 {bad_redirect} 超出会话目录，请写相对路径"
                return ToolResult(
                    ok=False, content="",
                    error=f"重定向目标越界: {bad_redirect}",
                    error_kind="SANDBOX",
                    hint=hint,
                )
        # cd 漂移拦截：cd 进空间后，相对路径输出（重定向 / --out 类参数）会落进
        # 空间而非会话目录（qiaoma-wudao 误写即此形态），直接拒绝并给出正确写法
        if scope is not None and _cd_into_space(command, scope.space_root):
            rel_targets = _relative_output_targets(command)
            if rel_targets:
                return ToolResult(
                    ok=False, content="",
                    error=f"cd 进项目空间后存在相对路径输出目标: {rel_targets[0]}",
                    error_kind="SANDBOX",
                    hint=(
                        f"cd 进项目空间后，相对输出 '{rel_targets[0]}' 会落到空间内；"
                        f"临时文件请写会话目录绝对路径（{scope.scratch}）或去掉 cd，"
                        "项目文件修改请用 filesystem.apply_patch（workspace:// 前缀）"
                    ),
                )

        # L4: 命令体绝对路径沙箱检查（优先走 PathSpace；未注入则回退内置实现）
        if self.enable_path_sandbox:
            if self.path_space is not None:
                violations = self.path_space.command_path_violations(command)
                if violations:
                    bad = violations[0]
                    ws = self.path_space.mounts.get("workspace")
                    ws_root = ws.physical if ws else self.work_dir
                    return ToolResult(
                        ok=False, content="",
                        error=f"路径越界: {bad} 不在工作目录内",
                        error_kind="SANDBOX",
                        hint=f"路径 '{bad}' 超出允许范围，请使用相对路径或在 {ws_root} 内操作",
                    )
            else:
                sandbox_err = self._check_command_paths(command)
                if sandbox_err is not None:
                    return sandbox_err

        # 4. 环境变量过滤
        env = dict(os.environ)
        for key in self.blocked_env:
            env.pop(key, None)

        # 5. 执行：后端包装（系统级沙箱）+ 进程组管理（超时杀全组，防孤儿进程）
        try:
            argv = self.backend.wrap_argv(command, scope, self.path_space)
            result = run_argv(argv, effective_cwd, env, effective_timeout)
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False, content="",
                error=f"命令超时（{effective_timeout}s）: {command[:100]}",
                error_kind="TIMEOUT",
                hint=f"命令执行超过 {effective_timeout}s，请缩小搜索范围或减少输出量",
            )
        except FileNotFoundError:
            # exit_code=127 的另一种表现：命令本身不存在
            cmd_name = command.split()[0]
            return ToolResult(
                ok=False, content="",
                error=f"命令不存在: {cmd_name}",
                error_kind="TOOL_UNAVAILABLE",
                hint=f"命令 '{cmd_name}' 未安装，请检查是否可用或使用替代工具",
            )
        except Exception as e:
            return ToolResult(
                ok=False, content="",
                error=f"执行异常: {e}",
                error_kind="EXEC_ERROR",
                hint=f"执行出错: {e}",
            )

        # L6: 结构化错误回流
        if result.returncode != 0:
            return self._classify_exit_code(result, command, rewrite_header, effective_cwd)

        # 6. 输出处理（成功）：
        #    读取类命令 → 走行号化视图（行号 + 头部契约 + 续读指令），
        #    普通命令 → 沿用字符硬切
        if self._is_read_command(command):
            view = self._render_read_view(command, result.stdout or "")
            if view is not None:
                content = f"[exit_code=0]\n{rewrite_header}stdout:\n{view}"
            else:
                # 视图渲染失败（目标文件解析失败等），回退到普通截断
                stdout = self._truncate(result.stdout or "")
                stderr = self._truncate(result.stderr or "")
                content = f"[exit_code=0]\n{rewrite_header}stdout:\n{stdout}"
                if stderr:
                    content += f"\nstderr:\n{stderr}"
        else:
            # 非读取类命令使用结构化截断
            stdout = self._truncate_structured(result.stdout or "", command) if result.stdout else ""
            stderr = self._truncate(result.stderr or "")
            content = f"[exit_code=0]\n{rewrite_header}stdout:\n{stdout}"
            if stderr:
                content += f"\nstderr:\n{stderr}"
        return ToolResult(ok=True, content=content)

    def _check_command_paths(self, command: str) -> Optional["ToolResult"]:
        """L4: 检查命令体中的绝对路径是否在 work_dir 内。

        提取命令中所有以 / 开头的路径 token，检查是否越界。
        白名单：管道符后的命令名（如 /usr/bin/grep）不检查，只检查操作目标路径。
        """
        from ..tools.base import ToolResult

        # 提取命令中的绝对路径 token（排除常见系统命令路径）
        # 匹配 /xxx/yyy 或 /xxx 形式的 token
        path_pattern = re.compile(r'(?:^|\s)((?:/[^/\s|;&><]+)+)')
        matches = path_pattern.findall(command)

        for raw_path in matches:
            path = raw_path.strip()
            if not path or path == "/dev/null":
                continue

            # 白名单：常见的只读系统命令路径
            if path.startswith("/usr/bin/") or path.startswith("/bin/") or \
               path.startswith("/usr/sbin/") or path.startswith("/sbin/"):
                continue

            # 解析为绝对路径
            try:
                resolved = os.path.realpath(path)
            except (OSError, ValueError):
                continue

            # 检查是否在 work_dir 内
            if not resolved.startswith(self.work_dir + os.sep) and resolved != self.work_dir:
                return ToolResult(
                    ok=False, content="",
                    error=f"路径越界: {path} 不在 {self.work_dir} 内",
                    error_kind="SANDBOX",
                    hint=f"路径 '{path}' 超出工作目录范围，请使用相对路径或在 {self.work_dir} 内操作",
                )

        return None

    def _resolve_if_exists_in_work_dir(
        self, path: str, effective_cwd: Optional[str]
    ) -> Optional[str]:
        """若 path 是裸相对路径、在当前 effective_cwd（会话 scratch）找不到、
        但在 self.work_dir（项目根）存在，返回其在项目根下的绝对路径，否则 None。

        用途：会话作用域下 run_command 的 cwd 被钉在 scratch，模型用裸相对路径
        （如 docs/design/）访问项目文件会失败。此函数用于在报错 hint 里指出
        「该路径其实在项目根存在」，引导改用 workspace:// 前缀或绝对路径，
        而不是泛泛地提示「用 ls 确认路径」。

        仅对裸相对路径生效；绝对路径 / scheme:// 路径直接返回 None（交给其他分支）。
        """
        if not path or os.path.isabs(path) or "://" in path:
            return None
        ec = effective_cwd or self.work_dir
        # 当前 cwd 就找得到 → 不是「在 scratch 找不到但在项目根有」的情形，不提示
        if os.path.exists(os.path.realpath(os.path.join(ec, path))):
            return None
        cand = os.path.realpath(os.path.join(self.work_dir, path))
        if os.path.exists(cand):
            return cand
        return None

    def _classify_exit_code(
        self,
        result: "subprocess.CompletedProcess",
        command: str,
        rewrite_header: str = "",
        effective_cwd: Optional[str] = None,
    ) -> "ToolResult":
        """L6: 根据退出码分类错误并生成 hint。"""
        from ..tools.base import ToolResult

        # stdout 走结构化截断（失败时也可能有大量输出，如 find 在权限拒绝前已列出大量路径）
        stdout = self._truncate_structured(result.stdout or "", command) if result.stdout else ""
        stderr = self._truncate(result.stderr or "")
        content = f"[exit_code={result.returncode}]\n{rewrite_header}stdout:\n{stdout}"
        if stderr:
            content += f"\nstderr:\n{stderr}"

        stderr_lower = (result.stderr or "").lower()

        # exit_code=127 或 "command not found"
        if result.returncode == 127 or "command not found" in stderr_lower:
            cmd_name = command.split()[0]
            return ToolResult(
                ok=False, content=content,
                error=f"命令不存在: {cmd_name}",
                error_kind="TOOL_UNAVAILABLE",
                hint=f"命令 '{cmd_name}' 未安装或不在 PATH 中，请改用其他可用工具",
            )

        # 命令中残留 PathSpace 已注册的 mount scheme（_normalize_scheme_paths 改不动的）：
        # 远程 scheme (http://...) 不在此列，因为 shell 工具应避免执行远程命令。
        # 放在「No such file」之前：含 framework scheme 的命令失败时，
        # 几乎一定是 scheme 错位，应优先给针对性 hint 而不是泛泛的"用 ls 确认"。
        if self.path_space is not None:
            for m in _TOKEN_SCHEME_RE.finditer(command):
                scheme = m.group(1).lower()
                if scheme in self.path_space.mounts:
                    ws = self.path_space.mounts.get("workspace")
                    ws_physical = f"\n  workspace:// 物理根: {ws.physical}" if ws else ""
                    return ToolResult(
                        ok=False, content=content,
                        error="命令中含框架逻辑路径（scheme://），shell 无法解析",
                        error_kind="SCHEME_PATH_MISUSE",
                        hint=(
                            "shell 不接受 workspace://、skills:// 这类逻辑 scheme。"
                            "改用以下任一方式：\n"
                            "  1. 相对路径（默认相对工作目录）\n"
                            "  2. filesystem 工具（list/read/write 支持 scheme:// 前缀）"
                            f"{ws_physical}"
                        ),
                    )

        # exit_code=1 且含 "No such file or directory"
        if "no such file or directory" in stderr_lower:
            # 提取 stderr 中的实际路径（sed: /path/to/file: No such file...）
            actual_path = None
            for line in stderr_lower.split('\n'):
                if 'no such file' in line:
                    # 尝试提取冒号后的路径
                    parts = line.split(':')
                    if len(parts) >= 2:
                        actual_path = parts[1].strip()
                        break

            # hint 按实际错误特征分派：自然描述发生了什么，不做硬性流程指令
            if actual_path:
                hint = f"路径不存在: {actual_path}"
                # 检查是否是相对路径拼接错误（工作目录被错误拼接）
                if self.work_dir in actual_path:
                    relative_part = actual_path.replace(self.work_dir, '').lstrip('/')
                    hint += f"\n（路径被拼接为 {self.work_dir}/{relative_part}，可能是相对路径前缀有误）"
                else:
                    # 裸相对路径在会话 scratch 找不到、但在项目根存在：
                    # 这是会话作用域下最常见的误用（cwd 被钉在 scratch 而非项目根），
                    # 主动引导用 workspace:// 前缀或绝对路径访问项目根，避免模型反复试错。
                    proj_path = self._resolve_if_exists_in_work_dir(actual_path, effective_cwd)
                    if proj_path is not None:
                        hint += (
                            f"\n该路径在会话目录（{effective_cwd}）下不存在，"
                            f"但在项目根下存在：{proj_path}\n"
                            f"shell 默认工作目录是会话 scratch（写安全），裸相对路径不会相对项目根解析。"
                            f"访问项目文件请用 workspace:// 前缀或绝对路径，例如："
                            f"\n  ls workspace://{actual_path}"
                            f"\n  ls {proj_path}"
                        )
            else:
                hint = "目标文件或目录不存在，请确认路径正确"

            return ToolResult(
                ok=False, content=content,
                error="文件或目录不存在",
                error_kind="EXEC_ERROR",
                hint=hint,
            )

        # exit_code=1 且含 "Permission denied"（含系统级沙箱拒绝信号：
        # seatbelt / bwrap 拦截越权写时常见 "Operation not permitted"）
        if "permission denied" in stderr_lower or "operation not permitted" in stderr_lower:
            backend_name = getattr(self.backend, "name", "local")
            if backend_name != "local":
                # sandbox_apply 失败 = profile 无法应用（常见于宿主环境本身
                # 已处于沙箱内，如 IDE 的 Bash 工具，嵌套沙箱被 macOS 拒绝）
                if "sandbox_apply" in stderr_lower:
                    hint = (
                        f"系统级沙箱（{backend_name}）profile 应用失败：当前进程可能"
                        "已处于另一层沙箱内（IDE/托管环境），无法嵌套。"
                        "请将 sandbox.mode 改为 off 或在原生终端运行"
                    )
                else:
                    hint = (
                        f"命令被系统级沙箱（{backend_name}）拒绝：可写区域仅限会话目录，"
                        "修改项目文件请用 filesystem.apply_patch（workspace:// 前缀），"
                        "临时文件请用相对路径"
                    )
            else:
                hint = "没有权限执行该操作，请检查文件权限或使用其他方式"
            return ToolResult(
                ok=False, content=content,
                error="权限不足",
                error_kind="SANDBOX",
                hint=hint,
            )

        # 默认：通用执行错误——按退出码给出事实描述
        return ToolResult(
            ok=False, content=content,
            error=f"退出码 {result.returncode}",
            error_kind="EXEC_ERROR",
            hint=f"命令执行失败，退出码 {result.returncode}",
        )

    def _truncate(self, text: str) -> str:
        """截断输出至 max_output_chars。"""
        if len(text) > self.max_output_chars:
            return text[: self.max_output_chars] + f"\n...(已截断，共 {len(text)} 字符)"
        return text

    def _truncate_structured(self, text: str, command: str) -> str:
        """非读取类命令的结构化截断：提供元信息头和续读建议。

        与简单 _truncate 的区别：
        1. 给出总量信息（总行数/总字符数）
        2. 给出展示范围（当前展示了前 N 行）
        3. 给出可操作的续读建议（如何缩小范围继续获取）

        格式：
        [元信息]
        命令: <original_command>
        原始输出: <total_lines> 行, <total_chars> 字符
        展示范围: 前 <shown_lines> 行 (第 1-<shown_lines> 行)
        ---
        <前 N 行内容>
        ---
        [截断建议] ...
        """
        total_chars = len(text)
        if total_chars <= self.max_output_chars:
            return text

        # 统计总行数
        lines = text.split("\n")
        total_lines = len(lines)

        # 按字符预算计算能展示多少行
        # 预留空间给元信息头和截断建议（约 400 字符）
        budget = self.max_output_chars - 400
        if budget < 0:
            budget = self.max_output_chars

        shown_lines = 0
        shown_chars = 0
        shown_parts = []
        for i, line in enumerate(lines):
            line_len = len(line) + 1  # +1 for \n
            if shown_chars + line_len > budget:
                break
            shown_parts.append(line)
            shown_chars += line_len
            shown_lines += 1

        # 如果一行都放不下，至少放前 1000 字符
        if shown_lines == 0:
            shown_content = text[:self.max_output_chars - 200]
            shown_lines = shown_content.count("\n") + 1
        else:
            shown_content = "\n".join(shown_parts)

        # 生成续读建议
        suggestions = self._generate_truncation_suggestions(command)

        # 组装结构化输出
        result = (
            f"[元信息]\n"
            f"命令: {command}\n"
            f"原始输出: {total_lines} 行, {total_chars} 字符\n"
            f"展示范围: 前 {shown_lines} 行 (第 1-{shown_lines} 行)\n"
            f"---\n"
            f"{shown_content}\n"
            f"---\n"
            f"{suggestions}"
        )
        return result

    def _generate_truncation_suggestions(self, command: str) -> str:
        """根据命令类型生成针对性的截断续读建议。"""
        cmd_name = _extract_command_name(command) or ""
        cmd_name = cmd_name.split()[0] if cmd_name else ""

        suggestions = ["[截断建议] 输出已截断。可尝试以下方式缩小范围:"]

        # 通用建议（所有命令都适用）
        suggestions.append(f"  • 管道过滤: {command} | grep '关键词'")
        suggestions.append(f"  • 限制数量: {command} | head -n N")

        # 针对特定命令的建议
        if cmd_name == "find":
            suggestions.append(f"  • 限制深度: {command} -maxdepth 2")
            suggestions.append(f"  • 指定类型: {command} -type f (只找文件)")
            suggestions.append(f"  • 名称过滤: {command} -name '*.py'")
        elif cmd_name in ("ls", "dir"):
            suggestions.append(f"  • 限制层级: {command} -d 1 (只看当前目录)")
            suggestions.append(f"  • 指定子目录: ls -la <具体路径>")
        elif cmd_name == "tree":
            suggestions.append(f"  • 限制深度: {command} -L 2")
        elif cmd_name == "git":
            if "log" in command:
                suggestions.append(f"  • 限制条数: {command} -n 20")
                suggestions.append(f"  • 时间范围: {command} --since='1 week ago'")
            elif "diff" in command:
                suggestions.append(f"  • 指定文件: {command} -- <file>")
        elif cmd_name in ("npm", "yarn", "pnpm"):
            # 避免重复子命令（如 "npm list" → "npm list --depth=1" 而非 "npm list list --depth=1"）
            suggestions.append(f"  • 限制深度: {command} --depth=1")
            suggestions.append(f"  • 过滤输出: {command} | grep 'package-name'")
        elif cmd_name == "docker":
            suggestions.append(f"  • 格式化输出: {command} --format '{{.ID}} {{.Names}}'")
            suggestions.append(f"  • 限制数量: {command} | head -n 10")
        else:
            suggestions.append(f"  • 指定子目录: {command} <更具体的路径>")
            suggestions.append(f"  • 添加过滤: {command} | awk '/pattern/'")

        return "\n".join(suggestions)

    # ── 读取语义识别 ──────────────────────────────────────────

    def _is_read_command(self, command: str) -> bool:
        """是否应走行号化视图输出（视图类或检索类命令）。

        关键约束：只在「独立使用」（管道右侧为空）时走行号化视图。
        管道链如 `sed -n '1,$p' file | wc -c` 中，sed 是数据源，下游 wc / grep -n
        等需要原始字节，不能被行号化视图污染 —— 这种情况走原路径。
        """
        cmd = _extract_command_name(command)
        if cmd is None:
            return False
        if cmd not in _READ_VIEW_COMMANDS and cmd not in _READ_GREP_COMMANDS:
            return False
        # 引号感知检测管道：awk 脚本里的 `|`（如 awk '{print $1|$2}'）会被引号包裹，
        # 先 hide 再 check 才不会误判
        s_hidden, _ = _hide_quoted(command)
        return "|" not in s_hidden

    def _is_blocked_command(self, command: str) -> bool:
        """是否在整读黑名单（默认 cat）。"""
        if not self.block_cat:
            return False
        cmd = _extract_command_name(command)
        return cmd in _BLOCKED_COMMANDS

    def _extract_target_file(self, command: str) -> Optional[str]:
        """从命令字符串粗略提取目标文件路径。

        解析策略：取管道左侧首个分段，跳过 -flag 及其值，对 awk/sed 等命令跳过
        首个非 flag 的「脚本/行范围」参数（awk 'NR...' file / sed -n 'A,Bp' file）。

        安全校验仍由 L4 path_sandbox 完成，此处仅用于读取视图渲染。
        """
        s = command.strip()
        if not s:
            return None
        # 引号感知分割链式：先把引号包裹的内容换成占位符，避免 awk 脚本里的 `&&`
        # / sed 脚本里的 `|` 被误当 shell 分隔符切掉
        s_hidden, placeholders = _hide_quoted(s)
        for sep in ("|", "&&", "||", ";"):
            if sep in s_hidden:
                s_hidden = s_hidden.split(sep, 1)[0]
                break
        s = _restore_quoted(s_hidden.strip(), placeholders)

        # token 化（保留引号包裹的 token 完整）
        tokens: List[str] = []
        in_quote: Optional[str] = None
        cur = ""
        for ch in s:
            if in_quote:
                cur += ch
                if ch == in_quote:
                    tokens.append(cur)
                    cur = ""
                    in_quote = None
                continue
            if ch in ("'", '"'):
                in_quote = ch
                cur += ch
                continue
            if ch.isspace():
                if cur:
                    tokens.append(cur)
                    cur = ""
                continue
            cur += ch
        if cur:
            tokens.append(cur)

        if not tokens:
            return None
        cmd = _extract_command_name(command)

        # awk / sed 的参数顺序是：脚本在前，文件在后
        # 例：awk 'NR>=20 && NR<=25' /file  → 脚本 = 'NR>=20 && NR<=25'
        # 例：sed -n '5,10p' /file          → -n + 范围都是 flag/参数，最后是文件
        skip_first_script = cmd in {"awk", "sed"}
        # wc 只有一个文件参数：wc -l /file
        i = 1
        skipped_first = False
        while i < len(tokens):
            t = tokens[i]
            if t.startswith("-"):
                # 长 flag 取值（--key=value / --key value）
                if "=" in t:
                    i += 1
                    continue
                # sed 的 -n 后紧跟范围表达式（'A,Bp'），不视作独立的 value：
                # -n 'A,Bp' 整体等价于 -nA,Bp，跳过 -n 后让 'A,Bp' 进入通用循环
                if skip_first_script and t == "-n":
                    i += 1
                    continue
                if t in _FLAGS_TAKING_VALUE and i + 1 < len(tokens):
                    i += 2
                    continue
                # 短 flag 取值（-n100 / -A3 / -e 'pat'）
                if re.match(r"^-[A-Za-z]\S", t):
                    i += 1
                    continue
                # 纯 flag
                i += 1
                continue
            # 第一个非 flag token：awk/sed 时是脚本，跳过
            if skip_first_script and not skipped_first:
                skipped_first = True
                i += 1
                continue
            return _strip_quotes(t)
        return None

    def _extract_view_range(
        self, command: str, cmd: str, total_lines: int,
    ) -> "tuple[int, int, str]":
        """从命令字符串解析行范围 (start, end, mode)，1-indexed，含两端。

        read_max_lines 是「单次输出行窗口」硬上限：无论用户命令指定的范围多大，
        输出都不会超过 read_max_lines 行，确保续读契约生效。

        不同命令的解析：
        - sed -n 'A,Bp' | -n "A,B" | -n "A,$p"  → (A, B, "sed")
        - head -n N | head -N                  → (1, N, "head")
        - tail -n N | tail -N                  → (total-N+1, total, "tail")
        - awk 'NR>=A' / 'NR<=B'                → (A, B, "awk")
        - nl                                    → (1, read_max_lines, "nl")
        """
        cap = self.read_max_lines  # 单次输出行窗口上限
        if cmd == "sed":
            m = _SED_RANGE_RE.search(command)
            if m:
                start = int(m.group(1))
                end_str = m.group(2)
                end = total_lines if end_str == "$" else int(end_str)
                end = min(end, start + cap - 1, total_lines)
                return start, end, "sed"
            # sed 没带 -n：整读
            return 1, min(cap, total_lines), "sed(全量)"
        if cmd == "head":
            m = _HEAD_N_RE.search(command)
            if m:
                n = int(m.group(1))
                return 1, min(n, cap, total_lines), "head"
            return 1, min(10, cap, total_lines), "head(默认10)"
        if cmd == "tail":
            m = _TAIL_N_RE.search(command)
            if m:
                n = min(int(m.group(1)), cap)
                return max(1, total_lines - n + 1), total_lines, "tail"
            n = min(10, cap)
            return max(1, total_lines - n + 1), total_lines, "tail(默认10)"
        if cmd == "awk":
            m_gte = _AWK_NR_GTE_RE.search(command)
            m_lte = _AWK_NR_LTE_RE.search(command)
            start = int(m_gte.group(1)) if m_gte else 1
            end = int(m_lte.group(1)) if m_lte else total_lines
            end = min(end, start + cap - 1, total_lines)
            return start, end, "awk"
        if cmd == "nl":
            return 1, min(cap, total_lines), "nl"
        return 1, min(cap, total_lines), cmd

    def _count_lines(self, path: str) -> int:
        """轻量统计文件行数（按换行符）。"""
        n = 0
        with open(path, "rb") as f:
            for _ in f:
                n += 1
        return n

    def _read_window(
        self, path: str, start: int, end: int,
    ) -> List[str]:
        """读取文件 [start, end] 范围（1-indexed，含两端），去除行尾换行符。

        单行长度不在此截断，由 _render_read_view 内联处理（便于保留截断提示）。
        """
        lines: List[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i < start:
                    continue
                if i > end:
                    break
                lines.append(line.rstrip("\n").rstrip("\r"))
        return lines

    def _render_read_view(self, command: str, raw_stdout: str) -> Optional[str]:
        """把读取类命令的输出改造成「行号化 + 头部契约 + 续读指令」格式。

        返回 None 表示无法改造（命令不是读取类、目标文件解析失败等），
        调用方应回退到普通 _truncate 路径。
        """
        cmd = _extract_command_name(command)
        if cmd is None:
            return None
        target_file = self._extract_target_file(command)

        # 检索类（grep -n / rg）：自带行号，原样输出 + 头部契约
        if cmd in _READ_GREP_COMMANDS:
            if not target_file or not os.path.isfile(target_file):
                return None
            try:
                total_lines = self._count_lines(target_file)
            except OSError:
                return None
            try:
                size = os.path.getsize(target_file)
            except OSError:
                return None
            header = (
                f"{self.read_header_marker}{target_file} | {total_lines} lines | "
                f"~{size // 1024}KB ===\n"
                f"[read] grep -n （grep 已带行号，原样透传）\n"
            )
            # 检索类不走行号化，避免破坏 grep 行号；尾部给"如需精确区段"的指引
            footer = (
                f"\n\n--- 续读 ---\n"
                f"↳ 如需精确行号区段：sed -n 'A,Bp' {target_file}\n"
                f"↳ 如需定位：rg -n 'pattern' {target_file}"
            )
            return header + raw_stdout.rstrip("\n") + footer

        # 视图类（sed / head / tail / awk / nl）
        if not target_file or not os.path.isfile(target_file):
            return None
        try:
            total_lines = self._count_lines(target_file)
        except OSError:
            return None
        try:
            size = os.path.getsize(target_file)
        except OSError:
            return None

        start, end, mode = self._extract_view_range(command, cmd, total_lines)
        # 防御性夹紧
        start = max(1, start)
        end = min(end, total_lines)
        if start > end:
            return f"{self.read_header_marker}{target_file} | {total_lines} lines | ~{size // 1024}KB ===\n[read] 空范围 {start}-{end}\n"

        try:
            lines = self._read_window(target_file, start, end)
        except OSError:
            return None

        # 行号化（行内超 read_line_max_chars 加提示）
        body_parts: List[str] = []
        line_total_chars = 0
        truncated_by_total = False
        for offset, line in enumerate(lines):
            line_no = start + offset
            if len(line) > self.read_line_max_chars:
                line = line[: self.read_line_max_chars] + f"…(本行截断，原 {len(line)} 字符)"
            piece = f"{line_no:5d}│ {line}"
            line_total_chars += len(piece) + 1  # +1 for \n
            if line_total_chars > self.read_max_chars:
                truncated_by_total = True
                break
            body_parts.append(piece)
        body = "\n".join(body_parts)

        header = (
            f"{self.read_header_marker}{target_file} | {total_lines} lines | "
            f"~{size // 1024}KB ===\n"
            f"[read] lines {start}-{start + len(body_parts) - 1} of {total_lines} ({mode})"
            + (" [总字符已达 read_max_chars 上限]" if truncated_by_total else "")
            + "\n"
        )

        # 续读指令 / End of file
        next_start = start + len(body_parts)
        if truncated_by_total:
            next_end = min(next_start + self.read_max_lines - 1, total_lines)
            footer = (
                f"\n\n--- 续读 ---\n"
                f"↳ sed -n '{next_start},{next_end}p' {target_file}\n"
                f"↳ 先 grep -n 'pattern' {target_file} 定位目标行"
            )
        elif next_start <= total_lines:
            next_end = min(next_start + self.read_max_lines - 1, total_lines)
            footer = (
                f"\n\n--- 续读 ---\n"
                f"↳ sed -n '{next_start},{next_end}p' {target_file}\n"
                f"↳ 先 grep -n 'pattern' {target_file} 定位目标行"
            )
        else:
            footer = f"\n\n[End of file - {total_lines} lines total]"

        return header + body + footer

    def render_command(
        self, template: str, params: dict, param_schema: Optional[dict] = None,
        extra_condition_templates: Optional[dict] = None
    ) -> "ToolResult":
        """将参数填入命令模板。

        简单变量替换：{name} → params["name"]
        条件变量：{xxx_opt} → 由 params["xxx"] 决定生成什么

        返回值从 str 改为 ToolResult，以便在渲染失败时返回错误。
        """
        from ..tools.base import ToolResult

        # L1: 参数校验
        if param_schema:
            error = self._validate_params(params, param_schema)
            if error:
                return ToolResult(ok=False, content="", error=error, error_kind="PARAM_MISSING")

        rendered = template

        # 处理条件变量 {xxx_opt}
        # 例如 rg 的 {type_opt} 对应 params["type"]，如果 type 存在则渲染为 "--type {type}"
        condition_templates = {
            "type_opt": ("type", "--type {}"),
            "glob_opt": ("glob", "-g {}"),
            "hidden_opt": ("hidden", "--hidden"),
            "case_opt": ("case_insensitive", "-i"),
            "ext_opt": ("extension", "--extension {}"),
            "depth_opt": ("max_depth", "--max-depth {}"),
            "staged_opt": ("staged", "--staged"),
            "all_opt": ("all", "-a"),
            "oneline_opt": ("oneline", "--oneline"),
            "author_opt": ("author", "--author=\"{}\""),
            "since_opt": ("since", "--since=\"{}\""),
        }

        # 合并额外条件模板（如 fallback 模板的 case_grep_opt）
        if extra_condition_templates:
            for opt_key, spec in extra_condition_templates.items():
                param_key = spec.get("param")
                fmt = spec.get("format")
                if param_key and fmt:
                    condition_templates[opt_key] = (param_key, fmt)

        for opt_key, (param_key, fmt) in condition_templates.items():
            value = params.get(param_key)
            if value is not None and value is not False:
                # 布尔值为 True 时只渲染 flag，字符串值填充到 {}
                if isinstance(value, bool):
                    rendered = rendered.replace(f"{{{opt_key}}}", fmt.split(" ")[0])
                else:
                    rendered = rendered.replace(f"{{{opt_key}}}", fmt.format(value))
            else:
                rendered = rendered.replace(f"{{{opt_key}}}", "")

        # 处理普通变量
        for key, value in params.items():
            if value is not None and not key.endswith("_opt"):
                placeholder = f"{{{key}}}"
                if placeholder in rendered:
                    # 路径参数归一化（为 L4 打基础）
                    if key in ("path", "file", "pathspec"):
                        value = self._normalize_path(value)
                    rendered = rendered.replace(placeholder, str(value))

        # 通用 _opt 回退：未注册在 condition_templates 中的 xxx_opt，
        # 若 params 里有同名的 xxx（且为 truthy）则用 xxx_opt 的值替换
        for key, value in list(params.items()):
            if key.endswith("_opt"):
                placeholder = f"{{{key}}}"
                if placeholder not in rendered:
                    continue
                base_key = key[:-4]
                base_value = params.get(base_key)
                if value and base_value:
                    rendered = rendered.replace(placeholder, str(value))
                else:
                    rendered = rendered.replace(placeholder, "")

        # 清理多余空格
        rendered = " ".join(rendered.split())

        return ToolResult(ok=True, content=rendered)

    def _validate_params(self, params: dict, schema: dict) -> Optional[str]:
        """校验必填参数。"""
        for name, spec in schema.items():
            if spec.get("required") and params.get(name) in (None, ""):
                return f"缺少必填参数: {name}"
        return None

    def _normalize_path(self, path: str) -> str:
        """路径归一化：scheme:// 走 PathSpace 解析，相对路径转绝对路径。"""
        if not path:
            return "."
        # scheme:// 路径（workspace:// 等）必须优先走 PathSpace.resolve()，
        # 否则会被当作相对路径拼到 work_dir 后面，realpath 把 // 折叠为 /，
        # 产出 workspace:/xxx 这种不存在的路径。
        if self.path_space is not None:
            m = URI_RE.match(path.strip())
            if m and m.group(1).lower() in self.path_space.mounts:
                try:
                    resolved = self.path_space.resolve(path)
                    self.path_space.assert_safe(resolved, mode="read")
                    return str(resolved)
                except (ValueError, PermissionError):
                    pass  # 解析失败回退原逻辑
        if path.startswith("~"):
            path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.work_dir, path)
        return os.path.realpath(path)
