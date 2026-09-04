"""余额只有一本，别自己拼一个出来

两件事在这里锁住：

一、上游把 useFreeTranslateQuota 回成过 -4（正好等于上一单的实扣）。这种数不能
    原样念给用户。

二、更要命的是 available 曾经被算成「免费剩余 + 钱包」。2026-09-04 实测推翻了这个
    前提：一单全走免费额度（上游记 freeTranslateQuota=1、walletTranslateQuota=0），
    translateQuota 照样减 1——它本来就是含免费在内的总数，加一次就是重复计，
    最多虚报 1200，而这个数还会去判断「够不够翻这一单」。现在两个数分开报。
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


def test_balance_is_translate_quota_alone():
    snap = _build_account_snapshot(wallet(used=200), SUB)
    assert snap["quota"]["wallet"] == 19635
    assert snap["quota"]["freeUsed"] == 200
    assert snap["quota"]["freeTotal"] == 1200
    # 免费那两个数是用量计数，不是另一份余额——不许再出现合计
    assert "available" not in snap["quota"]
    assert "freeLeft" not in snap["quota"]


def test_negative_used_quota_is_flagged_not_passed_through():
    snap = _build_account_snapshot(wallet(used=-4), SUB)
    assert "-4" in snap["quotaSuspect"] and "说不通" in snap["quotaSuspect"]
    # 余额本身不受影响：它是上游直接给的数，没参与那个减法
    assert snap["quota"]["wallet"] == 19635


def test_used_beyond_total_is_flagged_too():
    snap = _build_account_snapshot(wallet(used=5000), SUB)
    assert "quotaSuspect" in snap


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
async def test_status_reports_the_two_numbers_separately():
    snap = _build_account_snapshot(wallet(used=200), SUB)
    out = await _account_status(FakeClient(snap))
    assert "对不上" not in out["msg"]
    assert "可用额度 19635" in out["msg"]
    assert "今日免费已用 200/1200" in out["msg"]
    # 合计数一个都不许出现：19635 + 1000 = 20635
    assert "20635" not in out["msg"]
    assert "一个都不要相加" in out["msg"]


# ---- OCR 那组数字 ------------------------------------------------------------
# 2026-09-04 测试环境对照：同模型同语言对各翻一页，scanned.pdf（isOcr=1）和
# text.pdf（isOcr=0）对钱包的影响一模一样——translateQuota -1、ocrTranslateQuota -1、
# useFreeTranslateQuota +1、useFreeOcrTranslateQuota +1。连不走 OCR 那单都把
# useFreeOcrTranslateQuota 加了 1。所以这组数照报，但不是另一份余额。

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


def test_ocr_numbers_are_reported_as_given():
    snap = _build_account_snapshot(wallet_with_ocr(), SUB)
    assert snap["quota"]["ocrWallet"] == 500
    assert snap["quota"]["ocrFreeUsed"] == 300
    assert snap["quota"]["ocrFreeTotal"] == 1200
    assert "ocrAvailable" not in snap["quota"]


def test_ocr_free_counter_is_flagged_like_the_normal_one():
    snap = _build_account_snapshot(wallet_with_ocr(useFreeOcrTranslateQuota=-4), SUB)
    assert "OCR" in snap["quotaSuspect"]


def test_missing_ocr_fields_are_not_reported_as_zero():
    """老服务端不回 OCR 字段：整组不报，报成 0 会被念成「OCR 额度用完了」"""
    snap = _build_account_snapshot(wallet(used=200), SUB)
    assert "ocrWallet" not in snap["quota"]


@pytest.mark.asyncio
async def test_account_status_lists_ocr_and_advanced_without_summing():
    snap = _build_account_snapshot(wallet_with_ocr(), SUB)
    out = await _account_status(FakeClient(snap))
    assert "可用额度 19612" in out["msg"]
    assert "OCR 额度 500" in out["msg"]
    assert "高级模型额度 125" in out["msg"]
    assert "不是另一份余额" in out["msg"]
