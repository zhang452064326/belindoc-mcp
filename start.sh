#!/bin/bash
# Trans MCP Server 启动脚本

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Trans MCP Server ===${NC}"

# 检查环境变量
if [ -z "$BELINDOC_API_KEY" ]; then
    echo -e "${YELLOW}警告: 未设置 BELINDOC_API_KEY${NC}"
    if [ -f .env ]; then
        echo "从 .env 文件加载..."
        export $(cat .env | grep -v '^#' | xargs)
    else
        echo "请创建 .env 文件或设置环境变量 BELINDOC_API_KEY"
        exit 1
    fi
fi

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
echo "安装依赖..."
pip install -e . -q

# 启动服务
echo -e "${GREEN}启动 Trans MCP Server...${NC}"
trans-mcp
