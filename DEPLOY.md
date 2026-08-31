# Trans MCP Server - 部署指南

## 架构说明

**新设计**：客户端传入 API Key，服务器不存储任何密钥

```
客户端 A (张三)                 客户端 B (李四)
    ↓                              ↓
X-Api-Key: ft_kjo...         X-Api-Key: ft_abc...
    ↓                              ↓
    └──────────┬───────────────────┘
               ↓
         MCP 服务器
               ↓
    使用客户端传入的 Key 调用翻译 API
```

**优势**：
- ✅ 每个人用自己的 API Key
- ✅ 服务器不存储敏感信息
- ✅ 可以统计每个人的用量
- ✅ Key 泄露只影响个人

---

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

# 运行部署脚本
chmod +x deploy.sh
./deploy.sh
```

### 3. 本机配置

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

## 多用户使用

每个人在自己的 Codex 配置中填入自己的 API Key：

```json
// 张三的配置
{
  "mcpServers": {
    "belindoc": {
      "url": "http://server:8080/mcp",
      "headers": {
        "X-Api-Key": "ft_kjo..."
      }
    }
  }
}

// 李四的配置
{
  "mcpServers": {
    "belindoc": {
      "url": "http://server:8080/mcp",
      "headers": {
        "X-Api-Key": "ft_abc..."
      }
    }
  }
}
```

---

## 服务器管理

```bash
# 启动服务器
./server.sh start

# 停止服务器
./server.sh stop

# 查看状态
./server.sh status

# 重启服务器
./server.sh restart
```

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
| `.env` | 环境变量配置（只有端口等） |
