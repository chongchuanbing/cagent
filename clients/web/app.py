"""FastAPI 应用：Web 客户端入口。"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routes import router
from .agent_service import AgentService


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化 AgentService。"""
    app.state.agent_service = AgentService()
    yield
    # 清理资源（如有需要）


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title="cagent Web",
        description="cagent Agent 框架的 Web 客户端",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS 配置（开发环境允许前端跨域）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册 API 路由
    app.include_router(router, prefix="/api")

    # 静态文件服务（生产环境）
    frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    assets_dir = os.path.join(frontend_dist, "assets")
    if os.path.exists(frontend_dist) and os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        async def serve_frontend(full_path: str):
            """前端 SPA 入口：所有非 API 路径返回 index.html。"""
            file_path = os.path.join(frontend_dist, full_path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
            return FileResponse(os.path.join(frontend_dist, "index.html"))

    return app


# 开发环境入口
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("clients.web.app:create_app", factory=True, host="0.0.0.0", port=8000, reload=True)
