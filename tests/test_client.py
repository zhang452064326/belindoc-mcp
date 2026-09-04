"""测试用例"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from trans_mcp.client import TranslationClient


@pytest.fixture
def client():
    """创建测试客户端"""
    return TranslationClient("test_api_key")


@pytest.mark.asyncio
async def test_get_language_enum(client):
    """测试获取语言列表"""
    # Mock HTTP 响应
    mock_response = MagicMock()
    # 上游按界面语言返回多份等价的语种表
    mock_response.json.return_value = {
        "code": 0,
        "data": {
            "en": {"en": "English", "zh-CN": "Simplified Chinese"},
            "zh": {"en": "英语", "zh-CN": "简体中文"},
        }
    }
    mock_response.raise_for_status = MagicMock()
    
    # get_language_enum 走的是 POST，不是 GET
    client.client.post = AsyncMock(return_value=mock_response)
    
    result = await client.get_language_enum()
    client.client.post.assert_awaited_once()
    assert result["code"] == 0
    # 只保留一份语种表，避免把 9 份等价数据塞进调用方上下文
    assert result["displayLocale"] == "zh"
    assert result["data"] == {"en": "英语", "zh-CN": "简体中文"}


@pytest.mark.asyncio
async def test_get_language_enum_display_locale(client):
    """指定界面语言时返回对应的那一份"""
    def fresh_response():
        # 每次调用返回全新的对象：真实场景下每个响应都会重新解析 JSON
        r = MagicMock()
        r.json.return_value = {
            "code": 0,
            "data": {
                "en": {"zh-CN": "Simplified Chinese"},
                "zh": {"zh-CN": "简体中文"},
            }
        }
        r.raise_for_status = MagicMock()
        return r
    
    client.client.post = AsyncMock(return_value=fresh_response())
    result = await client.get_language_enum("en")
    assert result["displayLocale"] == "en"
    assert result["data"] == {"zh-CN": "Simplified Chinese"}
    
    # 不存在的界面语言回退到 zh
    client.client.post = AsyncMock(return_value=fresh_response())
    fallback = await client.get_language_enum("xx")
    assert fallback["displayLocale"] == "zh"
    assert fallback["data"] == {"zh-CN": "简体中文"}


@pytest.mark.asyncio
async def test_account_snapshot_merges_wallet_and_subscription(client):
    """额度要自己算：上游给的是「免费额度用了多少 / 一共多少」，不是余额"""
    from trans_mcp.client import _ACCOUNTS

    _ACCOUNTS.clear()

    async def fake_post(path, payload):
        if path.endswith("getMyWalletInfo"):
            return {"code": "200", "data": {
                "translateQuota": 120, "useFreeTranslateQuota": 6,
                "totalFreeTranslateQuota": 10, "ocrTranslateQuota": 0,
            }}
        return {"code": "200", "data": {
            "vipName": "pro", "vipType": 10, "endTime": 1788340732000,
            "videoDurationLimit": 60, "videoTranslateConcurrency": 2,
        }}

    client._post = fake_post
    snapshot = await client.account_snapshot()
    assert snapshot["ok"] is True
    # 余额就是 translateQuota 本身：它已经含了免费那部分的消耗，不能再加
    assert snapshot["quota"]["wallet"] == 120
    assert snapshot["quota"]["freeUsed"] == 6
    assert snapshot["quota"]["freeTotal"] == 10
    assert snapshot["limits"]["videoDurationMinutes"] == 60


@pytest.mark.asyncio
async def test_account_snapshot_degrades_when_endpoint_missing(client):
    """老版本服务端没有 /external/user：查不到就放行，不能把提交拦下来"""
    from trans_mcp.client import _ACCOUNTS, video_duration_over_limit

    _ACCOUNTS.clear()

    async def fake_post(path, payload):
        return {"code": "404", "msg": "接口不存在"}

    client._post = fake_post
    snapshot = await client.account_snapshot()
    assert snapshot["ok"] is False
    assert video_duration_over_limit(99 * 60 * 1000, snapshot) is None
