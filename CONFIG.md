# Trans MCP 配置

## 环境变量

```bash
# 必需：Belindoc API Key
export BELINDOC_API_KEY="your_api_key_here"
```

## MCP 配置示例

在 Claude Desktop 或其他 MCP 客户端中配置：

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "trans-mcp",
      "env": {
        "BELINDOC_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

## 支持的语言代码

| 语言 | 代码 |
|------|------|
| 中文 | zh-CN |
| 英文 | en |
| 日文 | ja |
| 韩文 | ko |
| 法文 | fr |
| 德文 | de |
| 俄文 | ru |
| 阿拉伯文 | ar |

## 工具列表

### 语言工具
- `get_supported_languages` - 获取支持的语言列表

### 配额工具
- `get_quota` - 查询账户配额

### 文档翻译
- `upload_document` - 获取文档上传链接
- `translate_document` - 提交文档翻译任务
- `get_document_translation_status` - 查询翻译状态
- `get_document_translation_result` - 获取翻译结果
- `list_document_translations` - 查询翻译任务列表
- `cancel_document_translation` - 取消翻译任务
- `download_translated_document` - 下载翻译文档

### 图片翻译
- `translate_image` - 提交图片翻译任务
- `get_image_translation_status` - 查询图片翻译状态

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
