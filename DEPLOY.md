# Trans MCP Server - 部署指南

## 架构说明

**新设计**：客户端传入 API Key，服务器不存储任何密钥

```
客户端 A (张三)                 客户端 B (李四)
    ↓                              ↓
Authorization: Bearer ft_aaa...   Authorization: Bearer ft_bbb...
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

**升级已经在跑的服务器**前先确认一件事：上游地址的默认值从测试环境改成了生产
`https://belindoc.com/api`。服务器的 `.env` 里如果没写 `BELINDOC_API_BASE_URL`，升级之后
就会打到生产。要继续用测试环境，升级前把它显式写进 `.env`。

### 3. 反向代理

服务监听 `127.0.0.1:8080`，由 nginx 挂到 `mcp.belindoc.com`：

```nginx
server {
    listen 80;
    server_name mcp.belindoc.com;

    location /mcp {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Streamable HTTP 的响应是 SSE 流。nginx 默认会把上游响应攒够一块再发，
        # 那样等待期间的进度通知会全部堵在代理里，客户端要么看不到进度、要么
        # 一次性收到一堆——必须关掉。
        proxy_buffering off;
        proxy_cache off;

        # 翻译任务等几分钟很正常，这期间连接上没有字节。默认 60s 会被反代
        # 判成超时掐断，wait_for_translation 那类工具就白等了。
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

改完 `nginx -t && systemctl reload nginx`，然后 `curl http://mcp.belindoc.com/health`
确认通了再配客户端。

只让 nginx 连得上的话，把服务的监听地址收到本机：在 `.env` 里设 `MCP_HOST=127.0.0.1`
（默认是 `0.0.0.0`，那样 `IP:8080` 也能直连，绕过反代）。

### 4. 本机配置

部署完成后，在客户端配置中添加：

```json
{
  "mcpServers": {
    "belindoc": {
      "type": "streamablehttp",
      "url": "http://mcp.belindoc.com/mcp",
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
url = "http://mcp.belindoc.com/mcp"
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
      "url": "http://mcp.belindoc.com/mcp",
      "headers": {
        "Authorization": "Bearer ft_aaa..."
      }
    }
  }
}

// 李四的配置
{
  "mcpServers": {
    "belindoc": {
      "url": "http://mcp.belindoc.com/mcp",
      "headers": {
        "Authorization": "Bearer ft_bbb..."
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

客户端 URL 相应改成 `http://mcp.belindoc.com/api/mcp`。

服务挂在独立子域名 `mcp.belindoc.com` 上，`/mcp` 不会被落地页占用，所以这个变量
一般用不着——它是留给「和落地页共用一个域名」那种部署的。

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
