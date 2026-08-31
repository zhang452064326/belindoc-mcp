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

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未安装 Python3${NC}"
    exit 1
fi

# 创建安装目录
echo "创建安装目录: $INSTALL_DIR"
sudo mkdir -p $INSTALL_DIR
sudo chown $USER:$USER $INSTALL_DIR

# 复制项目文件
echo "复制项目文件..."
cp -r /Users/zhangjun/project/trans_mcp/* $INSTALL_DIR/
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
BELINDOC_API_KEY=ft_REDACTED_KEY_ROTATED
MCP_HOST=0.0.0.0
MCP_PORT=$PORT
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
echo "MCP Server 地址: http://$(hostname -I | awk '{print $1}'):$PORT"
echo "SSE 端点: http://$(hostname -I | awk '{print $1}'):$PORT/sse"
echo "消息端点: http://$(hostname -I | awk '{print $1}'):$PORT/message"
echo ""
echo -e "${YELLOW}本机配置:${NC}"
cat << EOF
{
  "mcpServers": {
    "trans-mcp": {
      "url": "http://YOUR_SERVER_IP:$PORT/sse"
    }
  }
}
EOF
