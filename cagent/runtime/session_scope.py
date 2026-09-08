"""会话路径作用域：run 期间让文件/Shell 工具感知「会话 scratch + 空间根」。

核心规则（会话路径隔离）：
- 临时/中间文件 → 会话 scratch（.data/sessions/<sid>/scratch）
- 无空间模式：workspace:// 即 scratch（模型无感知，所有生成文件落会话目录）
- 空间模式：workspace:// = 空间根（如项目代码目录）；
  - 结构化工具（filesystem.apply_patch 等）可显式写空间，写入自动登记审计；
  - shell 的 cwd 钉在 scratch，重定向等绝对路径写空间被拦截。

实现方式：ContextVar 携带当前会话作用域，Agent.run() 进入时设置、
结束时恢复。工具执行发生在 run 调用链内，天然可见；无作用域时
（单测直接调用工具、旧端未接线）各工具回退到原有 work_dir 行为。
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import contextvars


@dataclass(frozen=True)
class SessionScope:
    """一次 run 的路径作用域。

    - scratch: 会话工作目录（唯一默认可写区）
    - space_root: 空间根目录（None = 无空间模式）
    - path_space: 会话级派生 PathSpace（session:// + workspace:// 已按规则挂载）
    - record_write: 空间写入审计回调 (rel_path, op)，由 Agent.run 注入
    """

    scratch: Path
    space_root: Optional[Path] = None
    path_space: Optional[object] = None
    record_write: Optional[Callable[[str, str], None]] = None

    def in_space(self, path) -> bool:
        """路径是否位于空间子树内（无空间时恒 False）。"""
        if self.space_root is None:
            return False
        p = Path(str(path))
        sr = self.space_root
        return p == sr or sr in p.parents

    def in_scratch(self, path) -> bool:
        p = Path(str(path))
        return p == self.scratch or self.scratch in p.parents


_scope: contextvars.ContextVar[Optional[SessionScope]] = contextvars.ContextVar(
    "cagent_session_scope", default=None
)


def current_scope() -> Optional[SessionScope]:
    """当前线程/任务绑定的会话作用域；无则 None。"""
    return _scope.get()


@contextmanager
def enter_scope(scope: SessionScope):
    """在 with 块内激活会话作用域（可重入，退出后恢复）。"""
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
