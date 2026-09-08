"""Web 客户端 CLI 入口：启动 FastAPI 服务。"""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="cagent Web 客户端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式（热重载）")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("Error: Web 依赖未安装。请运行 `pip install -e .[web]`")
        sys.exit(1)

    uvicorn.run(
        "clients.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
