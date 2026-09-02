"""字幕改写的扣费闸门

改写是一单新的翻译任务，按视频时长计价、真实扣费。这条路以前只有工具描述里
一句「提交前请把预计消耗告诉用户」——同样的祈使句在 translate_video 上已经被
实测证伪过。这里盯住：不带确认码一定不提交，确认码换了字幕就作废。
"""

import types

import pytest

from trans_mcp import tools
from trans_mcp.client import TranslationClient, rewrite_fingerprint
import trans_mcp.client as client_mod


ARGS = {
    "order_no": "VO1",
    "source_subtitles_txt": "1\n00:00:01 --> 00:00:02\n你好\n",
    "target_subtitles_txt": "1\n00:00:01 --> 00:00:02\nHello\n",
}


@pytest.fixture(autouse=True)
def clean_confirms():
    client_mod._PENDING_CONFIRMS.clear()
    yield
    client_mod._PENDING_CONFIRMS.clear()


@pytest.fixture
def client():
    c = TranslationClient("test_api_key")
    c.submitted = []

    async def fake_post(path, payload):
        if path.endswith("videoTranslateRewriteQuotaCalculate"):
            return {"code": "200", "data": {"translateQuota": 12}}
        c.submitted.append(payload)
        return {"code": "200", "data": {"videoTranslateRewriteOrderNo": "VR1"}}

    c._post = fake_post
    return c


@pytest.mark.asyncio
async def test_first_call_never_submits(client):
    result = await tools._submit_video_rewrite(client, dict(ARGS))
    assert result["code"] == "409"
    assert result["data"]["submitted"] is False
    assert result["data"]["quota"] == 12          # 试算数字要摆到用户面前
    assert "12 额度" in result["data"]["userPrompt"]
    assert client.submitted == []


@pytest.mark.asyncio
async def test_confirm_token_lets_it_through_once(client):
    first = await tools._submit_video_rewrite(client, dict(ARGS))
    token = first["data"]["confirmToken"]

    ok = await tools._submit_video_rewrite(client, {**ARGS, "confirm_token": token})
    assert ok["code"] == "200" and len(client.submitted) == 1

    # 一次性：同一个码不能再提交一单
    again = await tools._submit_video_rewrite(client, {**ARGS, "confirm_token": token})
    assert again["code"] == "400" and len(client.submitted) == 1


@pytest.mark.asyncio
async def test_token_is_void_when_the_subtitles_changed(client):
    """用户点头同意的是他刚校对完的那一版；换一版就得重新问"""
    first = await tools._submit_video_rewrite(client, dict(ARGS))
    token = first["data"]["confirmToken"]

    tampered = {**ARGS, "target_subtitles_txt": "1\n00:00:01 --> 00:00:02\nHi there\n",
                "confirm_token": token}
    result = await tools._submit_video_rewrite(client, tampered)
    assert result["code"] == "400"
    assert client.submitted == []


@pytest.mark.asyncio
async def test_declining_in_the_dialog_stops_everything(client, monkeypatch):
    """客户端支持弹窗时直接问真人，选放弃就到此为止"""
    async def elicit_form(message, requested_schema):
        return types.SimpleNamespace(
            action="accept",
            content={"choice": requested_schema["properties"]["choice"]["enum"][1]},
        )

    session = types.SimpleNamespace(elicit_form=elicit_form)
    monkeypatch.setattr(tools, "_elicitation_session", lambda: session)

    result = await tools._submit_video_rewrite(client, dict(ARGS))
    assert result["code"] == "400"
    assert result["data"]["userDecision"] == "declined"
    assert client.submitted == []


def test_fingerprint_covers_the_subtitle_text():
    a = rewrite_fingerprint("VO1", "x", "y")
    assert a == rewrite_fingerprint("VO1", "x", "y")
    assert a != rewrite_fingerprint("VO1", "x", "y2")
    assert a != rewrite_fingerprint("VO2", "x", "y")
