"""预签名上传链接

上传一律由调用方自己跑 uploadCommand——服务端不碰用户的文件。这里盯住那条命令
拼得对不对，以及「只签发了链接、字节从没传上去」这种情况能不能被说清楚。
"""

import pytest
from unittest.mock import AsyncMock, patch

from trans_mcp.client import TranslationClient, is_video_file


def _presigned(object_key: str) -> dict:
    return {
        "code": "200",
        "data": [{
            "persignedUploadUrl": "https://s3.example/put",
            "objectKey": object_key,
            "encodeFileName": "f",
        }],
    }


@pytest.fixture
def client():
    c = TranslationClient("test_api_key")
    # PDF 传完会自动发一次「是不是扫描件」的检测。这里盯的是端点选择，
    # 别让它真去连上游——单测不出网。
    c.start_ocr_detection = lambda *args, **kwargs: None
    return c


def test_is_video_file():
    assert is_video_file("a.MP4") and is_video_file("b.mov")
    assert not is_video_file("c.pdf") and not is_video_file("noext")


@pytest.mark.asyncio
async def test_31004_says_the_file_was_never_uploaded(client):
    """31004 的真实含义是「这个 key 底下没文件」，得把话说到点子上"""
    from trans_mcp import tools
    from trans_mcp.client import _attach_upload_command, note_issued_key, record_quota_calc

    key = "belin/file/never-uploaded.mp4"
    note_issued_key(key)
    client.submit_video_translate = AsyncMock(
        return_value={"code": "31004", "msg": "文件上传失败，请联系管理员", "data": None}
    )
    args = {
        "source_language": "zh-CN", "target_language": "en",
        "source_file_object_key": key, "video_file_name": "a.mp4",
        "voice_role": "No", "user_confirmed": True, "subtitle_type": 1,
    }
    # 提交前的闸门：先试算、再从菜单里拿确认码，否则根本走不到调上游这一步
    record_quota_calc(60000, "No", 1, {"translateQuota": 8, "videoDuration": 2})
    client._post = AsyncMock(return_value={
        "code": "200", "data": {"translateQuota": 8, "videoDuration": 2},
    })
    menu = await tools._submit_video_translate(client, args)
    # 菜单只给 序号/额度/确认码；No + 译文字幕是第 1 项（VIDEO_COMBOS 的顺序）
    token = next(o["confirmToken"] for o in menu["data"]["options"] if o["index"] == 1)
    result = await tools._submit_video_translate(client, dict(args, confirm_token=token))
    assert "没有传上去" in result["diagnosis"]
    assert "沙箱" in result["diagnosis"]


