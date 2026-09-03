#!/bin/bash
# Trans MCP Server - 一键部署脚本
# 用法: ./deploy.sh [端口号]

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PORT=${1:-8080}
# 落地页可能已占用 /mcp，服务端点可以让开；用法: ./deploy.sh [端口] [路径]
MCP_PATH=${2:-/mcp}
INSTALL_DIR="/opt/trans-mcp"

echo -e "${GREEN}=== Trans MCP Server 部署 ===${NC}"

# 1. 安装 Docker（如果没有）
if ! command -v docker &> /dev/null; then
    echo "安装 Docker..."
    curl -fsSL https://get.docker.com | sh
fi

# 2. 创建安装目录
echo "创建安装目录..."
sudo mkdir -p $INSTALL_DIR
cd $INSTALL_DIR

# 3. 解压部署包（如果存在）
if [ -f /tmp/trans-mcp-deploy.tar.gz ]; then
    echo "解压部署包..."
    sudo tar xzf /tmp/trans-mcp-deploy.tar.gz -C $INSTALL_DIR
fi

# 4. 配置环境变量
if [ ! -f .env ]; then
    echo "创建配置文件..."
    
    cat > .env << EOF
# Trans MCP Server 配置
MCP_HOST=0.0.0.0
MCP_PORT=$PORT
MCP_PATH=$MCP_PATH
# 上游地址，不设即测试环境；生产环境取消下面这行的注释
# BELINDOC_API_BASE_URL=https://belindoc.com/api
EOF
    
    echo -e "${GREEN}配置完成${NC}"
fi

# 5. 构建并启动
echo "构建 Docker 镜像..."
docker build -t trans-mcp .

echo "启动服务..."
docker-compose up -d

# 6. 检查状态
sleep 3
echo -e "${GREEN}=== 部署完成 ===${NC}"
echo ""
echo "服务状态:"
docker-compose ps
echo ""
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost')
echo "MCP 端点: http://$IP:$PORT$MCP_PATH"
echo "健康检查: http://$IP:$PORT/health"
echo ""
echo -e "${YELLOW}本机配置:${NC}"
cat << EOF
{
  "mcpServers": {
    "trans-mcp": {
      "type": "streamablehttp",
      "url": "http://YOUR_SERVER_IP:$PORT$MCP_PATH",
      "headers": { "Authorization": "Bearer 你的API密钥" }
    }
  }
}
EOF
