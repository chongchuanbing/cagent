#!/bin/bash

# cagent 本地开发环境停止脚本
# 停止所有服务并清理 PID 文件

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_DIR="$PROJECT_ROOT/.pids"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  cagent 本地开发环境停止${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 检查 PID 目录
if [ ! -d "$PID_DIR" ]; then
    echo -e "${YELLOW}没有找到运行中的服务${NC}"
    exit 0
fi

STOPPED=0

# 停止后端服务
if [ -f "$PID_DIR/backend.pid" ]; then
    PID=$(cat "$PID_DIR/backend.pid")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo -e "${GREEN}停止后端 API 服务 (PID: $PID)...${NC}"
        kill "$PID" 2>/dev/null || true
        sleep 1
        # 如果还在运行，强制杀死
        if ps -p "$PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}  强制停止...${NC}"
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo -e "${GREEN}  ✓ 后端服务已停止${NC}"
        STOPPED=$((STOPPED + 1))
    else
        echo -e "${YELLOW}后端服务已不在运行 (PID: $PID)${NC}"
    fi
    rm -f "$PID_DIR/backend.pid"
fi

# 停止前端服务
if [ -f "$PID_DIR/frontend.pid" ]; then
    PID=$(cat "$PID_DIR/frontend.pid")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo -e "${GREEN}停止前端开发服务器 (PID: $PID)...${NC}"
        # 停止进程组（包括子进程）
        pkill -P "$PID" 2>/dev/null || true
        kill "$PID" 2>/dev/null || true
        sleep 1
        # 如果还在运行，强制杀死
        if ps -p "$PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}  强制停止...${NC}"
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo -e "${GREEN}  ✓ 前端服务已停止${NC}"
        STOPPED=$((STOPPED + 1))
    else
        echo -e "${YELLOW}前端服务已不在运行 (PID: $PID)${NC}"
    fi
    rm -f "$PID_DIR/frontend.pid"
fi

# 停止桌面服务
if [ -f "$PID_DIR/desktop.pid" ]; then
    PID=$(cat "$PID_DIR/desktop.pid")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo -e "${GREEN}停止 Tauri 桌面应用 (PID: $PID)...${NC}"
        # 停止进程组（包括子进程）
        pkill -P "$PID" 2>/dev/null || true
        kill "$PID" 2>/dev/null || true
        sleep 2
        # 如果还在运行，强制杀死
        if ps -p "$PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}  强制停止...${NC}"
            kill -9 "$PID" 2>/dev/null || true
        fi
        echo -e "${GREEN}  ✓ 桌面应用已停止${NC}"
        STOPPED=$((STOPPED + 1))
    else
        echo -e "${YELLOW}桌面应用已不在运行 (PID: $PID)${NC}"
    fi
    rm -f "$PID_DIR/desktop.pid"
fi

# 清理空的 PID 目录
if [ -d "$PID_DIR" ] && [ -z "$(ls -A "$PID_DIR")" ]; then
    rmdir "$PID_DIR" 2>/dev/null || true
fi

echo ""
if [ $STOPPED -gt 0 ]; then
    echo -e "${BLUE}========================================${NC}"
    echo -e "${GREEN}  已停止 $STOPPED 个服务${NC}"
    echo -e "${BLUE}========================================${NC}"
else
    echo -e "${YELLOW}没有找到运行中的服务${NC}"
fi
echo ""
