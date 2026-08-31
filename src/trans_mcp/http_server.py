"""MCP Server - HTTP 远程模式"""

import asyncio
import os
import sys
import json
from aiohttp import web
from mcp.server.lowlevel.server import Server
from mcp.types import (
    ListToolsRequest,
    CallToolRequest,
    ListToolsResult,
    CallToolResult,
    CallToolRequestParams,
    PaginatedRequestParams,
    Tool,
    TextContent,
)
from .client import TranslationClient


class TransMcpHttpServer:
    """HTTP 模式的 MCP Server"""
    
    def __init__(self, api_key: str, host: str = "0.0.0.0", port: int = 8080, token: str = None):
        self.api_key = api_key
        self.host = host
        self.port = port
        self.token = token  # 访问令牌
        self.server = Server("trans-mcp")
        self.client = TranslationClient(api_key)
        self._setup_handlers()
    
    def _setup_handlers(self):
        """设置处理器"""
        
        # 工具定义
        self.TOOLS = [
            Tool(
                name="get_supported_languages",
                description="获取支持的语言列表",
                inputSchema={"type": "object", "properties": {}}
            ),
            Tool(
                name="get_model_list",
                description="获取可用翻译模型列表",
                inputSchema={"type": "object", "properties": {}}
            ),
            Tool(
                name="upload_file",
                description="上传本地文件到翻译平台",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件路径"}
                    },
                    "required": ["file_path"]
                }
            ),
            Tool(
                name="translate_document",
                description="提交文档翻译任务",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_list": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "fileName": {"type": "string"},
                                    "fileObjectKey": {"type": "string"}
                                }
                            }
                        },
                        "source_language": {"type": "string"},
                        "target_language": {"type": "string"},
                        "model": {"type": "string"},
                        "is_ocr": {"type": "integer"}
                    },
                    "required": ["file_list", "source_language", "target_language", "model"]
                }
            ),
            Tool(
                name="get_document_translation_status",
                description="查询翻译状态",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "order_no": {"type": "string"}
                    },
                    "required": ["order_no"]
                }
            ),
            Tool(
                name="get_document_translation_result",
                description="获取翻译结果下载链接",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "order_no": {"type": "string"},
                        "url_type": {"type": "integer"}
                    },
                    "required": ["order_no"]
                }
            ),
            Tool(
                name="wait_for_translation",
                description="等待翻译完成",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "order_no": {"type": "string"},
                        "timeout": {"type": "integer"}
                    },
                    "required": ["order_no"]
                }
            ),
        ]
        
        # 工具处理器
        self.TOOL_HANDLERS = {
            "get_supported_languages": lambda args: self.client.get_language_enum(),
            "get_model_list": lambda args: self.client.get_model_list(),
            "upload_file": lambda args: self.client.upload_file(args["file_path"]),
            "translate_document": lambda args: self.client.batch_submit_translate_task(
                args["file_list"],
                args["source_language"],
                args["target_language"],
                args.get("model", "Gemini-2.5-Flash"),
                args.get("is_ocr", 0)
            ),
            "get_document_translation_status": lambda args: self.client.get_translate_file_detail(args["order_no"]),
            "get_document_translation_result": lambda args: self.client.get_translate_s3_download_url(
                args["order_no"],
                args.get("url_type", 1)
            ),
            "wait_for_translation": lambda args: self.client.wait_for_translation(
                args["order_no"],
                args.get("timeout", 300)
            ),
        }
    
    async def handle_sse(self, request):
        """处理 SSE 连接"""
        # 验证 API Key
        if self.token:
            api_key = request.headers.get('X-Api-Key', '')
            if api_key != self.token:
                return web.Response(status=401, text='Unauthorized: Invalid API Key')
        
        response = web.StreamResponse(
            status=200,
            reason='OK',
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            }
        )
        await response.prepare(request)
        
        # 发送初始化消息
        init_msg = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "trans-mcp",
                    "version": "0.1.0"
                }
            }
        }
        await response.write(f"data: {json.dumps(init_msg)}\n\n".encode())
        
        # 保持连接
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        
        return response
    
    async def handle_message(self, request):
        """处理 JSON-RPC 消息"""
        # 验证 API Key
        if self.token:
            api_key = request.headers.get('X-Api-Key', '')
            if api_key != self.token:
                return web.Response(status=401, text='Unauthorized: Invalid API Key')
        
        try:
            body = await request.json()
            method = body.get("method")
            params = body.get("params", {})
            msg_id = body.get("id")
            
            if method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema
                        }
                        for t in self.TOOLS
                    ]
                }
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                })
            
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                
                if tool_name in self.TOOL_HANDLERS:
                    try:
                        result = await self.TOOL_HANDLERS[tool_name](arguments)
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [{"type": "text", "text": str(result)}]
                            }
                        })
                    except Exception as e:
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32000, "message": str(e)}
                        })
                else:
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                    })
            
            else:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"}
                })
        
        except Exception as e:
            return web.json_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(e)}
            })
    
    async def handle_health(self, request):
        """健康检查"""
        return web.json_response({"status": "ok"})
    
    async def handle_mcp(self, request):
        """处理 MCP 请求（Streamable HTTP）"""
        # 验证 API Key
        if self.token:
            api_key = request.headers.get('X-Api-Key', '')
            if api_key != self.token:
                return web.Response(status=401, text='Unauthorized: Invalid API Key')
        
        try:
            body = await request.json()
            method = body.get("method")
            params = body.get("params", {})
            msg_id = body.get("id")
            
            if method == "initialize":
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "trans-mcp", "version": "0.1.0"}
                    }
                })
            
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema
                        }
                        for t in self.TOOLS
                    ]
                }
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                })
            
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                
                if tool_name in self.TOOL_HANDLERS:
                    try:
                        result = await self.TOOL_HANDLERS[tool_name](arguments)
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [{"type": "text", "text": str(result)}]
                            }
                        })
                    except Exception as e:
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32000, "message": str(e)}
                        })
                else:
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                    })
            
            else:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"}
                })
        
        except Exception as e:
            return web.json_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(e)}
            })
    
    def run(self):
        """启动 HTTP 服务器"""
        app = web.Application()
        app.router.add_get('/sse', self.handle_sse)
        app.router.add_post('/message', self.handle_message)
        app.router.add_post('/mcp', self.handle_mcp)  # Streamable HTTP 端点
        app.router.add_get('/health', self.handle_health)
        
        print(f"Trans MCP Server (HTTP) starting on {self.host}:{self.port}", file=sys.stderr)
        print(f"SSE endpoint: http://{self.host}:{self.port}/sse", file=sys.stderr)
        print(f"Message endpoint: http://{self.host}:{self.port}/message", file=sys.stderr)
        print(f"MCP endpoint: http://{self.host}:{self.port}/mcp", file=sys.stderr)
        
        web.run_app(app, host=self.host, port=self.port)


def main():
    """启动 HTTP MCP Server"""
    api_key = os.environ.get("BELINDOC_API_KEY")
    if not api_key:
        print("错误: 请设置 BELINDOC_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)
    
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    # 使用 API Key 作为访问令牌
    token = api_key
    
    server = TransMcpHttpServer(api_key, host, port, token)
    server.run()


if __name__ == "__main__":
    main()
