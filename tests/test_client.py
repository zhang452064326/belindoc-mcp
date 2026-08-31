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
    mock_response.json.return_value = {
        "code": 0,
        "data": {
            "en": "English",
            "zh-CN": "Chinese",
        }
    }
    mock_response.raise_for_status = MagicMock()
    
    # get_language_enum 走的是 POST，不是 GET
    client.client.post = AsyncMock(return_value=mock_response)
    
    result = await client.get_language_enum()
    client.client.post.assert_awaited_once()
    assert result["code"] == 0
    assert "en" in result["data"]


@pytest.mark.asyncio
async def test_get_quota(client):
    """测试查询配额"""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "code": 0,
        "data": {"quota": 1000}
    }
    mock_response.raise_for_status = MagicMock()
    
    client.client.get = AsyncMock(return_value=mock_response)
    
    result = await client.get_quota()
    client.client.get.assert_awaited_once()
    assert result["code"] == 0
