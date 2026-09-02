"""等待期间的进度推送

wait_for_* 一次调用要挂几十秒到几分钟，期间客户端界面上只有一行不动的
「正在调用」——用户看不到进度，也看不到已经等了多久（实测反馈：「没有一个
动态，也没有计时，不是很友好」）。服务端在这段时间里唯一能出声的通道是 MCP 的
notifications/progress，所以每轮轮询、以及轮询之间的心跳都要往外推一行带计时的
进度。推送本身是锦上添花，失败绝不能把等待弄挂。
"""

import asyncio

import pytest

import trans_mcp.client as client_mod
from trans_mcp.client import TranslationClient, VIDEO_STATUS_DONE
from trans_mcp import tools


@pytest.fixture
def fast_polling(monkeypatch):
    """把轮询、心跳、最小返回间隔都压到毫秒级，测试才跑得动"""
    monkeypatch.setattr(client_mod, "_VIDEO_POLL_SECONDS", 0.05)
    monkeypatch.setattr(client_mod, "_DOC_POLL_SECONDS", 0.05)
    monkeypatch.setattr(client_mod, "_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(client_mod, "_WAIT_MIN_SECONDS", 0)
    client_mod._WAITS.clear()
    yield
    client_mod._WAITS.clear()


def _pending(progress: float, rank=None, total=None):
    data = {
        "videoTranslateOrderNo": "VO1",
        "videoFileName": "a.mp4",
        "status": 1,
        "step": 1,
        "progress": {"progress": progress, "taskRanking": rank, "totalTask": total},
    }
    return {"code": "200", "data": client_mod._annotate_video_status(data)}


@pytest.mark.asyncio
async def test_video_wait_pushes_progress_with_a_running_clock(fast_polling):
    c = TranslationClient("test_api_key")
    frames = [_pending(10.0, 2, 5), _pending(40.0, 1, 5)]

    async def detail(order_no):
        return frames.pop(0) if len(frames) > 1 else frames[0]

    c.get_video_translate_detail = detail
    pushed = []

    async def on_progress(percent, total, message):
        pushed.append((percent, total, message))

    result = await c.wait_for_video_translation("VO1", timeout=30, on_progress=on_progress)

    assert result["code"] == "202"
    # 第一条要在第一次查询之前就发出去，否则开头几秒仍是一片静默
    assert pushed[0][0] == 0.0
    # 首轮之后还有心跳，中间不该有整段的静默
    assert len(pushed) >= 3
    assert all(total == 100.0 for _, total, _ in pushed)
    assert all("已等" in message for *_, message in pushed)
    assert any("10.0%" in message for *_, message in pushed)
    assert any("排队 2/5" in message for *_, message in pushed)
    assert pushed[-1][0] == 40.0


@pytest.mark.asyncio
async def test_progress_push_failure_does_not_break_the_wait(fast_polling):
    """客户端不认这个通知、或者通道断了，等待照常出结果"""
    c = TranslationClient("test_api_key")
    done = {
        "videoTranslateOrderNo": "VO1",
        "videoFileName": "a.mp4",
        "status": VIDEO_STATUS_DONE,
        "targetLanguage": "en",
        "videoDuration": 21134,
        "targetFileUrl": "https://cdn.example.com/a.mp4?Expires=99999999999&Signature=x",
        "paramJson": '{"voiceRole": "No", "subtitleType": 1}',
    }
    c.get_video_translate_detail = lambda o: asyncio.sleep(
        0, result={"code": "200", "data": client_mod._annotate_video_status(done)}
    )

    async def on_progress(percent, total, message):
        raise RuntimeError("客户端把流关了")

    result = await c.wait_for_video_translation("VO1", timeout=5, on_progress=on_progress)
    assert result["code"] == "200"
    assert result["data"]["finished"] is True


@pytest.mark.asyncio
async def test_document_wait_pushes_progress(fast_polling):
    monkey_frames = [
        {"code": "200", "data": {"status": 2, "sourceFileName": "a.docx",
                                 "taskProgress": {"progress": 30}}},
        {"code": "200", "data": {"status": 2, "sourceFileName": "a.docx",
                                 "taskProgress": {"progress": 70}}},
    ]
    c = TranslationClient("test_api_key")

    async def detail(order_no):
        return monkey_frames.pop(0) if len(monkey_frames) > 1 else monkey_frames[0]

    c.get_translate_file_detail = detail
    pushed = []

    async def on_progress(percent, total, message):
        pushed.append((percent, total, message))

    result = await c.wait_for_translation("DOC1", timeout=30, on_progress=on_progress)
    assert result["code"] == "202"
    assert pushed[0][0] == 0.0
    assert any("30.0%" in m for *_, m in pushed)
    assert all("已等" in m for *_, m in pushed)


def test_no_progress_token_means_no_pushing():
    """客户端没要进度就别推——没有 ctx（HTTP 模式）时同样返回 None"""
    assert tools._progress_reporter() is None
