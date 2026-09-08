"""cagent Web 客户端：FastAPI 后端 + React 前端。"""
__all__ = ["create_app"]

def create_app():
    """延迟导入，避免未安装 web 依赖时报错。"""
    from .app import create_app as _create_app
    return _create_app()
