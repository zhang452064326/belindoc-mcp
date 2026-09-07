# Trans MCP 客户端配置速查

工具清单、流程和环境变量看 [README.md](README.md)；服务器部署看 [DEPLOY.md](DEPLOY.md)。

## stdio（服务跑在自己机器上）

服务端从 `BELINDOC_API_KEY` 读密钥。

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`
**Codex** — `~/.codex/config.json`

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "uvx",
      "args": ["belindoc-mcp"],
      "env": {
        "BELINDOC_API_KEY": "ft_你的API密钥"
      }
    }
  }
}
```

`uvx` 自己拉包、自己建隔离环境，不用预装、不用管路径。前提是机器上有 uv
（`curl -LsSf https://astral.sh/uv/install.sh | sh`）。

从源码装的话，`command` 改填 `pip install -e .` 之后 venv 里 `belindoc-mcp` 的**绝对路径**
（客户端不走登录 shell，`PATH` 里没有这个 venv，裸命令名起不来）。

也可以把变量放进项目根目录的 `.env`，启动时自己加载：

```bash
cp .env.example .env   # 填入 API Key
source .env && belindoc-mcp
```

## HTTP 远程（多人共用一台服务器）

服务器不存任何密钥，每个人在自己的客户端里带自己的 key。

```json
{
  "mcpServers": {
    "belindoc": {
      "type": "streamablehttp",
      "url": "http://mcp.belindoc.com/mcp",
      "headers": {
        "Authorization": "Bearer ft_你的API密钥"
      }
    }
  }
}
```

Codex CLI 的 HTTP MCP 发不了自定义请求头，只能用 Bearer；走 `~/.codex/config.toml`：

```toml
[mcp_servers.belindoc]
url = "http://mcp.belindoc.com/mcp"
bearer_token_env_var = "BELINDOC_API_KEY"
```

服务端点默认 `/mcp`。服务器上设了 `MCP_PATH` 的话，这里的 URL 要跟着改。

## 接上之后

调一次 `get_account_status` 验证密钥通不通，顺便看余额。
想知道这个客户端支不支持服务端弹窗确认（关系到视频提交要走一步还是两步），
调一次 `probe_elicitation`——它不翻译、不提交任务、不扣额度。
