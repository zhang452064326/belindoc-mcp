#!/bin/bash
# Trans MCP Server 管理脚本

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 脚本所在目录（避免硬编码路径）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

PORT="${MCP_PORT:-8080}"
LOG="$DIR/server.log"
PATTERN="trans_mcp.http_server"

is_running() {
    pgrep -f "$PATTERN" > /dev/null 2>&1
}

wait_gone() {
    # 轮询等进程真正消失，参数为最多等多少个 0.1 秒；退干净返回 0
    local waited=0
    while is_running && [ "$waited" -lt "$1" ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    ! is_running
}

ensure_venv() {
    if [ ! -x ".venv/bin/python" ]; then
        echo -e "${YELLOW}创建虚拟环境...${NC}"
        # 项目要求 Python >= 3.10，系统 python3 可能过旧
        PY=""
        for c in python3.13 python3.12 python3.11 python3.10 python3; do
            if command -v "$c" > /dev/null 2>&1 && \
               "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
                PY="$c"
                break
            fi
        done
        if [ -z "$PY" ]; then
            echo -e "${RED}未找到 Python 3.10+，请先安装${NC}"
            exit 1
        fi
        "$PY" -m venv .venv || exit 1
        .venv/bin/pip install -q --upgrade pip
        .venv/bin/pip install -q -e . || exit 1
    fi
}

case "$1" in
    start)
        if is_running; then
            echo -e "${YELLOW}服务器已在运行${NC}"
            exit 0
        fi
        echo -e "${GREEN}启动 Trans MCP Server...${NC}"
        ensure_venv
        # 可选：从 .env 读取配置（API Key 由客户端传入，非必填）
        [ -f .env ] && export $(grep -v '^#' .env | grep -v '^$' | xargs)
        MCP_PORT="$PORT" nohup .venv/bin/python -m trans_mcp.http_server > "$LOG" 2>&1 &
        sleep 1
        if is_running; then
            echo "服务器已在后台启动 (日志: $LOG)"
            echo "MCP 端点: http://localhost:$PORT${MCP_PATH:-/mcp}"
        else
            echo -e "${RED}启动失败，最后几行日志:${NC}"
            tail -n 20 "$LOG"
            exit 1
        fi
        ;;
    stop)
        if ! is_running; then
            echo -e "${YELLOW}服务器未在运行${NC}"
            exit 0
        fi
        echo -e "${RED}停止 Trans MCP Server...${NC}"
        pkill -f "$PATTERN"
        # pkill 发完信号就返回，进程还要一会儿才真正退出。不等它退干净，
        # 紧接着的 start 会撞见旧进程、判定「服务器已在运行」直接跳过启动，
        # restart 就成了空转（只停不起）。
        if ! wait_gone 100; then          # 10 秒
            echo -e "${YELLOW}10 秒未退出，强制结束${NC}"
            pkill -9 -f "$PATTERN"
            wait_gone 30 > /dev/null      # 3 秒
        fi
        if is_running; then
            echo -e "${RED}进程仍未退出，请手动检查: pgrep -lf '$PATTERN'${NC}"
            exit 1
        fi
        echo "服务器已停止"
        ;;
    status)
        if is_running; then
            echo -e "${GREEN}服务器正在运行${NC}"
            curl -s "http://localhost:$PORT/health"
            echo
        else
            echo -e "${RED}服务器未运行${NC}"
        fi
        ;;
    restart)
        $0 stop || exit 1
        $0 start
        ;;
    logs)
        tail -f "$LOG"
        ;;
    *)
        echo "用法: $0 {start|stop|status|restart|logs}"
        exit 1
        ;;
esac
