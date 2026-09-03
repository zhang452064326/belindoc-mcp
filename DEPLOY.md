# Trans MCP Server - 部署指南

## 架构说明

**新设计**：客户端传入 API Key，服务器不存储任何密钥

```
客户端 A (张三)                 客户端 B (李四)
    ↓                              ↓
Authorization: Bearer ft_kjo...   Authorization: Bearer ft_abc...
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

API Key 是每个请求各带各的：同一条会话里换了 Bearer，后面的调用就走新的那把 key，
服务端不会把握手时那一把记下来复用。

---

## 传输方式

服务端跑的是标准 **Streamable HTTP**（SSE 响应体 + `Mcp-Session-Id` + 客户端把答复
POST 回来），不是一问一答的 JSON-RPC。这不只是协议合规问题，两件正事靠它：

- **扣费确认能问到人**。视频翻译、字幕改写要真扣额度，服务端通过 elicitation 直接
  向用户弹窗确认。没有回传通道时只能退回 `user_confirmed` 这类布尔量——那永远是
  模型自己填的，服务端无法验证背后到底有没有问过。
- **等待期间有进度**。等一个视频要几分钟，`notifications/progress` 是这中间唯一能
  出声的通道。

想确认某个客户端到底吃不吃 elicitation，调一次 `probe_elicitation`：它不翻译、不
提交任务、不扣额度，只把客户端声明的能力报出来，并在支持时真弹一次提问。

早期版本上的 `/sse` 和 `/message` 两个端点已删除——那是上一版手搓传输的残骸，
`/sse` 自己伪造了一条 initialize 然后挂着不动，任何标准 MCP 客户端都握不上手。
现在只有两个端点：MCP 端点（默认 `/mcp`）和 `/health`。

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

部署完成后，在客户端配置中添加：

```json
{
  "mcpServers": {
    "belindoc": {
      "type": "streamablehttp",
      "url": "http://YOUR_SERVER_IP:8080/mcp",
      "headers": {
        "Authorization": "Bearer 你的API密钥"
      }
    }
  }
}
```

### 认证方式

统一使用标准的 `Authorization: Bearer <key>` 请求头。

Codex CLI 的 HTTP MCP 无法发送自定义请求头，只能用 Bearer；
若走 `~/.codex/config.toml`，写法为：

```toml
[mcp_servers.belindoc]
url = "http://YOUR_SERVER_IP:8080/mcp"
bearer_token_env_var = "BELINDOC_API_KEY"
```

---

## 获取 API Key

1. 登录 https://belindoc.com
2. 进入「开放平台」→「API Key 管理」
3. 创建新的 API Key（格式：`ft_` + 40位随机串）

---

## 多用户使用

每个人在自己的客户端配置中填入自己的 API Key：

```json
// 张三的配置
{
  "mcpServers": {
    "belindoc": {
      "url": "http://server:8080/mcp",
      "headers": {
        "Authorization": "Bearer ft_kjo..."
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
        "Authorization": "Bearer ft_abc..."
      }
    }
  }
}
```

---

## 端点路径

服务端点默认是 `/mcp`。如果同域名下落地页已经占用了 `/mcp`，用 `MCP_PATH` 挪开：

```bash
MCP_PATH=/api/mcp ./server.sh start
```

客户端 URL 相应改成 `http://YOUR_SERVER_IP:8080/api/mcp`。
更省事的做法是把服务放到独立子域名（如 `mcp.example.com/mcp`），不用改任何配置。

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
