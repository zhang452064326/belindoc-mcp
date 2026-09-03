"""MCP Server - HTTP 远程模式"""

import asyncio
import os
import sys
import json
import time
from contextlib import asynccontextmanager
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


class _PooledClient:
    """池里的一条 TranslationClient，外加「还有几个请求正在用它」"""

    __slots__ = ("client", "inflight", "idle_since")

    def __init__(self, client: TranslationClient):
        self.client = client
        self.inflight = 0
        self.idle_since = 0.0


class ClientPool:
    """按 API Key 复用 TranslationClient。

    原先每个 JSON-RPC 请求都 new 一个 TranslationClient，而里面是一条自带连接池的
    httpx.AsyncClient，并且从头到尾没人 close——连接和 fd 只涨不落，服务多跑几天
    就耗尽。这里按 key 复用，空闲够久才关。

    关的时机要看引用计数：wait_for_video_translation 一次能挂几分钟，期间它用的
    那条 client 绝不能被回收线清掉，否则请求会在半路撞上 "client has been closed"。
    """

    # 空闲多久回收。比最长的一次等待（wait_* 的 timeout 上限）宽裕就行。
    IDLE_TTL = 300.0
    # 空闲连接的条数上限，防止一批一次性 key 把池撑大
    MAX_IDLE = 64

    def __init__(self):
        self._entries: dict[str, _PooledClient] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, api_key: str):
        async with self._lock:
            entry = self._entries.get(api_key)
            if entry is None:
                entry = _PooledClient(TranslationClient(api_key))
                self._entries[api_key] = entry
            entry.inflight += 1
        try:
            yield entry.client
        finally:
            async with self._lock:
                entry.inflight -= 1
                if entry.inflight <= 0:
                    entry.idle_since = time.monotonic()
                doomed = self._collect_stale()
            for victim in doomed:
                await victim.close()

    def _collect_stale(self):
        """挑出可以关掉的连接并从池里摘走。调用方已持锁。"""
        now = time.monotonic()
        idle = [
            (entry.idle_since, key)
            for key, entry in self._entries.items()
            if entry.inflight <= 0
        ]
        stale = {key for since, key in idle if now - since > self.IDLE_TTL}
        # 还没到期但空闲条数超上限，就从最早空闲的开始多关几条
        over = len(idle) - len(stale) - self.MAX_IDLE
        if over > 0:
            for _, key in sorted(k for k in idle if k[1] not in stale)[:over]:
                stale.add(key)
        return [self._entries.pop(key).client for key in stale]

    async def close_all(self):
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await entry.client.close()


class TransMcpHttpServer:
    """HTTP 模式的 MCP Server"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, mcp_path: str = "/mcp"):
        self.host = host
        self.port = port
        # 同域名下落地页可能已占用 /mcp，允许把服务端点挪到别的路径
        self.mcp_path = self._normalize_path(mcp_path)
        self.server = Server("trans-mcp")
        # 服务器不存密钥，但同一个 key 的连续调用该复用同一条连接
        self.pool = ClientPool()

    @staticmethod
    def _normalize_path(path: str) -> str:
        """把 'api/mcp'、'/api/mcp/' 之类的写法统一成 '/api/mcp'"""
        return '/' + path.strip().strip('/')
    
    @classmethod
    def _visible_tools(cls, request):
        # 服务端不碰用户机器上的文件：上传由调用方跑 uploadCommand，下载给签名链接。
        # 所以没有「本机文件工具」这一类，工具列表对谁都一样。
        return list(TOOLS)
    
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

    async def _call_tool(self, api_key: str, params: dict, msg_id):
        """跑一次 tools/call。只有这一条路径需要上游连接，所以池子在这里才借。"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        async with self.pool.acquire(api_key) as client:
            tool_handlers = self._build_tool_handlers(client)
            if tool_name not in tool_handlers:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                })
            try:
                result = await tool_handlers[tool_name](arguments)
            except Exception as e:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": str(e)}
                })
            return web.json_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": _to_json(result)}]
                }
            })

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
        api_key = self._get_api_key(request)
        if not api_key:
            return web.Response(status=401, text='Unauthorized: 缺少 Authorization: Bearer <key>')

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
                return await self._call_tool(api_key, params, msg_id)
            
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
                return await self._call_tool(api_key, params, msg_id)
            
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
        # 进程退出时把池里的连接一并关掉，别把 close 留给解释器回收
        app.on_cleanup.append(lambda _app: self.pool.close_all())

        print(f"Trans MCP Server (HTTP) starting on {self.host}:{self.port}", file=sys.stderr)
        print(f"SSE endpoint: http://{self.host}:{self.port}/sse", file=sys.stderr)
        print(f"Message endpoint: http://{self.host}:{self.port}/message", file=sys.stderr)
        print(f"MCP endpoint: http://{self.host}:{self.port}{self.mcp_path}", file=sys.stderr)
        
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
