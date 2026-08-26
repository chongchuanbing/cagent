"""命令行客户端。"""
from .main import main as cli_main, build_parser

# 注意：不要把 `main` 直接挂到包命名空间，否则会与同名子模块 `clients.cli.main`
# 冲突（import clients.cli.main as cli 会误绑到函数而非模块）。
__all__ = ["cli_main", "build_parser"]
