"""免费额度对不上时不能原样端出去

实测一次：上游把 useFreeTranslateQuota 回成了 -4（正好等于上一单的实扣），
于是「免费剩余」算出来 1204、总量 1200，报给用户就是「免费剩余 1204/1200」。
这个数还会经 available 参与「够不够翻这一单」的判断，所以不只是显示难看。
"""

import pytest

import trans_mcp.client as client_mod
from trans_mcp.client import _build_account_snapshot
from trans_mcp.tools import _account_status


def wallet(used, total=1200, purse=19635):
    return {"code": "200", "data": {
        "translateQuota": purse,
        "useFreeTranslateQuota": used,
        "totalFreeTranslateQuota": total,
    }}


SUB = {"code": "200", "data": {"vipName": "终极版", "vipType": 20,
                               "videoDurationLimit": 60, "videoTranslateConcurrency": 3}}


def test_normal_numbers_are_untouched():
    snap = _build_account_snapshot(wallet(used=200), SUB)
    assert snap["quota"]["freeLeft"] == 1000
    assert snap["quota"]["available"] == 1000 + 19635
    assert "quotaSuspect" not in snap


def test_negative_used_quota_is_clamped_not_passed_through():
    snap = _build_account_snapshot(wallet(used=-4), SUB)
    # 剩余不能比总量还大
    assert snap["quota"]["freeLeft"] == 1200
    assert snap["quota"]["freeLeft"] <= snap["quota"]["freeTotal"]
    # available 跟着往小里走，别让一个虚高的余额去替用户判断够不够翻
    assert snap["quota"]["available"] == 1200 + 19635
    assert "-4" in snap["quotaSuspect"] and "不自洽" in snap["quotaSuspect"]


def test_used_beyond_total_still_floors_at_zero():
    snap = _build_account_snapshot(wallet(used=5000), SUB)
    assert snap["quota"]["freeLeft"] == 0
    assert "quotaSuspect" not in snap


class FakeClient:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def account_snapshot(self, refresh: bool = False):
        return self._snapshot


@pytest.mark.asyncio
async def test_status_says_the_free_quota_is_unreliable():
    """原样转述的要求在前，所以「这一项不可信」必须一起说出去"""
    snap = _build_account_snapshot(wallet(used=-4), SUB)
    out = await _account_status(FakeClient(snap))
    assert "免费额度这一项这次对不上" in out["msg"]
    assert "不要只报数字" in out["msg"]


@pytest.mark.asyncio
async def test_status_stays_quiet_when_the_numbers_add_up():
    snap = _build_account_snapshot(wallet(used=200), SUB)
    out = await _account_status(FakeClient(snap))
    assert "对不上" not in out["msg"]
    assert "免费剩余 1000/1200" in out["msg"]
