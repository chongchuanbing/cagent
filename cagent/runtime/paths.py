"""PathSpace：通用 Agent 的统一路径空间。

解决框架路径管理散乱问题（详见 docs/path-management-design.md）：

- 单一事实来源：所有根目录集中在一个 PathSpace，全局取用。
- 逻辑路径 / 物理路径分离：模型只接触紧凑的 scheme://（如 workspace://、
  skills://<name>/、data://）；物理绝对路径只在框架内部流动。
- 安全校验集中一处：assert_safe() 是所有文件访问的唯一闸门。
- 实例冻结（frozen）：per-run / per-agent 持有自己的 PathSpace，不可变，
  便于并发与会话恢复时固化 base_dir。

边界（不在本模块职责内）：
- 技能脚本内部 open()、MCP 资源自有沙箱，由"IO 经框架代理"契约 / 协议负责。
- 模型幻觉路径（存在但猜错）只能由 assert_safe + exists 兜底，靠 skill 链接
  重写 + 速查卡从源头减少。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# scheme:// 识别（注意 Windows 的 C:\ 无 //，不会误判为 scheme）
URI_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$")

# 命令体中绝对路径 token：以 / 开头，遇空格/管道/重定向/引号截止
CMD_ABS_PATH_RE = re.compile(r"(?:^|\s)((?://?[^/\s|;&><()']+)+)")

# 系统只读/可执行路径白名单（命令体扫描时跳过，不视为用户越界目标）
SYSTEM_PREFIXES: Tuple[str, ...] = (
    "/usr/bin/",
    "/bin/",
    "/usr/sbin/",
    "/sbin/",
    "/usr/lib/",
    "/lib/",
    "/usr/local/bin/",
)

# 远程引用 scheme（不进本地沙箱，交给专门 fetch）
REMOTE_SCHEMES = frozenset({"http", "https", "ftp", "ftps", "s3", "gs", "oss", "cos", "ssh"})


@dataclass(frozen=True)
class Mount:
    """一个逻辑挂载点。

    physical: 归一化后的绝对物理路径（沙箱根）。
    expose_to_llm: 是否允许模型用 <name>:// 直接引用（self/plugins/config 默认 False）。
    sandbox: 是否做越界校验（默认 True）。
    modes: 允许的操作集合，默认 {read, write, exec}。
    """

    name: str
    physical: Path
    expose_to_llm: bool = True
    sandbox: bool = True
    modes: frozenset = frozenset({"read", "write", "exec"})

    def allows(self, mode: str) -> bool:
        return mode in self.modes


@dataclass(frozen=True)
class PathSpace:
    """路径空间：逻辑 scheme 与物理挂载点的映射。

    实例冻结（frozen），构造后不可变；扩展挂载点用 with_mount() 返回新实例。
    """

    base_dir: Path
    mounts: Dict[str, Mount] = field(default_factory=dict)
    allow_paths: Tuple[Path, ...] = ()  # 受信外挂区（绝对前缀），合法越界用
    allow_symlink_targets: Tuple[Path, ...] = ()  # 允许的 symlink 目标前缀
    default_mount: str = "workspace"  # 裸相对路径的解析基准（scheme 缺省值）

    def __post_init__(self) -> None:
        base = Path(os.path.realpath(str(self.base_dir)))
        object.__setattr__(self, "base_dir", base)
        norm_mounts = {
            name: Mount(
                m.name,
                Path(os.path.realpath(str(m.physical))),
                m.expose_to_llm,
                m.sandbox,
                m.modes,
            )
            for name, m in self.mounts.items()
        }
        object.__setattr__(self, "mounts", norm_mounts)
        object.__setattr__(
            self, "allow_paths", tuple(Path(os.path.realpath(str(p))) for p in self.allow_paths)
        )
        object.__setattr__(
            self,
            "allow_symlink_targets",
            tuple(Path(os.path.realpath(str(p))) for p in self.allow_symlink_targets),
        )

    # ── 构造助手 ──────────────────────────────────────────

    @classmethod
    def build_default(
        cls,
        base_dir: Union[str, Path],
        *,
        work_dir: Optional[Union[str, Path]] = None,
        data_dir: Union[str, Path] = ".data",
        plugins_dir: Optional[Union[str, Path]] = None,
        config_dir: Optional[Union[str, Path]] = None,
        allow_paths: Tuple[Union[str, Path], ...] = (),
        allow_symlink_targets: Tuple[Union[str, Path], ...] = (),
    ) -> "PathSpace":
        """从端上配置构造默认 PathSpace（相对 base_dir 解析所有目录）。

        skills://<name> 由 SkillsPluginLoader 在加载后通过 with_mount 注入；
        本工厂只注册 workspace/data/self/plugins/config 等框架自有根。
        """
        base = Path(base_dir)
        mounts: Dict[str, Mount] = {
            "workspace": Mount("workspace", _rel(base, work_dir or base)),
            "data": Mount("data", _rel(base, data_dir), expose_to_llm=False),
            "self": Mount(
                "self",
                Path(__file__).resolve().parent.parent,  # cagent/ 包目录
                expose_to_llm=False,
                modes=frozenset({"read"}),
            ),
        }
        if plugins_dir is not None:
            mounts["plugins"] = Mount(
                "plugins", _rel(base, plugins_dir), expose_to_llm=False
            )
        if config_dir is not None:
            mounts["config"] = Mount(
                "config", _rel(base, config_dir), expose_to_llm=False
            )
        return cls(
            base,
            mounts,
            allow_paths=tuple(allow_paths),
            allow_symlink_targets=tuple(allow_symlink_targets),
        )

    def with_mount(self, mount: Mount) -> "PathSpace":
        """返回注入/覆盖一个挂载点的新实例（冻结语义）。"""
        new_mounts = dict(self.mounts)
        new_mounts[mount.name] = mount
        return PathSpace(
            self.base_dir, new_mounts, self.allow_paths, self.allow_symlink_targets
        )

    def for_session(
        self,
        scratch: Union[str, Path],
        space_root: Optional[Union[str, Path]] = None,
    ) -> "PathSpace":
        """按会话派生新实例（会话路径隔离，详见 runtime/session_scope.py）。

        - 始终注册 session:// → scratch（会话工作目录，临时/中间文件落此处）
        - space_root=None（无空间）：workspace:// 重挂为 scratch（别名，模型无感知，
          所有生成文件天然落会话目录）
        - space_root 指定（空间模式）：workspace:// 挂空间根——结构化工具可显式
          写入（登记审计），shell 写入由 ShellExecutor 拦截
        - workspace 原有的 expose_to_llm / modes 语义保留，仅替换物理根
        """
        scratch_p = Path(os.path.realpath(str(scratch)))
        ws = self.mounts.get("workspace")
        mounts = dict(self.mounts)
        mounts["session"] = Mount(
            "session",
            scratch_p,
            expose_to_llm=ws.expose_to_llm if ws else True,
            sandbox=ws.sandbox if ws else True,
            modes=ws.modes if ws else frozenset({"read", "write", "exec"}),
        )
        if space_root is None:
            new_ws_physical = scratch_p
        else:
            new_ws_physical = Path(os.path.realpath(str(space_root)))
        if ws is not None:
            mounts["workspace"] = Mount(
                "workspace", new_ws_physical, ws.expose_to_llm, ws.sandbox, ws.modes
            )
        else:
            mounts["workspace"] = Mount("workspace", new_ws_physical)
        # 空间模式：裸相对路径的解析基准改为 session://（临时文件落会话目录）；
        # 无空间模式：workspace 即 scratch，基准保持 workspace 不变
        return PathSpace(
            self.base_dir, mounts, self.allow_paths, self.allow_symlink_targets,
            default_mount="session" if space_root is not None else "workspace",
        )

    # ── 解析：逻辑 → 物理 ─────────────────────────────────

    def resolve(
        self,
        uri_or_rel: str,
        default_mount: Optional[str] = None,
        scope: Optional[Union[str, Path]] = None,
        mode: str = "read",
    ) -> Path:
        """解析为物理绝对路径。

        - 识别 scheme://：已知 mount 则相对其物理根；远程 scheme 抛 ValueError。
        - 裸绝对路径：直接归一（后续由 assert_safe 校验）。
        - 裸相对路径：相对 default_mount（缺省取 self.default_mount，会话派生
          实例在空间模式下为 session，即裸相对路径落会话 scratch）。
        """
        dm = default_mount or self.default_mount
        s = uri_or_rel.strip()
        m = URI_RE.match(s)
        if m:
            scheme = m.group(1).lower()
            rest = m.group(2)
            if scheme in REMOTE_SCHEMES:
                raise ValueError(f"远程 URI 不走本地解析: {s}")
            if scheme not in self.mounts:
                raise ValueError(f"未知 scheme: {scheme}:// (可用: {sorted(self.mounts)})")
            base = self.mounts[scheme].physical
            target = base / rest if rest else base
        else:
            if os.path.isabs(s):
                target = Path(s)
            else:
                target = self._scope_base(dm, scope) / s
        # ~ 展开 + 归一
        if str(target).startswith("~"):
            target = Path(os.path.expanduser(str(target)))
        return Path(os.path.realpath(str(target)))

    def _scope_base(self, default_mount: str, scope: Optional[Union[str, Path]]) -> Path:
        """解析相对路径的基准目录。

        scope 三种形态：
          - None → default_mount 的物理根
          - Path → 直接使用
          - "skills:tencent-docx" 形如 "mnt:sub" → 该 mount 物理根下的 sub 子目录
        """
        if scope is None:
            return self.mounts[default_mount].physical
        if isinstance(scope, Path):
            return scope
        if isinstance(scope, str):
            if ":" in scope and not scope.startswith("/"):
                mnt, _, sub = scope.partition(":")
                if mnt not in self.mounts:
                    raise ValueError(f"scope 挂载点不存在: {mnt}")
                return self.mounts[mnt].physical / sub
            return Path(scope)
        raise TypeError(f"scope 类型不支持: {type(scope)}")

    def resolve_user_input(
        self,
        s: str,
        default_mount: str = "workspace",
        scope: Optional[Union[str, Path]] = None,
    ) -> Path:
        """模型输入的宽松解析（去 ./ 前缀后调用 resolve）。"""
        return self.resolve(s, default_mount=default_mount, scope=scope)

    # ── 反解析：物理 → 逻辑（给模型看） ─────────────────────

    def relativize(self, abs_path: Union[str, Path]) -> str:
        """物理绝对路径 → 给模型的逻辑路径。

        优先匹配 expose_to_llm 且包含该路径的 mount 中**物理根最长者**（最具体，
        例如 skills:// 优先于其所在的 workspace://）；否则回退 base_dir；
        都不命中则兜底返回绝对路径。
        """
        p = Path(os.path.realpath(str(abs_path)))
        best: Optional[tuple] = None
        for name, mount in self.mounts.items():
            if mount.expose_to_llm and self._within(p, mount.physical):
                if best is None or len(str(mount.physical)) > len(str(best[1].physical)):
                    best = (name, mount)
        if best is not None:
            name, mount = best
            return f"{name}://{p.relative_to(mount.physical).as_posix()}"
        if self._within(p, self.base_dir):
            return f"workspace://{p.relative_to(self.base_dir).as_posix()}"
        return str(p)  # 兜底绝对（受信外挂区等）

    # ── 安全闸门 ──────────────────────────────────────────

    def assert_safe(self, path: Union[str, Path], mode: str = "read") -> Path:
        """唯一安全闸门：realpath 后校验归属任一允许前缀。

        - 允许前缀 = 受信外挂区 ∪ 各 sandbox 且允许 mode 的 mount.physical
                       ∪（read/exec 模式下的系统只读前缀）
        - 符号链接：realpath 后若原路径是 symlink 且目标不在 allow_symlink_targets，
          视为越界拒绝。
        越界抛 PermissionError；返回归一后的绝对路径。
        """
        p = Path(os.path.realpath(str(path)))
        allowed: List[Path] = list(self.allow_paths)
        for mount in self.mounts.values():
            if mount.sandbox and mount.allows(mode):
                allowed.append(mount.physical)
        if mode in ("read", "exec"):
            allowed.extend(Path(x) for x in SYSTEM_PREFIXES)
        if any(self._within(p, a) for a in allowed):
            return p
        # 符号链接特例
        if os.path.islink(str(path)):
            real = Path(os.path.realpath(str(path)))
            if any(self._within(real, t) for t in self.allow_symlink_targets):
                return p
        raise PermissionError(f"路径越界: {path} (mode={mode})")

    # ── 命令体扫描（收编 ShellExecutor._check_command_paths） ──

    def command_path_violations(self, command: str) -> List[str]:
        """扫描 shell 命令体中的绝对路径 token，返回越界的用户目标路径列表。

        系统只读前缀与 /dev/null 跳过；其余逐个 assert_safe(read)。
        空列表 = 无越界。对应原 ShellExecutor._check_command_paths 行为。
        """
        tokens = CMD_ABS_PATH_RE.findall(command)
        violations: List[str] = []
        for raw in tokens:
            path = raw.strip()
            if not path or path == "/dev/null":
                continue
            if path.startswith(SYSTEM_PREFIXES):
                continue
            try:
                resolved = Path(os.path.realpath(path))
            except (OSError, ValueError):
                continue
            try:
                self.assert_safe(resolved, mode="read")
            except PermissionError:
                violations.append(path)
        return violations

    # ── 分流 ──────────────────────────────────────────────

    @staticmethod
    def classify(uri: str) -> str:
        """区分路径类型：local（本地/相对） / scheme（框架 mount） / remote（远程 URI）。"""
        s = uri.strip()
        m = URI_RE.match(s)
        if not m:
            return "local"
        scheme = m.group(1).lower()
        if scheme in REMOTE_SCHEMES:
            return "remote"
        if scheme == "file":
            return "local"
        return "scheme"

    # ── 速查卡（注入 LLM 提示词） ───────────────────────────

    def path_card(self) -> str:
        """给 LLM 的路径速查卡：列出可引用的逻辑 scheme 与含义，禁止猜绝对路径。

        skills:// 为框架级约定（技能资源经 read_skill_file 读取），无论是否已显式
        挂载都在此说明；其余 scheme 仅列 expose_to_llm 的挂载点。

        会话隔离语义：session:// 与 workspace:// 物理根不同（空间模式）时分别
        说明用途；相同（无空间模式）时合并为一条，模型无感知差异。
        """
        ws = self.mounts.get("workspace")
        sm = self.mounts.get("session")
        if (
            ws is not None
            and sm is not None
            and ws.expose_to_llm
            and sm.expose_to_llm
            and ws.physical != sm.physical
        ):
            meaning = {
                "session": (
                    "本次会话工作目录，临时/中间文件一律写这里"
                    "（裸相对路径即落此处）"
                ),
                "workspace": (
                    "项目空间（只读浏览）；修改项目文件必须用 workspace:// 前缀"
                    "走文件工具（写入会登记审计），shell 命令中勿写此目录"
                ),
                "data": "框架私有数据存储（默认对模型隐藏，勿直接读写）",
                "self": "框架自身代码（只读，勿改）",
                "plugins": "插件目录（只读）",
                "config": "配置目录（只读）",
            }
        else:
            meaning = {
                "workspace": "本次会话工作目录，所有生成文件默认写这里（裸相对路径即落此处）",
                "session": "本次会话工作目录，所有生成文件默认写这里（裸相对路径即落此处）",
                "data": "框架私有数据存储（默认对模型隐藏，勿直接读写）",
                "self": "框架自身代码（只读，勿改）",
                "plugins": "插件目录（只读）",
                "config": "配置目录（只读）",
            }
        lines = ["路径速查（仅可用以下 scheme://，切勿猜测绝对路径）："]
        for name, mount in self.mounts.items():
            if not mount.expose_to_llm:
                continue
            desc = meaning.get(name, "框架资源")
            lines.append(f"  - {name}:// : {desc}")
        # skills 是框架级逻辑约定，始终提示
        lines.append("  - skills://<name>/... : 技能资源，读取请用 read_skill_file(skill_name, relative_path)")
        lines.append("  - 远程 https://... 可直接交给联网工具，不进本地沙箱")
        return "\n".join(lines)

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def _within(p: Path, base: Path) -> bool:
        """p 是否等于 base 或位于 base 子树内（均经 realpath 归一）。"""
        p = Path(os.path.realpath(str(p)))
        base = Path(os.path.realpath(str(base)))
        return p == base or base in p.parents


def _rel(base: Path, p: Union[str, Path]) -> Path:
    """相对路径相对 base 解析为绝对；绝对路径原样归一。"""
    p = Path(p)
    if p.is_absolute():
        return Path(os.path.realpath(str(p)))
    return Path(os.path.realpath(str(base / p)))
