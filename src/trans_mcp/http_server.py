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
from .tools import TOOLS, build_tool_handlers, _to_json


class TransMcpHttpServer:
    """HTTP 模式的 MCP Server"""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8080, mcp_path: str = "/mcp"):
        self.host = host
        self.port = port
        # 同域名下落地页可能已占用 /mcp，允许把服务端点挪到别的路径
        self.mcp_path = self._normalize_path(mcp_path)
        self.server = Server("trans-mcp")

    @staticmethod
    def _normalize_path(path: str) -> str:
        """把 'api/mcp'、'/api/mcp/' 之类的写法统一成 '/api/mcp'"""
        return '/' + path.strip().strip('/')
    
    # upload_file 由服务端 open() 客户端给的路径，download_video_result 反过来
    # 由服务端往那条路径写文件——两者都只在服务端与用户同机时成立，否则「下载到
    # 本地」落的是服务器的盘，还等于给了任意路径的写入面。
    # 靠 remote addr 判断不住：经隧道或反代进来的请求一样是 loopback。所以默认
    # 全部隐藏，只在部署者显式声明同机时才放出来——比如就监听 localhost 给本机的
    # MCP 客户端用，这时设 MCP_LOCAL_FS=1。
    LOCAL_FS_TOOLS = {
        'upload_file', 'get_upload_status',
        'download_video_result', 'get_download_status',
    }
    LOCAL_FS = os.getenv('MCP_LOCAL_FS', '').strip().lower() in ('1', 'true', 'yes', 'on')
    HTTP_HIDDEN_TOOLS = set() if LOCAL_FS else LOCAL_FS_TOOLS
    
    @classmethod
    def _visible_tools(cls, request):
        return [t for t in TOOLS if t.name not in cls.HTTP_HIDDEN_TOOLS]
    
    @staticmethod
    def _get_api_key(request):
        """从 Authorization: Bearer <key> 取 API Key"""
        auth = request.headers.get('Authorization', '')
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        return None
    
    @staticmethod
    def _build_tool_handlers(client):
        """复用 tools.py 中的工具处理器定义"""
        return build_tool_handlers(client)
    
    async def handle_sse(self, request):
        """处理 SSE 连接"""
        # 验证 API Key
        if not self._get_api_key(request):
            return web.Response(status=401, text='Unauthorized: 缺少 Authorization: Bearer <key>')
        
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
        if not self._get_api_key(request):
            return web.Response(status=401, text='Unauthorized: 缺少 Authorization: Bearer <key>')
        
        tool_handlers = self._build_tool_handlers(TranslationClient(self._get_api_key(request), local_fs=self.LOCAL_FS))
        
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
                        for t in self._visible_tools(request)
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
                
                if tool_name in self.HTTP_HIDDEN_TOOLS:
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"content": [{"type": "text", "text": _to_json({
                            "code": "400",
                            "msg": (
                                f"{tool_name} 要读写服务端本机的路径，本次部署没有开放"
                                "（已从工具列表中隐藏，服务端与客户端可能不在同一台机器上）。"
                                "上传请改用 upload_document 取预签名链接并原样执行其 uploadCommand；"
                                "下载请把返回里的签名链接原样完整交给用户。"
                                "若服务端确实与用户同机，部署者可以设 MCP_LOCAL_FS=1 后重启放开。"
                            ),
                        })}]}
                    })
                
                if tool_name in tool_handlers:
                    try:
                        result = await tool_handlers[tool_name](arguments)
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [{"type": "text", "text": _to_json(result)}]
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
        api_key = self._get_api_key(request)
        if not api_key:
            return web.json_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "Missing Authorization: Bearer <key> header"}
            }, status=401)
        
        # 创建翻译客户端（按请求绑定 API Key）
        tool_handlers = self._build_tool_handlers(TranslationClient(api_key, local_fs=self.LOCAL_FS))
        try:
            body = await request.json()
            method = body.get("method")
            params = body.get("params", {})
            msg_id = body.get("id")
            
            if method == "initialize":
                # 之前这里把 params 整个丢掉了。客户端声明的能力（尤其 elicitation）
                # 只在握手这一次出现，不记下来就永远不知道对面支持什么。
                info = params.get("clientInfo") or {}
                print(
                    "[initialize] client="
                    + f"{info.get('name')}/{info.get('version')}"
                    + f" protocol={params.get('protocolVersion')}"
                    + f" capabilities={json.dumps(params.get('capabilities') or {}, ensure_ascii=False)}",
                    file=sys.stderr,
                    flush=True,
                )
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
                        for t in self._visible_tools(request)
                    ]
                }
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": result
                })
            
            elif method in ("notifications/initialized", "notifications/cancelled"):
                # 通知消息没有 id，按 JSON-RPC 规范不返回响应体
                return web.Response(status=202)

            elif method == "resources/list":
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"resources": []}
                })

            elif method == "resources/templates/list":
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"resourceTemplates": []}
                })

            elif method == "prompts/list":
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"prompts": []}
                })

            elif method == "ping":
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {}
                })

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                
                if tool_name in self.HTTP_HIDDEN_TOOLS:
                    return web.json_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"content": [{"type": "text", "text": _to_json({
                            "code": "400",
                            "msg": (
                                f"{tool_name} 要读写服务端本机的路径，本次部署没有开放"
                                "（已从工具列表中隐藏，服务端与客户端可能不在同一台机器上）。"
                                "上传请改用 upload_document 取预签名链接并原样执行其 uploadCommand；"
                                "下载请把返回里的签名链接原样完整交给用户。"
                                "若服务端确实与用户同机，部署者可以设 MCP_LOCAL_FS=1 后重启放开。"
                            ),
                        })}]}
                    })
                
                if tool_name in tool_handlers:
                    try:
                        result = await tool_handlers[tool_name](arguments)
                        return web.json_response({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [{"type": "text", "text": _to_json(result)}]
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
        app.router.add_post(self.mcp_path, self.handle_mcp)  # Streamable HTTP 端点
        app.router.add_get('/health', self.handle_health)
        
        print(f"Trans MCP Server (HTTP) starting on {self.host}:{self.port}", file=sys.stderr)
        print(f"SSE endpoint: http://{self.host}:{self.port}/sse", file=sys.stderr)
        print(f"Message endpoint: http://{self.host}:{self.port}/message", file=sys.stderr)
        print(f"MCP endpoint: http://{self.host}:{self.port}{self.mcp_path}", file=sys.stderr)
        print(
            "本机文件工具（" + "、".join(sorted(self.LOCAL_FS_TOOLS)) + "）："
            + ("已开放（MCP_LOCAL_FS=1）" if self.LOCAL_FS else "已隐藏，同机部署可设 MCP_LOCAL_FS=1 放开"),
            file=sys.stderr,
        )
        
        web.run_app(app, host=self.host, port=self.port)


def main():
    """启动 HTTP MCP Server"""
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    path = os.environ.get("MCP_PATH", "/mcp")
    
    server = TransMcpHttpServer(host, port, path)
    server.run()


if __name__ == "__main__":
    main()
