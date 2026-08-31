# @zhangjun/trans-mcp

MCP Server for document translation via Belindoc API.

## Installation

```bash
npx @zhangjun/trans-mcp
```

## Configuration

Add to your Claude Desktop or Codex config:

```json
{
  "mcpServers": {
    "trans-mcp": {
      "command": "npx",
      "args": ["-y", "@zhangjun/trans-mcp"],
      "env": {
        "BELINDOC_API_KEY": "your_api_key"
      }
    }
  }
}
```

## Features

- PDF, Word, Excel, Markdown translation
- Image translation
- Video translation
- Multiple translation models (Gemini-2.5-Flash, GPT-5, etc.)
- Real-time progress tracking

## Requirements

- Python 3.10+
- Node.js 18+
