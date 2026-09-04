"""术语表透传

上游 batchSubmitTranslateTask 早就收 terminologyCollectionId，只是文档里没写。
它既不校验归属也不校验存在：ID 写错了照样提交成功、术语表静默不生效，所以这边
唯一能做的事就是原样带过去——不加工、不猜、不填空串。
"""

import pytest

from trans_mcp import tools
from trans_mcp.client import TranslationClient


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def client():
    return TranslationClient("test_api_key")


def make_client(client):
    calls = []

    async def fake_post(url, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("searchTranslateFilePage"):
            return FakeResponse({"code": "200", "data": {"records": []}})
        return FakeResponse({"code": "200", "data": {}})

    client.client.post = fake_post
    return calls


def submitted(calls):
    for url, payload in calls:
        if url.endswith("batchSubmitTranslateTask"):
            return payload
    raise AssertionError("没有提交请求")


FILES = [{"fileName": "a.docx", "fileObjectKey": "k1"}]


@pytest.mark.asyncio
async def test_id_is_passed_through(client):
    calls = make_client(client)
    await client.batch_submit_translate_task(
        FILES, "en", "zh-CN", "Gemini-2.5-Flash", None, "tc_123"
    )
    assert submitted(calls)["terminologyCollectionId"] == "tc_123"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "   "])
async def test_absent_id_leaves_the_field_out(client, value):
    """没传、空串、纯空白都当「没有术语表」，字段整个不出现"""
    calls = make_client(client)
    await client.batch_submit_translate_task(
        FILES, "en", "zh-CN", "Gemini-2.5-Flash", None, value
    )
    assert "terminologyCollectionId" not in submitted(calls)


@pytest.mark.asyncio
async def test_id_is_trimmed(client):
    """用户从网页端复制 ID 常带一头一尾的空白，去掉再发"""
    calls = make_client(client)
    await client.batch_submit_translate_task(
        FILES, "en", "zh-CN", "Gemini-2.5-Flash", None, "  tc_123\n"
    )
    assert submitted(calls)["terminologyCollectionId"] == "tc_123"


@pytest.mark.asyncio
async def test_tool_handler_forwards_the_arg(client):
    """工具入参名对得上 client 的位置参数——顺序错了会静默传给 is_ocr"""
    calls = make_client(client)
    handlers = tools.build_tool_handlers(client)
    await handlers["translate_document"]({
        "file_list": FILES,
        "source_language": "en",
        "target_language": "zh-CN",
        "model": "Gemini-2.5-Flash",
        "terminology_collection_id": "tc_456",
    })
    payload = submitted(calls)
    assert payload["terminologyCollectionId"] == "tc_456"
    assert payload["isOcr"] == 0


def test_tool_schema_declares_the_param():
    tool = next(t for t in tools.TOOLS if t.name == "translate_document")
    prop = tool.input_schema["properties"]["terminology_collection_id"]
    assert prop["type"] == "string"
    # 选填：模型不该为了它去打断用户
    assert "terminology_collection_id" not in tool.input_schema["required"]
