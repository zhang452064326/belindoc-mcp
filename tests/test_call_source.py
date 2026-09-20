"""提交任务时请求体带 callSource=3，上游据此把任务记成 MCP 调用"""
import pytest

from trans_mcp.client import TranslationClient


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.mark.asyncio
async def test_doc_submit_marks_call_source_as_mcp():
    c = TranslationClient("test_api_key")
    calls = []

    async def fake_post(url, json=None, **kwargs):
        calls.append((url, json))
        return FakeResponse({"code": "200", "data": {}})

    c.client.post = fake_post
    await c.batch_submit_translate_task(
        [{"fileName": "a.docx", "fileObjectKey": "k1"}], "en", "zh-CN", "Gemini-2.5-Flash"
    )
    payload = next(p for u, p in calls if u.endswith("batchSubmitTranslateTask"))
    assert payload["callSource"] == 3


@pytest.mark.asyncio
async def test_video_submit_marks_call_source_as_mcp():
    c = TranslationClient("test_api_key")
    calls = []

    async def fake_post(path, payload, timeout=None):
        calls.append((path, payload))
        return {"code": "500", "msg": "x"}

    c._post = fake_post
    await c.submit_video_translate(
        "AnyLanguage", "en", "belin/file/a.mp4", "a.mp4",
        {"voiceRole": "clone", "subtitleType": 1},
    )
    payload = next(p for u, p in calls if u.endswith("submitVideoTranslate"))
    assert payload["callSource"] == 3


@pytest.mark.asyncio
async def test_video_rewrite_marks_call_source_as_mcp():
    c = TranslationClient("test_api_key")
    calls = []

    async def fake_post(path, payload, timeout=None):
        calls.append((path, payload))
        return {"code": "500", "msg": "x"}

    c._post = fake_post
    await c.submit_video_rewrite("VT20260101_1_1", "a\n", "b\n")
    payload = next(p for u, p in calls if u.endswith("submitVideoRewrite"))
    assert payload["callSource"] == 3
