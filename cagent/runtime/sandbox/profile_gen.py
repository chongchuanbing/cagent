"""沙箱规则编译器：PathSpace（逻辑权限）→ SandboxRules（平台无关中间规则）。

单一事实源原则：本模块是系统级沙箱规则的唯一出处，各平台后端
（seatbelt / bwrap）只做「规则 → 平台语法」的翻译，不自行发明规则。

规则语义（与 shell 校验层既有策略对齐，作为其物理投影）：
- 可写区 = 会话 scratch 唯一（shell 写项目空间必须走文件工具，
  应用层已拦截，系统层同步收紧为只读）；
- 只读区 = 全部 PathSpace mounts + 系统前缀 + 受信外挂区 + 用户主目录；
- 网络 = 默认禁（内置联网工具走主进程 httpx，不经 shell，天然不受影响）。
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..paths import SYSTEM_PREFIXES

# 系统只读前缀之外，命令运行普遍依赖的只读目录
# （macOS 的 /etc 等符号链接归一到 /private/*，realpath 已处理）
COMMON_READ_PREFIXES: Tuple[str, ...] = (
    "/etc",
    "/private/etc",
    "/System",
    "/Library",
    "/opt",
    "/usr/local",
    "/dev",
)


@dataclass(frozen=True)
class SandboxRules:
    """平台无关的沙箱规则（各后端翻译为目标语法）。"""

    writable: Tuple[Path, ...]      # 允许写的前缀（会话 scratch）
    readable: Tuple[Path, ...]       # 允许读的前缀
    network_deny: bool              # True = 禁网

    def allows_write(self, path) -> bool:
        p = Path(str(path))
        return any(p == w or w in p.parents for w in self.writable)


def build_rules(
    path_space=None,
    scope=None,
    extra_readable: tuple = (),
    network_deny: bool = True,
) -> Optional[SandboxRules]:
    """从会话作用域编译沙箱规则。

    - scope 为 None：无会话作用域（单测直调 / 旧端未接线）→ 返回 None，
      后端退化为普通 sh 执行；
    - path_space 优先用 scope.path_space（会话级派生空间），缺省回退入参。
    """
    if scope is None:
        return None

    scratch = Path(os.path.realpath(str(scope.scratch)))
    readable = set()

    # 系统只读前缀（命令体扫描白名单 + 常见依赖目录）
    for p in SYSTEM_PREFIXES:
        readable.add(Path(p))
    for p in COMMON_READ_PREFIXES:
        readable.add(Path(p))

    # 用户主目录只读（.gitconfig / 解释器环境等命令普遍依赖）
    try:
        home = Path.home()
        if home != Path("/") and not _within(home, scratch):
            readable.add(home)
    except (OSError, RuntimeError):
        pass

    # PathSpace mounts 全部可读（workspace / data / skills 等）+ 受信外挂区
    ps = getattr(scope, "path_space", None) or path_space
    if ps is not None:
        for mount in getattr(ps, "mounts", {}).values():
            readable.add(Path(os.path.realpath(str(mount.physical))))
        for p in getattr(ps, "allow_paths", ()) or ():
            readable.add(Path(str(p)))

    # 配置声明的额外只读区
    for p in extra_readable:
        if p:
            readable.add(Path(os.path.realpath(str(p))))

    # 可写区优先：剔除落在 scratch 子树内的只读项（如无空间模式下
    # workspace 挂到 scratch），避免后端 bind 顺序歧义
    readable = {
        r for r in readable
        if not (r == scratch or _within(r, scratch))
    }

    return SandboxRules(
        writable=(scratch,),
        readable=tuple(sorted(readable)),
        network_deny=network_deny,
    )


def _within(child: Path, parent: Path) -> bool:
    """child 是否位于 parent 子树内。"""
    return parent in child.parents
