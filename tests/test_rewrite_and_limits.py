"""字幕改写后的产物归属，以及账户额度/限额

改写成功后新视频在 videoTranslateRewrite 子记录里，父记录的 targetFileObjectKey
不更新（服务端 VideoTranslateRewriteServiceImpl 另存一份 entity）。只读顶层就会
把改写前那份当成最终产物交出去——用户刚花额度校对完字幕，拿到的还是旧的。
"""

import asyncio

import pytest

from trans_mcp import tools
from trans_mcp.client import TranslationClient, VIDEO_STATUS_DONE, video_products
import trans_mcp.client as client_mod


OLD = "https://cdn.example.com/old.mp4?Expires=99999999999&Signature=x"
NEW = "https://cdn.example.com/rewritten.mp4?Expires=99999999999&Signature=y"


def detail(rewrite_status=VIDEO_STATUS_DONE):
    return {
        "videoTranslateOrderNo": "VO1",
        "videoFileName": "a.mp4",
        "status": VIDEO_STATUS_DONE,
        "targetLanguage": "en",
        "videoDuration": 21134,
        "targetFileUrl": OLD,
        "targetSubtitlesUrl": OLD + ".srt",
        "paramJson": '{"voiceRole": "No", "subtitleType": 1}',
        "videoTranslateRewrite": {
            "videoTranslateRewriteOrderNo": "VR9",
            "status": rewrite_status,
            "targetFileUrl": NEW,
            "targetSubtitlesUrl": NEW + ".srt",
        },
    }


@pytest.fixture(autouse=True)
def clean_accounts():
    client_mod._ACCOUNTS.clear()
    yield
    client_mod._ACCOUNTS.clear()


def test_rewritten_video_wins():
    urls, info = video_products(detail())
    assert urls["targetFileUrl"] == NEW
    assert urls["targetSubtitlesUrl"] == NEW + ".srt"
    assert info["rewriteOrderNo"] == "VR9"


def test_unfinished_rewrite_does_not_hijack_the_result():
    """改写还没跑完（或失败了），交付的仍然是原来那一版"""
    urls, info = video_products(detail(rewrite_status=1))
    assert urls["targetFileUrl"] == OLD
    assert info == {}


@pytest.mark.asyncio
async def test_wait_hands_back_the_rewritten_file():
    c = TranslationClient("test_api_key")
    record = client_mod._annotate_video_status(detail())
    c.get_video_translate_detail = lambda o: asyncio.sleep(0, result={"code": "200", "data": record})

    result = await c.wait_for_video_translation("VO1", timeout=5)
    assert result["data"]["translatedVideoUrl"] == NEW
    assert result["data"]["rewriteOrderNo"] == "VR9"
    # 产出说明得说清楚这是改写后的版本，否则用户无从分辨两次下载有什么不同
    assert "改写" in result["data"]["outputNote"]


@pytest.mark.asyncio
async def test_account_status_reports_real_numbers():
    c = TranslationClient("test_api_key")

    async def fake_post(path, payload):
        if path.endswith("getMyWalletInfo"):
            return {"code": "200", "data": {
                "translateQuota": 0, "useFreeTranslateQuota": 6,
                "totalFreeTranslateQuota": 10,
            }}
        return {"code": "200", "data": {
            "vipName": "free", "vipType": 0, "videoDurationLimit": 10,
            "videoTranslateConcurrency": 1,
        }}

    c._post = fake_post
    result = await tools._account_status(c)
    assert result["code"] == "200"
    assert result["data"]["quota"]["wallet"] == 0
    assert result["data"]["quota"]["freeUsed"] == 6
    assert "可用额度 0" in result["msg"]
    assert "今日免费已用 6/10" in result["msg"]


@pytest.mark.asyncio
async def test_submit_blocked_before_upload_when_video_is_too_long():
    """超时长上限的单子，服务端本来就会拒——别等文件传完了才知道"""
    c = TranslationClient("test_api_key")
    submitted = []

    async def fake_post(path, payload):
        if path.endswith("getMyWalletInfo"):
            return {"code": "200", "data": {"translateQuota": 999}}
        if path.endswith("getMySubscriptionInfo"):
            return {"code": "200", "data": {"vipName": "free", "videoDurationLimit": 10}}
        submitted.append(path)
        return {"code": "200", "data": {}}

    c._post = fake_post
    client_mod.record_quota_calc(
        11 * 60 * 1000, "No", 1, {"translateQuota": 88, "videoDuration": 22}
    )
    result = await tools._submit_video_translate(c, {
        "source_language": "AnyLanguage", "target_language": "en",
        "source_file_object_key": "belin/file/long.mp4", "video_file_name": "long.mp4",
        "voice_role": "No", "user_confirmed": True, "subtitle_type": 1,
    })
    assert result["code"] == "400"
    assert "10 分钟" in result["msg"]
    assert submitted == []          # 一个字节都没往上游提交
