"""HTTP 模式的 JSON-RPC 端点

线上跑的就是这个文件，之前一行没测。这里直接喂 handle_mcp 一个假 request，
不起真监听——CI 和沙箱里都能跑。
"""

import json

import pytest

from trans_mcp.http_server import TransMcpHttpServer
from trans_mcp.tools import TOOLS


class FakeRequest:
    """只提供 handle_mcp 用到的两样：请求头和 json()"""

    def __init__(self, body, api_key="ft_test"):
        self._body = body
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def json(self):
        return self._body


def payload(resp):
    return json.loads(resp.body)


@pytest.fixture
def server():
    return TransMcpHttpServer(port=0)


@pytest.mark.parametrize("path,expected", [
    ("/mcp", "/mcp"),
    ("mcp", "/mcp"),
    ("/api/mcp/", "/api/mcp"),
    ("  api/mcp  ", "/api/mcp"),
])
def test_endpoint_path_is_normalized(path, expected):
    assert TransMcpHttpServer(mcp_path=path).mcp_path == expected


@pytest.mark.asyncio
async def test_missing_bearer_is_401(server):
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, api_key=None
    ))
    assert resp.status == 401


@pytest.mark.asyncio
async def test_tools_list_returns_every_tool(server):
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ))
    names = [t["name"] for t in payload(resp)["result"]["tools"]]
    assert names == [t.name for t in TOOLS]


@pytest.mark.asyncio
async def test_notifications_get_202_with_no_body(server):
    """通知没有 id，按 JSON-RPC 规范不能回响应体"""
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ))
    assert resp.status == 202
    assert not resp.body


@pytest.mark.asyncio
async def test_unknown_method_is_method_not_found(server):
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 7, "method": "completion/complete"}
    ))
    assert payload(resp)["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_tool_is_method_not_found(server):
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "translate_everything", "arguments": {}}}
    ))
    assert payload(resp)["error"]["code"] == -32601
    await server.pool.close_all()


@pytest.mark.asyncio
async def test_tool_result_comes_back_as_text_content(server, monkeypatch):
    async def fake_tool(args):
        return {"code": "200", "data": {"echo": args}}

    monkeypatch.setattr(
        TransMcpHttpServer, "_build_tool_handlers",
        staticmethod(lambda client: {"get_model_list": fake_tool}),
    )
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "get_model_list", "arguments": {"locale": "en"}}}
    ))
    content = payload(resp)["result"]["content"]
    assert content[0]["type"] == "text"
    assert json.loads(content[0]["text"])["data"]["echo"] == {"locale": "en"}
    await server.pool.close_all()


@pytest.mark.asyncio
async def test_tool_exception_becomes_a_jsonrpc_error(server, monkeypatch):
    async def boom(args):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(
        TransMcpHttpServer, "_build_tool_handlers",
        staticmethod(lambda client: {"get_model_list": boom}),
    )
    resp = await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "get_model_list", "arguments": {}}}
    ))
    err = payload(resp)["error"]
    assert err["code"] == -32000 and "上游炸了" in err["message"]
    await server.pool.close_all()


@pytest.mark.asyncio
async def test_repeated_calls_reuse_one_upstream_client(server, monkeypatch):
    """同一个 key 连着调，不该每次新开一条 httpx 连接池"""
    seen = []

    def record(client):
        seen.append(client)
        return {"get_model_list": lambda args: _ok()}

    async def _ok():
        return {"code": "200"}

    monkeypatch.setattr(TransMcpHttpServer, "_build_tool_handlers", staticmethod(record))
    for _ in range(3):
        await server.handle_mcp(FakeRequest(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_model_list", "arguments": {}}}
        ))
    assert len(seen) == 3 and len(set(map(id, seen))) == 1

    # 换个 key 就必须换一条，绝不能串用别人的密钥
    await server.handle_mcp(FakeRequest(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_model_list", "arguments": {}}},
        api_key="ft_other",
    ))
    assert seen[-1] is not seen[0] and seen[-1].api_key == "ft_other"
    await server.pool.close_all()


@pytest.mark.asyncio
async def test_bad_json_body_is_parse_error(server):
    class Broken(FakeRequest):
        async def json(self):
            raise ValueError("not json")

    resp = await server.handle_mcp(Broken({}))
    assert payload(resp)["error"]["code"] == -32700
