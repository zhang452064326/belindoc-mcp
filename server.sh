#!/bin/bash
# Trans MCP Server 管理脚本

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

case "$1" in
    start)
        echo -e "${GREEN}启动 Trans MCP Server...${NC}"
        cd /Users/zhangjun/project/trans_mcp
        source .venv/bin/activate
        BELINDOC_API_KEY="ft_REDACTED_KEY_ROTATED" MCP_PORT=8080 python -m trans_mcp.http_server 2>&1 &
        echo "服务器已在后台启动"
        echo "MCP 端点: http://localhost:8080/mcp"
        ;;
    stop)
        echo -e "${RED}停止 Trans MCP Server...${NC}"
        pkill -f "trans_mcp.http_server"
        echo "服务器已停止"
        ;;
    status)
        if pgrep -f "trans_mcp.http_server" > /dev/null; then
            echo -e "${GREEN}服务器正在运行${NC}"
            curl -s http://localhost:8080/health
        else
            echo -e "${RED}服务器未运行${NC}"
        fi
        ;;
    restart)
        $0 stop
        sleep 1
        $0 start
        ;;
    *)
        echo "用法: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
