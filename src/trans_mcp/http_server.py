"""MCP Server - HTTP 远程模式（Streamable HTTP）

这里原先是手搓的一问一答 JSON-RPC：每个 POST 回一个 json_response，服务端没有
任何向客户端发起请求的通道。后果不是「少个功能」，而是两件正事在远程模式下全废：

1. elicitation 发不出去。视频提交、字幕改写是要真扣额度的，确认必须问到人；
   问不到人就只能退回 user_confirmed 这类布尔量——那永远是模型自己填的，
   服务端无法验证背后到底有没有问过。
2. 进度通知发不出去。等一个视频要几分钟，中间界面完全静止。

现在改用 SDK 的 StreamableHTTPSessionManager：SSE 响应体 + Mcp-Session-Id +
客户端把答复 POST 回来，服务端于是拿到了和 stdio 一样的双向会话，elicit_form 和
notifications/progress 两条路都通。传输层的协议细节（会话、重放、协议版本协商）
交给 SDK，这里只管三件自己的事：认 Bearer、按 key 借上游连接、健康检查。

顺带删掉了 /sse 和 /message：那两个端点是上一版手搓传输的残骸——/sse 自己伪造了
一条 initialize 然后 sleep 死循环，任何标准 MCP 客户端都握不上手。
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .client import TranslationClient
from .tools import register_tools_per_request

# 缺 Bearer 时回的那句话。服务器不存任何密钥，key 只能由调用方每次带上。
_UNAUTHORIZED = "Unauthorized: 缺少 Authorization: Bearer <key>"


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
        # asyncio.Lock 要绑到跑它的那个事件循环上。池子在建服务器时就造好，而循环
        # 是 uvicorn 起的，所以推迟到第一次用的时候再建。
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @asynccontextmanager
    async def acquire(self, api_key: str):
        lock = self._get_lock()
        async with lock:
            entry = self._entries.get(api_key)
            if entry is None:
                entry = _PooledClient(TranslationClient(api_key))
                self._entries[api_key] = entry
            entry.inflight += 1
        try:
            yield entry.client
        finally:
            async with lock:
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
        async with self._get_lock():
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            await entry.client.close()


def api_key_of(headers) -> Optional[str]:
    """从 Authorization: Bearer <key> 取 API Key。headers 是大小写无关的映射。"""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


class BearerRequired:
    """MCP 端点上没带 Bearer 就直接 401，别让它走到会话层。

    健康检查和其他路径不管——只有 MCP 端点需要密钥。
    """

    def __init__(self, app, mcp_path: str):
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == self.mcp_path:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", ())
            }
            if not api_key_of(headers):
                response = JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32000, "message": _UNAUTHORIZED},
                    },
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class MissingApiKey(RuntimeError):
    """本次调用没能从请求里拿到 API Key"""


class TransMcpHttpServer:
    """HTTP 模式的 MCP Server"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, mcp_path: str = "/mcp"):
        self.host = host
        self.port = port
        # 同域名下落地页可能已占用 /mcp，允许把服务端点挪到别的路径
        self.mcp_path = self._normalize_path(mcp_path)
        # 服务器不存密钥，但同一个 key 的连续调用该复用同一条连接
        self.pool = ClientPool()
        self.server = Server("trans-mcp", version=__version__)
        register_tools_per_request(self.server, self._borrow)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """把 'api/mcp'、'/api/mcp/' 之类的写法统一成 '/api/mcp'"""
        return '/' + path.strip().strip('/')

    @asynccontextmanager
    async def _borrow(self, ctx):
        """本次工具调用该用哪条上游连接：看这个请求自己带的 Bearer。

        ctx.request 是传输层挂上来的那个 HTTP 请求。同一条 MCP 会话里的每个请求都
        要重新读一次头，绝不能把握手时的 key 记下来一直用——那等于让后来的调用花
        别人的额度。
        """
        request = getattr(ctx, "request", None)
        api_key = api_key_of(request.headers) if request is not None else None
        if not api_key:
            raise MissingApiKey(_UNAUTHORIZED)
        async with self.pool.acquire(api_key) as client:
            yield client

    async def handle_health(self, request):
        """健康检查"""
        return JSONResponse({"status": "ok"})

    def build_app(self) -> Starlette:
        session_manager = StreamableHTTPSessionManager(
            app=self.server,
            # 必须留 SSE：JSON 模式下响应体只装得下一条回复，服务端就发不出
            # elicitation 了（SDK 的 can_send_request 正是照这个置位的）。
            json_response=False,
            # 必须有会话：无状态模式下客户端的答复没地方可落，同样发不出 elicitation。
            stateless=False,
            # 半小时没动静的会话自己回收，免得断线的客户端把会话表撑大
            session_idle_timeout=1800,
        )

        @asynccontextmanager
        async def lifespan(app):
            async with session_manager.run():
                try:
                    yield
                finally:
                    # 进程退出时把池里的上游连接一并关掉
                    await self.pool.close_all()

        return Starlette(
            routes=[
                Route(self.mcp_path, endpoint=StreamableHTTPASGIApp(session_manager)),
                Route("/health", self.handle_health, methods=["GET"]),
            ],
            middleware=[Middleware(BearerRequired, mcp_path=self.mcp_path)],
            lifespan=lifespan,
        )

    def run(self):
        """启动 HTTP 服务器"""
        print(f"Trans MCP Server (HTTP) starting on {self.host}:{self.port}", file=sys.stderr)
        print(f"MCP endpoint: http://{self.host}:{self.port}{self.mcp_path}", file=sys.stderr)
        print(f"Health check: http://{self.host}:{self.port}/health", file=sys.stderr)

        uvicorn.run(self.build_app(), host=self.host, port=self.port, log_level="info")


def main():
    """启动 HTTP MCP Server"""
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    path = os.environ.get("MCP_PATH", "/mcp")

    server = TransMcpHttpServer(host, port, path)
    server.run()


if __name__ == "__main__":
    main()
