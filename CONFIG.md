# Trans MCP 环境变量速查

带注释的完整版在 [.env.example](.env.example)；工具清单和使用流程在 [README.md](README.md)。

| 变量 | 用在哪 | 默认 | 说明 |
|------|--------|------|------|
| `BELINDOC_API_KEY` | stdio | 无，必填 | `ft_` + 40 位随机串，共 43 字符 |
| `BELINDOC_API_BASE_URL` | 都 | `http://internal-test-host:6101`（测试环境） | 生产填 `https://belindoc.com/api` |
| `MCP_HOST` | HTTP | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | HTTP | `8080` | 监听端口 |
| `MCP_PATH` | HTTP | `/mcp` | MCP 服务端点路径。同域名下落地页占了 `/mcp` 时挪开，客户端 URL 要同步改 |
| `MCP_LOCALE` | 都 | `zh` | 用户可见文案的语言：`zh` / `zh-Hant` / `en` / `ja` / `ko` / `de` / `fr` / `ru` / `ar` |

两点容易踩：

- **HTTP 模式不读 `BELINDOC_API_KEY`。** 每个客户端自己带 `Authorization: Bearer <key>`，
  服务器不存任何密钥——别把真实 key 写进服务器的 `.env`。
- **`MCP_LOCALE` 只影响会被念给用户听的那部分**（任务状态、产出说明、进度行、失败原因、
  下载说明）。工具描述和给模型的操作指令始终是中文。工具参数里的 `locale` 优先级更高，
  本变量只是兜底。
