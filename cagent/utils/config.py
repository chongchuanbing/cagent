"""配置加载。"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """运行时配置（后续可由 yaml / env 覆盖）。"""

    model: str = "gpt-4o"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_steps: int = 20
    react_max_iterations: int = 5
    # 本地文件存储根目录（默认项目下 .data）
    data_dir: str = ".data"


def load_config(path: Optional[str] = None) -> Config:
    """从文件或环境变量加载配置，占位实现。"""
    # TODO: 读取 yaml / .env，覆盖默认 Config
    return Config()
