#!/bin/bash
# Trans MCP Server - Linux 部署脚本

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=== Trans MCP Server 部署脚本 ===${NC}"

# 配置
INSTALL_DIR="/opt/trans-mcp"
SERVICE_NAME="trans-mcp"
PORT=${1:-8080}
# 落地页可能已占用 /mcp，服务端点可以让开；用法: ./deploy-linux.sh [端口] [路径]
MCP_PATH=${2:-/mcp}

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未安装 Python3${NC}"
    exit 1
fi

# 创建安装目录
echo "创建安装目录: $INSTALL_DIR"
sudo mkdir -p $INSTALL_DIR
sudo chown $USER:$USER $INSTALL_DIR

# 复制项目文件（默认取脚本所在目录，可用 SRC_DIR 覆盖）
SRC_DIR="${SRC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
echo "复制项目文件: $SRC_DIR -> $INSTALL_DIR"
cp -r "$SRC_DIR"/* $INSTALL_DIR/
cd $INSTALL_DIR

# 创建虚拟环境
echo "创建虚拟环境..."
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
echo "安装依赖..."
pip install -e .

# 配置环境变量
if [ ! -f .env ]; then
    echo "创建 .env 文件..."
    cat > .env << EOF
# API Key 不在这里配：HTTP 模式下每个客户端自己带
# Authorization: Bearer <key>，服务器不存任何密钥
MCP_HOST=0.0.0.0
MCP_PORT=$PORT
MCP_PATH=$MCP_PATH
# 上游地址，不设即生产 https://belindoc.com/api；
# 要打到测试环境才取消下面这行的注释并填上地址
# BELINDOC_API_BASE_URL=http://内部测试机:6101
EOF
fi

# 创建 systemd 服务
echo "创建 systemd 服务..."
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=Trans MCP Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/trans-mcp-http
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启动服务
echo "启动服务..."
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME

echo -e "${GREEN}=== 部署完成 ===${NC}"
echo ""
echo "服务状态: sudo systemctl status $SERVICE_NAME"
echo "查看日志: sudo journalctl -u $SERVICE_NAME -f"
echo ""
IP=$(hostname -I | awk '{print $1}')
echo "MCP 端点: http://$IP:$PORT$MCP_PATH"
echo "健康检查: http://$IP:$PORT/health"
echo "对外地址: http://mcp.belindoc.com$MCP_PATH （经 nginx 反代，见 DEPLOY.md）"
echo ""
echo -e "${YELLOW}本机配置:${NC}"
cat << EOF
{
  "mcpServers": {
    "trans-mcp": {
      "type": "streamablehttp",
      "url": "http://mcp.belindoc.com$MCP_PATH",
      "headers": { "Authorization": "Bearer 你的API密钥" }
    }
  }
}
EOF
