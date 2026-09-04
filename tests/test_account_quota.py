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


# ---- OCR 是另一本账 ----------------------------------------------------------
# 全项目都在跟模型强调「扫描件扣的是 OCR 额度，和普通翻译不是同一本账」，可
# get_account_status 报出去的那句话里一个 OCR 数字都没有：快照只把钱包那半截
# 原样带出来（还是个没人读的 key），免费那半截整个丢了。用户问「OCR 还剩多少」
# 只能拿普通额度顶上——那是另一本账的数。

def wallet_with_ocr(**over):
    data = {
        "translateQuota": 19612,
        "useFreeTranslateQuota": 19,
        "totalFreeTranslateQuota": 1200,
        "ocrTranslateQuota": 500,
        "useFreeOcrTranslateQuota": 300,
        "totalFreeOcrTranslateQuota": 1200,
        "advancedTranslateQuota": 125,
    }
    data.update(over)
    return {"code": "200", "data": data}


def test_ocr_ledger_counts_its_own_free_quota():
    snap = _build_account_snapshot(wallet_with_ocr(), SUB)
    assert snap["quota"]["ocrFreeLeft"] == 900        # 1200 - 300
    assert snap["quota"]["ocrAvailable"] == 900 + 500
    # 两本账各算各的，别串
    assert snap["quota"]["available"] == 1181 + 19612


def test_ocr_free_quota_is_clamped_like_the_normal_one():
    snap = _build_account_snapshot(wallet_with_ocr(useFreeOcrTranslateQuota=-4), SUB)
    assert snap["quota"]["ocrFreeLeft"] == 1200
    assert "OCR" in snap["quotaSuspect"]


def test_missing_ocr_fields_are_not_reported_as_zero():
    """老服务端不回 OCR 字段：整本不报，报成 0 会被念成「OCR 额度用完了」"""
    snap = _build_account_snapshot(wallet(used=200), SUB)
    assert "ocrAvailable" not in snap["quota"]


@pytest.mark.asyncio
async def test_account_status_says_all_three_ledgers():
    class Fake:
        async def account_snapshot(self, refresh: bool = False):
            return _build_account_snapshot(wallet_with_ocr(), SUB)

    result = await _account_status(Fake())
    assert "OCR 额度 1400" in result["msg"]
    assert "高级模型额度 125" in result["msg"]
    # 三本账不能相加，这句得说出来
    assert "不要把三本账加在一起" in result["msg"]
