# Trans MCP Server

文档翻译 MCP 服务，支持 PDF、文档、图片、视频翻译。

## 功能特性

- 多格式支持：PDF、Word、Excel、Markdown、图片、视频
- 语言检测：自动识别源语言
- 状态追踪：实时查询翻译进度
- 多模型支持：Gemini-2.5-Flash 等多种翻译模型

## 安装

```bash
# 克隆项目
cd /Users/zhangjun/project/trans_mcp

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -e .
```

## 配置

### 1. 获取 API Key

在 Belindoc 平台注册并创建 API Key：
- 登录 https://belindoc.com
- 进入「开放平台」→「API Key 管理」
- 创建新的 API Key（格式：`ft_` + 40 位随机串）

### 2. 配置环境变量

```bash
export BELINDOC_API_KEY="ft_your_api_key_here"
```

或使用 `.env` 文件：

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 API Key
```

### 3. 测试连接

```bash
python test_api.py
```

## 使用

### 启动 MCP Server

```bash
# 激活虚拟环境
source .venv/bin/activate

# 启动服务
trans-mcp
```

### MCP 配置示例

在 Claude Desktop 或其他 MCP 客户端中配置：

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "/Users/zhangjun/project/trans_mcp/.venv/bin/trans-mcp",
      "env": {
        "BELINDOC_API_KEY": "ft_your_api_key_here"
      }
    }
  }
}
```

## 支持的语言

| 语言 | 代码 |
|------|------|
| 任意语言 | AnyLanguage |
| 中文 | zh-CN |
| 英文 | en |
| 日文 | ja |
| 韩文 | ko |
| 法文 | fr |
| 德文 | de |
| 俄文 | ru |
| 阿拉伯文 | ar |

## 翻译模型

- Gemini-2.5-Flash（默认）
- 其他模型可通过 `get_model_list` 获取

## 工具列表

### 语言工具
- `get_supported_languages` - 获取支持的语言列表

### 模型工具
- `get_model_list` - 获取翻译模型列表

### 文档翻译
- `upload_document` - 批量获取文档上传链接
- `translate_document` - 提交文档翻译任务
- `get_document_translation_status` - 查询翻译状态
- `get_document_translation_result` - 获取翻译结果下载链接
- `list_document_translations` - 查询翻译任务列表
- `get_document_translation_by_batch` - 通过批次号查询翻译任务

### 视频翻译
- `upload_video` - 获取视频上传链接
- `translate_video` - 提交视频翻译任务
- `calculate_video_translation_quota` - 计算视频翻译配额
- `get_video_translation_status` - 查询视频翻译状态
- `list_video_translations` - 查询视频翻译任务列表
- `cancel_video_translation` - 取消视频翻译任务
- `get_video_subtitles` - 获取视频字幕
- `rewrite_video_subtitles` - 提交字幕改写任务
- `get_video_rewrite_status` - 查询字幕改写状态

## 故障排除

### 认证失败 (10004)

```
错误: {'code': '10004', 'msg': '认证失败请重新登录'}
```

**可能原因：**
1. API Key 不正确或未在数据库中注册
2. API Key 已过期或被禁用
3. API Key 格式错误（必须以 `ft_` 开头，共 43 字符）

**解决方法：**
1. 确认 API Key 是否正确：`echo $BELINDOC_API_KEY`
2. 在 Belindoc 平台检查 API Key 状态
3. 重新创建 API Key

### 连接超时

```
错误: ConnectTimeout
```

**可能原因：**
1. 后端服务未启动
2. 网络连接问题
3. 防火墙阻止访问

**解决方法：**
1. 检查后端服务状态
2. 测试网络连接：`ping internal-test-host`
3. 检查防火墙设置

## 环境配置

上游地址由 `BELINDOC_API_BASE_URL` 决定，stdio 和 HTTP 两种模式都读它：

| 环境 | 取值 |
|------|------|
| 测试（默认，不设即用） | `http://internal-test-host:6101` |
| 生产 | `https://belindoc.com/api` |

```bash
export BELINDOC_API_BASE_URL="https://belindoc.com/api"
```

不用改源码——部署包是 tar 解出来的，改源码等于每次升级都要重改一遍。

## 开发

### 运行测试

```bash
# 激活虚拟环境
source .venv/bin/activate

# 运行测试
pytest tests/
```

### 项目结构

```
trans_mcp/
├── README.md           # 项目说明
├── CONFIG.md          # 配置文档
├── pyproject.toml     # Python 包配置
├── .env.example       # 环境变量示例
├── .gitignore
├── test_api.py        # API 测试脚本
├── src/
│   └── trans_mcp/
│       ├── __init__.py
│       ├── server.py  # MCP Server 入口
│       ├── client.py  # API 客户端
│       └── tools.py   # MCP 工具定义
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_client.py
```

## 许可证

MIT License
