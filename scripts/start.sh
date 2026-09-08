#!/bin/bash

# cagent 本地开发环境启动脚本
# 启动所有服务：后端 API、前端、桌面应用（可选）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_DIR="$PROJECT_ROOT/.pids"
LOG_DIR="$PROJECT_ROOT/logs"

# 创建目录
mkdir -p "$PID_DIR" "$LOG_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  cagent 本地开发环境启动${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到 python3${NC}"
    exit 1
fi

# 检查 Node.js
if ! command -v node &> /dev/null; then
    echo -e "${RED}错误: 未找到 node${NC}"
    exit 1
fi

# 检查依赖是否安装
if [ ! -d "$PROJECT_ROOT/clients/web/frontend/node_modules" ]; then
    echo -e "${YELLOW}首次运行，安装前端依赖...${NC}"
    cd "$PROJECT_ROOT/clients/web/frontend"
    npm install
    cd "$PROJECT_ROOT"
fi

# 检查 Rust (Tauri 需要)
HAS_RUST=false
if command -v cargo &> /dev/null; then
    HAS_RUST=true
    if [ ! -d "$PROJECT_ROOT/clients/desktop/node_modules" ]; then
        echo -e "${YELLOW}安装 Tauri CLI 依赖...${NC}"
        cd "$PROJECT_ROOT/clients/desktop"
        npm install
        cd "$PROJECT_ROOT"
    fi
fi

# 启动后端 API 服务
echo -e "${GREEN}[1/3] 启动后端 API 服务...${NC}"
cd "$PROJECT_ROOT"

# 清理旧进程
if [ -f "$PID_DIR/backend.pid" ]; then
    OLD_PID=$(cat "$PID_DIR/backend.pid")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${YELLOW}  停止旧的后端服务 (PID: $OLD_PID)${NC}"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_DIR/backend.pid"
fi

# 启动新进程
nohup python3 -m uvicorn clients.web.app:create_app --factory --reload --host 127.0.0.1 --port 8000 > "$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$PID_DIR/backend.pid"
echo -e "${GREEN}  ✓ 后端 API 服务已启动 (PID: $BACKEND_PID)${NC}"
echo -e "${BLUE}    地址: http://127.0.0.1:8000${NC}"
echo -e "${BLUE}    日志: logs/backend.log${NC}"

# 等待后端启动
sleep 2

# 启动前端开发服务器
echo -e "${GREEN}[2/3] 启动前端开发服务器...${NC}"
cd "$PROJECT_ROOT/clients/web/frontend"

# 清理旧进程
if [ -f "$PID_DIR/frontend.pid" ]; then
    OLD_PID=$(cat "$PID_DIR/frontend.pid")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${YELLOW}  停止旧的前端服务 (PID: $OLD_PID)${NC}"
        pkill -P "$OLD_PID" 2>/dev/null || true
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_DIR/frontend.pid"
fi

# 启动新进程
nohup npm run dev > "$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "$FRONTEND_PID" > "$PID_DIR/frontend.pid"
echo -e "${GREEN}  ✓ 前端开发服务器已启动 (PID: $FRONTEND_PID)${NC}"
echo -e "${BLUE}    地址: http://localhost:5173${NC}"
echo -e "${BLUE}    日志: logs/frontend.log${NC}"

# 启动 Tauri 桌面应用（可选）
if [ "$HAS_RUST" = true ]; then
    echo -e "${GREEN}[3/3] 启动 Tauri 桌面应用...${NC}"
    cd "$PROJECT_ROOT/clients/desktop"
    
    # 清理旧进程
    if [ -f "$PID_DIR/desktop.pid" ]; then
        OLD_PID=$(cat "$PID_DIR/desktop.pid")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}  停止旧的桌面应用 (PID: $OLD_PID)${NC}"
            pkill -P "$OLD_PID" 2>/dev/null || true
            kill "$OLD_PID" 2>/dev/null || true
            sleep 2
        fi
        rm -f "$PID_DIR/desktop.pid"
    fi
    
    # 启动新进程
    nohup npm run tauri dev > "$LOG_DIR/desktop.log" 2>&1 &
    DESKTOP_PID=$!
    echo "$DESKTOP_PID" > "$PID_DIR/desktop.pid"
    echo -e "${GREEN}  ✓ Tauri 桌面应用已启动 (PID: $DESKTOP_PID)${NC}"
    echo -e "${BLUE}    日志: logs/desktop.log${NC}"
else
    echo -e "${YELLOW}[3/3] 跳过 Tauri 桌面应用 (未安装 Rust)${NC}"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  所有服务已启动完成${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}服务访问地址:${NC}"
echo -e "  ${YELLOW}后端 API:${NC}      http://localhost:8000"
echo -e "  ${YELLOW}前端页面:${NC}      http://localhost:5173"
echo -e "  ${YELLOW}API 文档:${NC}      http://localhost:8000/docs"
echo ""
echo -e "${BLUE}日志目录:${NC} $LOG_DIR/"
echo -e "${BLUE}PID 目录:${NC} $PID_DIR/"
echo ""
echo -e "${YELLOW}查看状态: ./scripts/status.sh${NC}"
echo -e "${YELLOW}停止所有服务: ./scripts/stop.sh${NC}"
echo ""
