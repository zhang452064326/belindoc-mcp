"""排队等待时不要把同一行进度刷满屏

视频排队十几分钟不动是常事。以前每轮都要求「和上次一样也要说」，用户看到的就是
一屏一模一样的进度行（实测反馈：「怎么那么多」）。规则改成：跟**上一次返回给用户
的那一行**比，变了才原样复述；没变就一个字都不要输出，直接接着轮询——轮询本身
不该在屏幕上留下任何东西。
"""

import pytest

import trans_mcp.client as client_mod
from trans_mcp.client import TranslationClient


@pytest.fixture
def fast_polling(monkeypatch):
    monkeypatch.setattr(client_mod, "_VIDEO_POLL_SECONDS", 0.01)
    monkeypatch.setattr(client_mod, "_HEARTBEAT_SECONDS", 0.01)
    client_mod._WAITS.clear()
    yield
    client_mod._WAITS.clear()


@pytest.mark.asyncio
async def test_only_a_changed_line_gets_repeated_in_full(fast_polling):
    c = TranslationClient("test_api_key")
    percent = {"value": 0.0}

    async def detail(order_no):
        data = {
            "videoTranslateOrderNo": "VO1",
            "videoFileName": "a.mp4",
            "status": 1,
            "step": 1,
            "progress": {"progress": percent["value"], "taskRanking": 2, "totalTask": 2},
        }
        return {"code": "200", "data": client_mod._annotate_video_status(data)}

    c.get_video_translate_detail = detail

    first = await c.wait_for_video_translation("VO1", timeout=0.05)
    # 第一次：用户还没见过这行，要完整说出来
    assert "把 msg 那行原样告诉用户" in first["agentNote"]

    second = await c.wait_for_video_translation("VO1", timeout=0.05)
    # 第二次进度一动没动：这一轮不该产生任何输出
    assert "不要输出任何文字" in second["agentNote"]
    assert "把 msg 那行原样告诉用户" not in second["agentNote"]

    percent["value"] = 35.0
    third = await c.wait_for_video_translation("VO1", timeout=0.05)
    # 真变了就再完整说一次
    assert "把 msg 那行原样告诉用户" in third["agentNote"]
    assert "35.0%" in third["msg"]

    # msg 从头到尾只有念给用户的那一行，操作指令一个字都不许混进来
    for out in (first, second, third):
        assert "不要输出任何文字" not in out["msg"]
        assert "调用本工具" not in out["msg"]
