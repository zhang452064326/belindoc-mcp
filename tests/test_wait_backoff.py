"""进度不动时，等待自己越等越久

进度没变化的回合，调用方被要求一个字都不要输出——可它每回来一次仍是一整轮
对话往返：上下文重发一遍，界面上还多一行空回合（实测反馈：一屏的「...」）。
所以不传 timeout 时由本工具掌握节奏：起步 10 秒，之后每静默一轮翻一倍，
封顶 45 秒；进度一变立刻回到 10 秒。传了 timeout 就一切照传的来。
"""

import pytest

import trans_mcp.client as client_mod
from trans_mcp.client import TranslationClient


@pytest.fixture
def fast_polling(monkeypatch):
    monkeypatch.setattr(client_mod, "_DOC_POLL_SECONDS", 0.01)
    monkeypatch.setattr(client_mod, "_HEARTBEAT_SECONDS", 0.01)
    client_mod._WAITS.clear()
    yield
    client_mod._WAITS.clear()


def _doc_client(percent):
    c = TranslationClient("test_api_key")

    async def detail(order_no):
        return {
            "code": "200",
            "data": {
                "translateOrderNo": order_no,
                "sourceFileName": "a.pdf",
                "status": 2,
                "taskProgress": {"progress": percent["value"]},
            },
        }

    c.get_translate_file_detail = detail
    return c


@pytest.mark.asyncio
async def test_timeout_doubles_while_progress_stands_still(fast_polling, monkeypatch):
    """不传 timeout：静默一轮翻一倍，封顶 45，进度一变归零"""
    monkeypatch.setattr(client_mod, "_WAIT_TIMEOUT_BASE", 0.02)
    monkeypatch.setattr(client_mod, "_WAIT_TIMEOUT_MAX", 0.08)
    percent = {"value": 0.0}
    c = _doc_client(percent)

    seen = []
    real = client_mod._auto_timeout
    monkeypatch.setattr(
        client_mod, "_auto_timeout", lambda rec: seen.append(real(rec)) or seen[-1]
    )

    for _ in range(5):
        await c.wait_for_translation("DOC1")
    percent["value"] = 40.0
    await c.wait_for_translation("DOC1")
    await c.wait_for_translation("DOC1")

    # 第一轮把进度说给了用户，不算静默；从第二轮起才翻倍，最后撞上限
    assert seen[:5] == [0.02, 0.02, 0.04, 0.08, 0.08]
    # 进度变了那轮之后回到起步价
    assert seen[6] == 0.02


@pytest.mark.asyncio
async def test_explicit_timeout_is_taken_literally(fast_polling):
    """传了就按传的来，不自适应——调用方要更细的节奏时还是它说了算"""
    percent = {"value": 0.0}
    c = _doc_client(percent)

    for _ in range(3):
        out = await c.wait_for_translation("DOC1", timeout=0.05)
        assert out["code"] == "202"
    assert client_mod._WAITS["DOC1"]["silent"] == 2


def test_auto_timeout_caps_at_45():
    """真实取值：10 → 20 → 40 → 45，不要越过常见客户端 60 秒的工具超时"""
    curve = [client_mod._auto_timeout({"silent": n}) for n in range(6)]
    assert curve == [10, 20, 40, 45, 45, 45]
