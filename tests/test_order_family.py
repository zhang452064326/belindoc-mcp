"""订单号走错工具，和视频时长的单位

两条都是同一次真实会话里出的岔子：模型提交完视频（21 秒的片子），先拿视频单号
调了文档翻译的 wait_for_translation，上游只回一句 403「访问被拒绝」，它便告诉
用户「翻译服务暂时出现问题」；随后又把详情里的 videoDuration=21134（毫秒）当秒
念了出去——「视频时长较长（~5.87 小时）」。
"""

import asyncio

import pytest

import trans_mcp.client as client_mod
from trans_mcp import i18n, tools
from trans_mcp.client import TranslationClient


@pytest.fixture(autouse=True)
def zh(monkeypatch):
    monkeypatch.delenv("MCP_LOCALE", raising=False)
    token = i18n.use_locale(None)
    yield
    i18n.reset_locale(token)


def _handlers():
    c = TranslationClient("test_api_key")
    called = []

    async def boom(*a, **kw):
        called.append(a)
        return {"code": "403", "msg": "访问被拒绝"}

    c._post = boom
    c._post_sse = boom
    return tools.build_tool_handlers(c), called


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,order_no,instead", [
    ("wait_for_translation", "VO20260903105043_10024_01094", "wait_for_video_translation"),
    ("get_document_translation_status", "VO1", "get_video_translation_status"),
    ("get_document_translation_result", "VO1", "get_video_translation_status"),
    ("get_video_translation_status", "TR20260902182735_10024_00279", "get_document_translation_status"),
    ("wait_for_video_translation", "TR1", "wait_for_translation"),
])
async def test_wrong_family_is_stopped_before_it_reaches_upstream(tool, order_no, instead):
    handlers, called = _handlers()
    out = await handlers[tool]({"order_no": order_no})
    assert out["code"] == "400"
    assert instead in out["msg"] and order_no in out["msg"]
    # 关键是没打上游：403 会被模型读成权限问题，然后反复重试
    assert not called


@pytest.mark.asyncio
async def test_video_only_tools_say_which_line_to_use():
    handlers, called = _handlers()
    out = await handlers["get_video_subtitles"]({"order_no": "TR1"})
    assert out["code"] == "400" and "文档" in out["msg"]
    assert not called


@pytest.mark.asyncio
async def test_matching_family_still_goes_through():
    handlers, called = _handlers()
    out = await handlers["get_video_translation_status"]({"order_no": "VO1"})
    assert out["code"] == "403" and called
    handlers, called = _handlers()
    # 改写单号的前缀我们不认识，一律放行交给上游判
    out = await handlers["get_video_rewrite_status"]({"order_no": "XX1"})
    assert out["code"] == "403" and called


def test_duration_carries_its_unit():
    """21134 毫秒是 21 秒，不是 21134 秒"""
    record = client_mod._annotate_video_status({
        "videoTranslateOrderNo": "VO1", "status": 0, "videoDuration": 21134,
    })
    assert "videoDuration" not in record
    assert record["videoDurationMs"] == 21134
    assert record["videoDurationText"] == "21 秒"
    # 白名单裁剪之后这两项都还在
    slim = client_mod.slim_video_record(record)
    assert slim["videoDurationMs"] == 21134 and slim["videoDurationText"] == "21 秒"


@pytest.mark.asyncio
async def test_finished_wait_reports_the_duration_in_words(monkeypatch):
    monkeypatch.setattr(client_mod, "_VIDEO_POLL_SECONDS", 0.01)
    c = TranslationClient("test_api_key")

    async def detail(order_no):
        return {"code": "200", "data": client_mod._annotate_video_status({
            "videoTranslateOrderNo": "VO1", "videoFileName": "a.mp4",
            "status": client_mod.VIDEO_STATUS_DONE, "videoDuration": 21134,
            "targetFileUrl": "https://x/a.mp4?Signature=1",
        })}

    c.get_video_translate_detail = detail
    out = await c.wait_for_video_translation("VO1", timeout=1)
    assert out["data"]["videoDurationMs"] == 21134
    assert out["data"]["videoDurationText"] == "21 秒"
