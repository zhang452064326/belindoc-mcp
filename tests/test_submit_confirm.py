"""提交前确认闸门

user_confirmed 是模型自己填的，拦不住「试算完直接提交」。这里覆盖真正拦得住的
那两条路：服务端弹窗（elicitation）和两步确认码握手，以及「配音 × 字幕」这张
交叉菜单——做成什么样、扣多少额度，得由用户在带数字的选项里挑。
"""

import types

import pytest
from unittest.mock import AsyncMock

from trans_mcp import tools
from trans_mcp.client import TranslationClient, record_quota_calc, quota_by_rule
import trans_mcp.client as client_mod


BASE_ARGS = {
    "source_language": "AnyLanguage",
    "target_language": "en",
    "source_file_object_key": "belin/file/a.mp4",
    "video_file_name": "a.mp4",
    "voice_role": "No",
    "user_confirmed": True,
    "subtitle_type": 1,
}
DURATION_MS = 21134.0


@pytest.fixture(autouse=True)
def clean_state():
    """这些记账都是模块级的，用例之间必须互不影响"""
    stores = (client_mod._QUOTA_CALCS, client_mod._PENDING_CONFIRMS,
              client_mod._VIDEO_SUBMITS, client_mod._ACCOUNTS)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


@pytest.fixture
def client():
    c = TranslationClient("test_api_key")
    c.submit_video_translate = AsyncMock(
        return_value={"code": "200", "data": {"videoTranslateOrderNo": "VO1"}}
    )

    async def fake_post(path, payload):
        """按文档的计费规则伪造上游试算，不出网"""
        return {"code": "200", "data": {
            "translateQuota": quota_by_rule(
                payload["videoDuration"], payload["voiceRole"], payload["subtitleType"]
            ),
            "videoDuration": 1,
        }}

    c._post = AsyncMock(side_effect=fake_post)
    return c


def calc(voice_role="No", subtitle_type=1, quota=4, duration_ms=DURATION_MS):
    record_quota_calc(duration_ms, voice_role, subtitle_type, {"translateQuota": quota, "videoDuration": 1})


def option(result, voice_role, subtitle_type):
    """交出去的菜单只有 序号/额度/确认码——配音和字幕由确认码自己记着。
    序号就是 VIDEO_COMBOS 的顺序。"""
    from trans_mcp.client import VIDEO_COMBOS

    index = VIDEO_COMBOS.index((voice_role, subtitle_type)) + 1
    for opt in result["data"]["options"]:
        if opt["index"] == index:
            return opt
    raise AssertionError(f"菜单里没有 {voice_role}/{subtitle_type}")


def elicit_ctx(pick=None, action="accept", boom=False):
    """伪造支持 elicitation 的会话。pick 是要选的第几项（1 起），None 表示选放弃。"""
    async def elicit_form(message, requested_schema):
        if boom:
            raise RuntimeError("no back channel")
        choices = requested_schema["properties"]["choice"]["enum"]
        return types.SimpleNamespace(
            action=action,
            content={"choice": choices[pick - 1 if pick else -1]},
        )

    session = types.SimpleNamespace(
        client_params=types.SimpleNamespace(
            capabilities=types.SimpleNamespace(elicitation=types.SimpleNamespace()),
        ),
        elicit_form=elicit_form,
    )
    return types.SimpleNamespace(session=session)


async def submit(client, ctx=None, **overrides):
    args = dict(BASE_ARGS, **overrides)
    if ctx is None:
        return await tools._submit_video_translate(client, args)
    token = tools._REQUEST_CTX.set(ctx)
    try:
        return await tools._submit_video_translate(client, args)
    finally:
        tools._REQUEST_CTX.reset(token)


def submitted_param(client):
    """submit_video_translate 的第 5 个位置参数就是最终生效的 videoTaskParam"""
    return client.submit_video_translate.await_args.args[4]


@pytest.mark.asyncio
async def test_no_quota_calc_means_no_submit(client):
    """没试算过就提交 = 没跟用户说过要扣多少"""
    result = await submit(client)
    assert result["code"] == "400"
    assert "calculate_video_translation_quota" in result["msg"]
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_calc_must_match_submit_params(client):
    """按不配音试算、按 clone 配音提交，预算和实扣差一倍"""
    calc(voice_role="No")
    result = await submit(client, voice_role="clone")
    assert result["code"] == "400"
    assert "试算" in result["msg"]
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_call_hands_back_the_whole_menu(client):
    """user_confirmed=true 也不放行：第一次调用只交出「配音 × 字幕」这张表"""
    calc()
    result = await submit(client)
    assert result["code"] == "409"
    assert result["data"]["submitted"] is False
    options = result["data"]["options"]
    assert len(options) == 7
    assert all(o["confirmToken"].startswith("CONFIRM-") for o in options)
    # 每格一个自己的确认码，不能共用
    assert len({o["confirmToken"] for o in options}) == 7
    assert option(result, "No", 1)["quota"] == 4
    # 克隆配音 + 字幕翻倍，这个差价必须让用户在选之前就看见
    assert option(result, "clone", 1)["quota"] == 8
    assert option(result, "clone", 0)["quota"] == 4

    prompt = result["data"]["userPrompt"]
    assert "21 秒" in prompt  # 时长取自试算记录，不是模型转述
    assert "克隆配音 + 译文字幕 —— 8 额度" in prompt
    # 既不配音也不嵌字幕的那格是白扣费，不该出现在菜单里
    assert "No/0" not in prompt
    # 默认那一项的标记在菜单文字里，机读列表不再抄一遍
    assert "1. 原声 + 译文字幕 —— 4 额度   ← 默认这一项" in prompt
    assert "8. 放弃" in prompt
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_token_submits_once(client):
    """带确认码才真提交，而且一个码只能提交一单"""
    calc()
    menu = await submit(client)
    token = option(menu, "No", 1)["confirmToken"]

    ok = await submit(client, confirm_token=token)
    assert ok["code"] == "200"
    client.submit_video_translate.assert_awaited_once()

    again = await submit(client, confirm_token=token)
    assert again["code"] == "400"
    assert "已经用掉" in again["msg"]
    client.submit_video_translate.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_can_pick_a_different_combo_from_the_menu(client):
    """用户挑了带配音的那格，提交的就得是那格——菜单不是摆设"""
    calc()
    menu = await submit(client)
    picked = option(menu, "clone", 3)

    # 只回传确认码：配音和字幕由码自己记着
    ok = await submit(client, confirm_token=picked["confirmToken"])
    assert ok["code"] == "200"
    param = submitted_param(client)
    assert param["voiceRole"] == "clone"
    assert param["subtitleType"] == 3


@pytest.mark.asyncio
async def test_the_token_decides_the_combo_not_the_model(client):
    """模型拿着「不配音」那格的码、却说要克隆配音——那是替用户升一倍的费。
    以码为准：提交出去的必须还是用户点的那一格。"""
    calc()
    menu = await submit(client)
    token = option(menu, "No", 1)["confirmToken"]

    result = await submit(client, voice_role="clone", subtitle_type=1, confirm_token=token)
    assert result["code"] == "200"
    param = submitted_param(client)
    assert param["voiceRole"] == "No"        # 用户点的那格
    assert param["subtitleType"] == 1


@pytest.mark.asyncio
async def test_elicitation_pick_submits_in_one_call(client):
    """能直接问到真人时，一次调用就够了，且以用户挑的那格为准"""
    calc()
    result = await submit(client, ctx=elicit_ctx(pick=4))  # 第 4 项 = clone + 译文字幕
    assert result["code"] == "200"
    param = submitted_param(client)
    assert param["voiceRole"] == "clone"
    assert param["subtitleType"] == 1


@pytest.mark.asyncio
async def test_elicitation_give_up_does_not_submit(client):
    calc()
    result = await submit(client, ctx=elicit_ctx(pick=None))  # 最后一项 = 放弃
    assert result["code"] == "400"
    assert result["data"]["charged"] is False
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_elicitation_decline_does_not_submit(client):
    calc()
    result = await submit(client, ctx=elicit_ctx(action="decline"))
    assert result["code"] == "400"
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_elicitation_failure_falls_back_to_the_menu(client):
    """弹窗发不出去就退回两步握手，绝不能因为问不到人就默认放行"""
    calc()
    result = await submit(client, ctx=elicit_ctx(boom=True))
    assert result["code"] == "409"
    assert len(result["data"]["options"]) == 7
    client.submit_video_translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_asks_once_and_carries_the_previous_order(client):
    """重做同样先拒一次，菜单和上一单单号一起给回来，不用问两遍"""
    calc()
    # 提交成功的记账在真实的 submit_video_translate 里，这里是 mock，手工补上
    client_mod._record_video_submit(
        BASE_ARGS["source_file_object_key"], "en",
        {"videoTranslateOrderNo": "VO1", "freeTranslateQuota": 4},
    )

    blocked = await submit(client)
    assert blocked["code"] == "409"
    assert blocked["data"]["previousOrderNo"] == "VO1"
    assert "重做是一单新任务" in blocked["data"]["userPrompt"]
    client.submit_video_translate.assert_not_awaited()

    done = await submit(
        client,
        retry_of_order_no="VO1",
        retry_confirmed=True,
        confirm_token=option(blocked, "No", 1)["confirmToken"],
    )
    assert done["code"] == "200"
    client.submit_video_translate.assert_awaited_once()
