# Trans MCP 集成配置

## Claude Desktop 配置

配置文件位置：`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "/Users/zhangjun/project/trans_mcp/.venv/bin/trans-mcp",
      "env": {
        "BELINDOC_API_KEY": "ft_你的API密钥"
      }
    }
  }
}
```

## Codex 配置

配置文件位置：`~/.codex/config.json`

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "/Users/zhangjun/project/trans_mcp/.venv/bin/trans-mcp",
      "env": {
        "BELINDOC_API_KEY": "ft_你的API密钥"
      }
    }
  }
}
```

## 环境变量方式

在 `.env` 文件中配置：
```bash
BELINDOC_API_KEY=ft_你的API密钥
```

然后启动时加载：
```bash
source .env && trans-mcp
```
