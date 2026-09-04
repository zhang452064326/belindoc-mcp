"""模型列表要按账号自己的档位筛，不能一律当免费用户

线上实测：Ultimate 账号（vipType=20）调 getModelList，上游回了十个模型，
本工具只端出两个——GPT-5.5、Gemini-3.1-Pro、Claude-haiku-4-5 这些已经付过
钱的全被 `vipType<=0` 这条写死的判据滤掉了。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

import trans_mcp.client as client_mod
from trans_mcp.client import TranslationClient


# 上游 2026-09-04 的真实返回，截掉几条
UPSTREAM = [
    {"version": "Gemini-2.5-Flash", "vipType": -1, "coefficient": 1, "modelType": 2},
    {"version": "GPT-5-nano", "vipType": 0, "coefficient": 1, "modelType": 1},
    {"version": "GPT-5.2", "vipType": 10, "coefficient": 1, "modelType": 1},
    {"version": "GPT-5.5", "vipType": 10, "coefficient": 3, "modelType": 1},
    {"version": "Gemini-3.1-Pro", "vipType": 10, "coefficient": 3, "modelType": 2},
]


@pytest.fixture
def client():
    return TranslationClient("test_api_key")


def _upstream_models(client):
    response = MagicMock()
    response.json.return_value = {"code": "200", "data": UPSTREAM}
    response.raise_for_status = MagicMock()
    client.client.post = AsyncMock(return_value=response)


def _account(client, vip_type):
    # 缓存要从模块上现取：test_base_url 会 importlib.reload 这个模块，
    # 导入时绑定的那个 dict 之后就不是活着的那份了，清了也白清。
    client_mod._ACCOUNTS.clear()

    async def fake_post(path, payload):
        if path.endswith("getMyWalletInfo"):
            return {"code": "200", "data": {"translateQuota": 100}}
        return {"code": "200", "data": {"vipName": "Ultimate", "vipType": vip_type}}

    client._post = fake_post


def names(result):
    return [m["model"] for m in result["data"]]


@pytest.mark.asyncio
async def test_paid_account_sees_the_models_it_paid_for(client):
    _upstream_models(client)
    _account(client, 20)

    result = await client.get_model_list()
    assert names(result) == [m["version"] for m in UPSTREAM]
    assert "locked" not in result


@pytest.mark.asyncio
async def test_free_account_only_sees_free_models(client):
    _upstream_models(client)
    _account(client, 0)

    result = await client.get_model_list()
    assert names(result) == ["Gemini-2.5-Flash", "GPT-5-nano"]
    # 用不了的另列出来，别让模型拿去提交
    assert [m["model"] for m in result["locked"]] == ["GPT-5.2", "GPT-5.5", "Gemini-3.1-Pro"]


@pytest.mark.asyncio
async def test_billing_coefficient_is_reported(client):
    """倍率 3 的模型扣三倍额度，选之前得让用户知道"""
    _upstream_models(client)
    _account(client, 20)

    result = await client.get_model_list()
    by_name = {m["model"]: m["coefficient"] for m in result["data"]}
    assert by_name["GPT-5.5"] == 3
    assert by_name["Gemini-2.5-Flash"] == 1


@pytest.mark.asyncio
async def test_unknown_tier_returns_everything_rather_than_hiding(client):
    """查不到档位时全量返回：宁可让服务端去拒，也不能把付过钱的模型藏起来"""
    _upstream_models(client)
    client_mod._ACCOUNTS.clear()

    async def fake_post(path, payload):
        return {"code": "404", "msg": "接口不存在"}

    client._post = fake_post

    result = await client.get_model_list()
    assert names(result) == [m["version"] for m in UPSTREAM]
    assert "没查到本账号的会员档位" in result["msg"]


@pytest.mark.asyncio
async def test_upstream_error_is_passed_through(client):
    response = MagicMock()
    response.json.return_value = {"code": "500", "msg": "系统繁忙"}
    response.raise_for_status = MagicMock()
    client.client.post = AsyncMock(return_value=response)

    result = await client.get_model_list()
    assert result == {"code": "500", "msg": "系统繁忙"}
