# Trans MCP Server - 部署指南

## 部署步骤

### 1. 上传部署包到服务器

```bash
scp ~/Desktop/trans-mcp-deploy.tar.gz user@your-server:/tmp/
```

### 2. 在服务器上执行

```bash
# SSH 登录
ssh user@your-server

# 创建安装目录
sudo mkdir -p /opt/trans-mcp
cd /opt/trans-mcp

# 解压部署包
sudo tar xzf /tmp/trans-mcp-deploy.tar.gz .

# 运行部署脚本（会提示配置 API Key）
chmod +x deploy.sh
./deploy.sh
```

### 3. 配置 API Key

部署脚本会提示你编辑 `.env` 文件：

```bash
vi /opt/trans-mcp/.env
```

填入你的 API Key：

```env
BELINDOC_API_KEY=你的API密钥
MCP_HOST=0.0.0.0
MCP_PORT=8080
```

### 4. 重新运行部署脚本

```bash
./deploy.sh
```

### 5. 本机配置

部署完成后，在 Codex 配置中添加：

```json
{
  "mcpServers": {
    "belindoc": {
      "type": "streamablehttp",
      "url": "http://YOUR_SERVER_IP:8080/mcp",
      "headers": {
        "X-Api-Key": "你的API密钥"
      }
    }
  }
}
```

---

## 获取 API Key

1. 登录 https://belindoc.com
2. 进入「开放平台」→「API Key 管理」
3. 创建新的 API Key（格式：`ft_` + 40位随机串）

---

## 本地测试

本地 MCP 服务器正在运行：

```bash
http://localhost:8080/mcp
```

配置文件：`~/.codex/config.json`

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `deploy.sh` | 部署脚本 |
| `server.sh` | 服务器管理脚本 |
| `DEPLOY.md` | 部署文档 |
| `.env` | 环境变量配置（需要用户填写） |
