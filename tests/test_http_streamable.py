"""HTTP 远程模式跑的是真 Streamable HTTP

上一版是手搓的一问一答 JSON-RPC，服务端没有回传通道，于是 elicitation 和进度通知
在远程模式下全废——而视频提交是要真扣额度的，确认必须问到人。这里用 SDK 的真客户端
接上真服务端（走 ASGI，不开监听端口）跑一遍，证明那条通道确实通了。
"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from mcp import ClientSession, types

from mcp.client.streamable_http import streamable_http_client

from trans_mcp.http_server import TransMcpHttpServer
from trans_mcp.tools import TOOLS


@asynccontextmanager
async def serving(server: TransMcpHttpServer):
    """在本机随机端口上真起一遍服务，给一个打到它身上的 httpx 客户端。

    这里不能用 httpx 的 ASGITransport 走进程内：它会把整个响应体收全了才返回，
    而 Streamable HTTP 的那条常驻 SSE 流永远不会结束——正是这条流让服务端能反过来
    向客户端提问，所以恰恰是最该测的部分。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(server.build_app(), log_level="warning", lifespan="on")
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    try:
        while not uv.started:
            await asyncio.sleep(0.01)
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            yield http
    finally:
        uv.should_exit = True
        await task


@asynccontextmanager
async def connected(server, api_key="ft_test", elicitation_callback=None):
    """一条完整的 MCP 会话：握手、协商能力，之后就能调工具"""
    async with serving(server) as http:
        http.headers["Authorization"] = f"Bearer {api_key}"
        url = f"{http.base_url}/mcp"
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(
                read, write, elicitation_callback=elicitation_callback
            ) as session:
                await session.initialize()
                yield session


def tool_payload(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def server():
    return TransMcpHttpServer(port=0)


@pytest.mark.asyncio
async def test_health_needs_no_key(server):
    async with serving(server) as http:
        resp = await http.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_mcp_endpoint_rejects_a_missing_bearer(server):
    async with serving(server) as http:
        resp = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 401
    assert "Bearer" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_a_real_client_can_handshake_and_list_tools(server):
    async with connected(server) as session:
        listed = await session.list_tools()
    assert [t.name for t in listed.tools] == [t.name for t in TOOLS]


@pytest.mark.asyncio
async def test_server_can_ask_the_user_directly(server):
    """probe_elicitation 是为排查这件事写的：服务端能不能真问到人。

    以前在 HTTP 上它只会回一句「本传输发不出去」。现在必须真弹一次、并把用户
    选的那一项带回来——扣费确认能不能不靠模型自证，全看这里。
    """
    asked = []

    async def answer(context, params: types.ElicitRequestParams):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"voice": "clone"})

    async with connected(server, elicitation_callback=answer) as session:
        out = tool_payload(await session.call_tool("probe_elicitation", {}))["data"]

    assert out["transport"] == "streamable-http" and out["backChannel"] is True
    assert out["elicitationDeclared"] is True
    assert out["elicitationUsable"] is True
    assert out["elicitResult"]["content"] == {"voice": "clone"}
    assert asked and "自检" in asked[0]


@pytest.mark.asyncio
async def test_client_without_elicitation_is_told_so_not_silently_allowed(server):
    """客户端没声明能力时，服务端要说清楚「问不到人」，而不是当作同意"""
    async with connected(server) as session:
        out = tool_payload(await session.call_tool("probe_elicitation", {}))["data"]

    assert out["elicitationDeclared"] is False
    assert out["elicitationUsable"] is False
    assert "没有声明 elicitation" in out["verdict"]


@pytest.mark.asyncio
async def test_each_call_uses_the_key_from_its_own_request(server, monkeypatch):
    """会话是长的，key 是每个请求各带各的——不能记住握手时那一个一直用"""
    borrowed = []
    real_acquire = server.pool.acquire

    @asynccontextmanager
    async def spy(api_key):
        borrowed.append(api_key)
        async with real_acquire(api_key) as client:
            yield client

    monkeypatch.setattr(server.pool, "acquire", spy)

    async with connected(server, api_key="ft_zhangsan") as session:
        await session.call_tool("probe_elicitation", {})
    assert borrowed == ["ft_zhangsan"]

    async with connected(server, api_key="ft_lisi") as session:
        await session.call_tool("probe_elicitation", {})
    assert borrowed == ["ft_zhangsan", "ft_lisi"]
    await server.pool.close_all()


@pytest.mark.parametrize("path,expected", [
    ("/mcp", "/mcp"),
    ("mcp", "/mcp"),
    ("/api/mcp/", "/api/mcp"),
    ("  api/mcp  ", "/api/mcp"),
])
def test_endpoint_path_is_normalized(path, expected):
    assert TransMcpHttpServer(mcp_path=path).mcp_path == expected


@pytest.mark.asyncio
async def test_endpoint_moves_with_mcp_path():
    """同域名下落地页占了 /mcp 时要能挪走，挪走后老路径就不该再有服务"""
    server = TransMcpHttpServer(mcp_path="/api/mcp")
    async with serving(server) as http:
        moved = await http.post(
            "/api/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        old = await http.post("/mcp", json={}, headers={"Authorization": "Bearer ft_x"})
    # 挪过去的那条认 Bearer（这里没带，所以 401 而不是 404）
    assert moved.status_code == 401
    assert old.status_code == 404


@pytest.mark.asyncio
async def test_unknown_tool_comes_back_as_text_not_a_crash(server):
    async with connected(server) as session:
        result = await session.call_tool("translate_everything", {})
    assert "未知工具" in result.content[0].text


@pytest.mark.asyncio
async def test_tool_exception_is_reported_as_an_error_result(server, monkeypatch):
    async def boom(args):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(
        "trans_mcp.tools.build_tool_handlers",
        lambda client: {"get_model_list": boom},
    )
    async with connected(server) as session:
        result = await session.call_tool("get_model_list", {})
    assert result.is_error and "上游炸了" in result.content[0].text
