#!/bin/bash

# cagent 本地开发环境状态查看脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_DIR="$PROJECT_ROOT/.pids"

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  cagent 服务状态${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 检查端口是否被监听
check_port() {
    local port=$1
    lsof -i :$port > /dev/null 2>&1
}

# 检查 PID 文件中的进程是否存活
check_pid() {
    local pidfile=$1
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile")
        ps -p "$pid" > /dev/null 2>&1
    else
        return 1
    fi
}

# 通过进程名查找 PID
find_pid() {
    local pattern=$1
    pgrep -f "$pattern" 2>/dev/null | head -1
}

RUNNING=0

# 检查后端服务（端口 8000）
if check_port 8000; then
    PID=$(find_pid "uvicorn clients.web")
    echo -e "${GREEN}✓ 后端 API 服务${NC}      运行中 (PID: ${PID:-?})"
    echo -e "  地址: http://localhost:8000"
    echo -e "  日志: logs/backend.log"
    RUNNING=$((RUNNING + 1))
elif check_pid "$PID_DIR/backend.pid"; then
    PID=$(cat "$PID_DIR/backend.pid")
    echo -e "${YELLOW}⚠ 后端 API 服务${NC}      PID 文件存在但端口未监听 (PID: $PID)"
else
    echo -e "${RED}✗ 后端 API 服务${NC}      未启动"
fi
echo ""

# 检查前端服务（端口 5173）
if check_port 5173; then
    PID=$(find_pid "vite")
    echo -e "${GREEN}✓ 前端开发服务器${NC}    运行中 (PID: ${PID:-?})"
    echo -e "  地址: http://localhost:5173"
    echo -e "  日志: logs/frontend.log"
    RUNNING=$((RUNNING + 1))
elif check_pid "$PID_DIR/frontend.pid"; then
    PID=$(cat "$PID_DIR/frontend.pid")
    echo -e "${YELLOW}⚠ 前端服务${NC}      PID 文件存在但端口未监听 (PID: $PID)"
else
    echo -e "${RED}✗ 前端开发服务器${NC}    未启动"
fi
echo ""

# 检查桌面服务
if check_pid "$PID_DIR/desktop.pid"; then
    PID=$(cat "$PID_DIR/desktop.pid")
    echo -e "${GREEN}✓ Tauri 桌面应用${NC}      运行中 (PID: $PID)"
    echo -e "  日志: logs/desktop.log"
    RUNNING=$((RUNNING + 1))
else
    echo -e "${YELLOW}○ Tauri 桌面应用${NC}      未启动"
fi
echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "服务状态: ${GREEN}$RUNNING${NC} 个端口在监听"
echo -e "${BLUE}========================================${NC}"
echo ""

if [ $RUNNING -gt 0 ]; then
    echo -e "${YELLOW}停止服务: ./scripts/stop.sh${NC}"
else
    echo -e "${YELLOW}启动服务: ./scripts/start.sh${NC}"
fi
echo ""