"""统一 shell 执行器：超时、路径沙箱、输出截断、黑名单。"""
import os
import subprocess
from typing import Dict, List, Optional


class ShellExecutor:
    """安全执行 shell 命令。

    安全层级：
    1. 命令黑名单拦截（rm -rf /, sudo 等）
    2. 工作目录限制（cwd 必须在 work_dir 子树内）
    3. 超时控制（杀进程）
    4. 输出截断（stdout/stderr 各截断至 max_output_chars）
    5. 环境变量过滤（清除敏感变量）
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.work_dir = os.path.realpath(config.get("work_dir", "."))
        self.default_timeout = config.get("timeout", 30)
        self.max_timeout = config.get("max_timeout", 300)
        self.max_output_chars = config.get("max_output_chars", 2000)
        self.blocked_patterns: List[str] = config.get("blocked_patterns", [])
        self.blocked_env: List[str] = config.get("blocked_env", [])

    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> "ToolResult":
        """执行 shell 命令，返回 ToolResult。"""
        from ..tools.base import ToolResult

        if not command or not command.strip():
            return ToolResult(ok=False, content="", error="命令为空")

        # 1. 黑名单检查
        for pattern in self.blocked_patterns:
            if pattern in command:
                return ToolResult(
                    ok=False, content="", error=f"命令被黑名单拦截: 包含 '{pattern}'"
                )

        # 2. 超时限制
        effective_timeout = min(timeout or self.default_timeout, self.max_timeout)

        # 3. 工作目录限制
        effective_cwd = os.path.realpath(cwd or self.work_dir)
        if not effective_cwd.startswith(self.work_dir + os.sep) and effective_cwd != self.work_dir:
            return ToolResult(
                ok=False, content="", error=f"工作目录越界: {cwd} 不在 {self.work_dir} 内"
            )

        # 4. 环境变量过滤
        env = dict(os.environ)
        for key in self.blocked_env:
            env.pop(key, None)

        # 5. 执行
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                cwd=effective_cwd,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                content="",
                error=f"命令超时（{effective_timeout}s）: {command[:100]}",
            )
        except Exception as e:
            return ToolResult(ok=False, content="", error=f"执行异常: {e}")

        # 6. 截断输出
        stdout = self._truncate(result.stdout or "")
        stderr = self._truncate(result.stderr or "")
        content = f"[exit_code={result.returncode}]\nstdout:\n{stdout}"
        if stderr:
            content += f"\nstderr:\n{stderr}"
        return ToolResult(
            ok=result.returncode == 0,
            content=content,
            error=None if result.returncode == 0 else f"退出码 {result.returncode}",
        )

    def _truncate(self, text: str) -> str:
        """截断输出至 max_output_chars。"""
        if len(text) > self.max_output_chars:
            return text[: self.max_output_chars] + f"\n...(已截断，共 {len(text)} 字符)"
        return text

    def render_command(self, template: str, params: dict) -> str:
        """将参数填入命令模板。

        简单变量替换：{name} → params["name"]
        条件变量：{xxx_opt} → 由 params["xxx"] 决定生成什么
        """
        rendered = template
        for key, value in params.items():
            if key.endswith("_opt"):
                base_key = key[:-4]
                if params.get(base_key):
                    rendered = rendered.replace(f"{{{key}}}", str(value))
                else:
                    rendered = rendered.replace(f"{{{key}}}", "")
            elif value is not None:
                rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered
