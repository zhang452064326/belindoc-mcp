# Trans MCP Server

Belindoc 翻译开放 API 的 MCP 服务：文档（PDF / Word / Excel / Markdown / 图片）和视频翻译、
字幕改写。

## 两种运行方式

| | stdio | HTTP 远程 |
|---|---|---|
| 入口 | `trans-mcp` | `trans-mcp-http` |
| 跑在哪 | 用户自己的机器上 | 一台服务器上，多人共用 |
| API Key | 服务端从 `BELINDOC_API_KEY` 读 | 每个客户端自己带 `Authorization: Bearer <key>`，服务器不存任何密钥 |
| 传输 | stdio | Streamable HTTP（SSE + `Mcp-Session-Id`） |

两种方式的工具、行为完全一致，包括服务端直接向用户弹窗确认（elicitation）和等待期间的
进度通知。部署 HTTP 模式看 [DEPLOY.md](DEPLOY.md)。

## 安装

```bash
cd /path/to/trans-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 环境变量

| 变量 | 用在哪 | 说明 |
|------|--------|------|
| `BELINDOC_API_KEY` | stdio | 必需。格式 `ft_` + 40 位随机串，共 43 字符 |
| `BELINDOC_API_BASE_URL` | 都 | 上游地址。不设即测试环境 `http://internal-test-host:6101`；生产填 `https://belindoc.com/api` |
| `MCP_HOST` / `MCP_PORT` | HTTP | 监听地址与端口，默认 `0.0.0.0:8080` |
| `MCP_PATH` | HTTP | MCP 服务端点路径，默认 `/mcp`。同域名下落地页占了 `/mcp` 时挪开 |
| `MCP_LOCALE` | 都 | 用户可见文案的语言，默认 `zh`。见下方「输出语言」 |

HTTP 模式**不读** `BELINDOC_API_KEY`——别把真实 key 写进服务器的 `.env`。
完整注释见 [.env.example](.env.example)。

### 获取 API Key

登录 https://belindoc.com → 「开放平台」→「API Key 管理」→ 创建。

## 客户端接入

### stdio

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "/path/to/trans-mcp/.venv/bin/trans-mcp",
      "env": {
        "BELINDOC_API_KEY": "ft_你的API密钥"
      }
    }
  }
}
```

配置文件位置：Claude Desktop 是 `~/Library/Application Support/Claude/claude_desktop_config.json`，
Codex 是 `~/.codex/config.json`。

`command` 必须是绝对路径——上面「安装」那步 `pip install -e .` 之后，venv 里会生成
`trans-mcp` 这个可执行文件，填它的完整路径（形如 `/path/to/trans-mcp/.venv/bin/trans-mcp`）。
客户端不走登录 shell，`PATH` 里通常没有这个 venv，写裸命令名会起不来。

不想把 key 写进客户端配置的话，也可以放进项目根目录的 `.env`，启动时自己加载：

```bash
cp .env.example .env   # 填入 API Key
source .env && trans-mcp
```

### HTTP 远程

```json
{
  "mcpServers": {
    "belindoc": {
      "type": "streamablehttp",
      "url": "http://YOUR_SERVER_IP:8080/mcp",
      "headers": {
        "Authorization": "Bearer ft_你的API密钥"
      }
    }
  }
}
```

Codex CLI 的 HTTP MCP 发不了自定义请求头，只能用 Bearer；走 `~/.codex/config.toml` 的话：

```toml
[mcp_servers.belindoc]
url = "http://YOUR_SERVER_IP:8080/mcp"
bearer_token_env_var = "BELINDOC_API_KEY"
```

### 接上之后

调一次 `get_account_status` 验证密钥通不通，顺便看余额。想知道这个客户端支不支持服务端
弹窗确认（关系到视频提交走一步还是两步），调一次 `probe_elicitation`——它不翻译、不提交
任务、不扣额度。

## 典型流程

**文档**：`upload_document` 取预签名链接 → 按返回的 `uploadCommand` 上传 →
（PDF 才要）`check_pdf_ocr` 看是不是扫描件 → `translate_document` 提交 →
`wait_for_translation` 跟进 → `get_document_translation_result` 取下载链接。

**视频**：`upload_video` → 上传 → `calculate_video_translation_quota` 试算 →
`translate_video` 提交（两步确认，见下）→ `wait_for_video_translation` 跟进。
想改字幕重出一版：`get_video_subtitles` → `calculate_rewrite_quota` →
`rewrite_video_subtitles` → `get_video_rewrite_status`。

上传由调用方自己执行返回的 `uploadCommand`，服务端不碰用户机器上的文件；下载给的是
签名链接，问号后面的签名参数一个字符都不能改，截掉就是 403。

## 工具列表

### 账户与元信息
| 工具 | 说明 |
|------|------|
| `get_supported_languages` | 支持的语言列表（79 种，语言码 → 显示名） |
| `get_model_list` | 当前账户可用的翻译模型 |
| `get_account_status` | 可用额度、会员档位、各项限额（单视频时长 / 并发数 / 单文件大小） |

### 文档翻译
| 工具 | 说明 |
|------|------|
| `upload_document` | 取文档的预签名上传链接 |
| `check_pdf_ocr` | 判断已上传的 PDF 是不是扫描件 / 双层 PDF |
| `translate_document` | 提交文档翻译任务 |
| `wait_for_translation` | 等待任务完成，进度一有变化就返回 |
| `get_document_translation_status` | 查单个任务状态 |
| `get_document_translation_result` | 取译文下载链接 |
| `list_document_translations` | 分页查任务列表 |
| `get_document_translation_by_batch` | 按批次号查任务 |

### 视频翻译
| 工具 | 说明 |
|------|------|
| `upload_video` | 取视频的预签名上传地址 |
| `calculate_video_translation_quota` | 试算要花多少额度，不扣费 |
| `translate_video` | 提交视频翻译任务（会真扣额度，两步确认） |
| `wait_for_video_translation` | 等待任务完成，进度一有变化就返回 |
| `get_video_translation_status` | 查单个任务状态 |
| `list_video_translations` | 分页查任务列表（只有最近 15 天） |
| `cancel_video_translation` | 取消任务 |

### 字幕改写
| 工具 | 说明 |
|------|------|
| `get_video_subtitles` | 取原文与译文字幕下载地址 |
| `calculate_rewrite_quota` | 试算改写要花多少额度，不扣费 |
| `rewrite_video_subtitles` | 用编辑后的字幕重新生成视频（会真扣额度，两步确认） |
| `get_video_rewrite_status` | 查改写进度 |

### 排查
| 工具 | 说明 |
|------|------|
| `probe_elicitation` | 自检：这个客户端到底吃不吃 elicitation。不翻译、不提交、不扣额度 |

## 扣费确认

`translate_video` 和 `rewrite_video_subtitles` 会真扣额度，所以提交是**两步**，第一次
一定不会提交：

- 客户端支持 **elicitation** 时，服务端直接弹窗问用户，一次调用即可；
- 不支持时退回**确认码**：第一次调用返回 409 + 一段给用户看的话 + 一张菜单（配音 ×
  字幕的各种组合，每格自带额度和 `confirmToken`），把菜单原样给用户看、他挑了哪一项，
  就用那一项的 `confirmToken` 重调一次，这一次才真的提交。

之所以不能只信一个 `user_confirmed=true`：那种布尔量永远是模型自己填的，服务端无法验证
背后到底有没有问过人。想知道某个客户端走哪条路，调一次 `probe_elicitation`。

## 输出语言

会被念给用户听的那部分文案（任务状态、产出说明、进度行、失败原因、下载说明）支持九种
语言：`zh` / `zh-Hant` / `en` / `ja` / `ko` / `de` / `fr` / `ru` / `ar`。工具描述和给模型
的操作指令始终是中文——那是写给模型的。

优先级：工具参数 `locale` > 服务端 `MCP_LOCALE` > `zh`。

## 故障排除

### 认证失败 (10004)

API Key 不对、没注册、或格式错（必须 `ft_` 开头共 43 字符）。先 `echo $BELINDOC_API_KEY`
确认，再去平台看 key 的状态。

### 密钥类错误码 (30306 / 30307 / 30308 / 30309 / 30312)

这几个上游一律用 HTTP 200 送回来，业务码在响应体里。工具会把它们翻成一句可执行的话
（key 没复制全 / 被禁用要重新启用 / 已过期 / IP 不在白名单 / 需联系客服），并明确标注
**重试、换参数、重新上传都没有用**。只有 30311 是该退避重试的。

### 接口不存在 (404)

返回里会写明「接口 X 在当前服务地址（Y）上不存在」。这不是网络故障，是该功能在这个环境
没部署，或者 `BELINDOC_API_BASE_URL` 指错了环境。重试无用。

### 连接超时

检查后端是否在跑、网络是否通、防火墙是否放行。

## 开发

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/
```

根目录的 `test_api.py` / `test_upload.py` 是手动连真实 API 的冒烟脚本，不是用例，
pytest 只收集 `tests/`。

### 项目结构

```
trans-mcp/
├── README.md              # 本文件
├── DEPLOY.md              # HTTP 远程模式的部署
├── INTEGRATION.md         # 客户端配置速查
├── CONFIG.md              # 环境变量速查
├── pyproject.toml
├── .env.example
├── src/trans_mcp/
│   ├── server.py          # stdio 入口
│   ├── http_server.py     # HTTP 入口（Streamable HTTP）
│   ├── tools.py           # 工具定义与处理器（两种模式共用）
│   ├── client.py          # 上游 API 客户端
│   └── i18n.py            # 用户可见文案的九种语言
├── tests/
├── docs/                  # 上游开放 API 文档
├── deploy.sh              # Docker 部署
├── deploy-linux.sh        # systemd 部署
└── server.sh              # 本机起停
```

## 许可证

MIT License
