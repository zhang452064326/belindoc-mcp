"""密钥层面的错误码

这几个码上游一律用 HTTP 200 送回来，业务码在响应体里。原来一路原样透传，调用
方看到的只是一句上游 msg，于是把「密钥过期」当网络故障反复重发、或者改个参数
再试一遍——这几条重试一万次也不会变。30311 是唯一该退避重试的。
分支必须认 code：msg 会随 language 头变语种。
"""

import asyncio

import pytest

import trans_mcp.client as client_mod
from trans_mcp import i18n, tools
from trans_mcp.client import TranslationClient, annotate_key_error


@pytest.fixture(autouse=True)
def zh(monkeypatch):
    monkeypatch.delenv("MCP_LOCALE", raising=False)
    token = i18n.use_locale(None)
    client_mod._WAITS.clear()
    yield
    i18n.reset_locale(token)
    client_mod._WAITS.clear()


@pytest.mark.parametrize("code,keyword", [
    ("30306", "ft_"),          # 密钥没复制全
    ("30307", "重新启用"),
    ("30308", "过期"),
    ("30309", "白名单"),
    ("30312", "客服"),
])
def test_key_errors_say_what_to_do_and_forbid_retrying(code, keyword):
    out = annotate_key_error({"code": code, "msg": "上游原话"})
    assert out["keyError"] is True and out["retryable"] is False
    assert keyword in out["msg"]
    assert "重试、换参数、重新上传都没有用" in out["msg"]


def test_rate_limit_is_the_only_retryable_one():
    out = annotate_key_error({"code": "30311", "msg": "x"})
    assert out["retryable"] is True and "退避" in out["msg"]
    # 也进了自动重试表，提交时会退避重发而不是直接失败
    assert "30311" in client_mod.TRANSIENT_CODES


def test_untouched_when_the_code_is_not_a_key_error():
    payload = {"code": "200", "msg": "ok"}
    assert annotate_key_error(dict(payload)) == payload
    assert annotate_key_error(None) is None


def test_key_error_message_follows_the_locale():
    token = i18n.use_locale("en")
    try:
        out = annotate_key_error({"code": "30308", "msg": "x"})
        assert "expired" in out["msg"]
    finally:
        i18n.reset_locale(token)


@pytest.mark.asyncio
async def test_tool_layer_annotates_every_handler():
    """二十个处理器不可能各自记得处理，统一在出口翻译"""
    c = TranslationClient("test_api_key")
    c._post_sse = lambda *a, **kw: asyncio.sleep(0, result={"code": "30307", "msg": "上游原话"})
    handlers = tools.build_tool_handlers(c)
    out = await handlers["get_video_translation_status"]({"order_no": "VO1"})
    assert out["keyError"] is True and "重新启用" in out["msg"]


@pytest.mark.asyncio
async def test_rate_limit_does_not_kill_an_ongoing_wait(monkeypatch):
    """限流不是任务失败：退避接着等，别让调用方立刻重来又撞一次"""
    monkeypatch.setattr(client_mod, "_VIDEO_POLL_SECONDS", 0.01)
    monkeypatch.setattr(client_mod, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(client_mod, "_RATE_LIMIT_BACKOFF", 0.01)
    c = TranslationClient("test_api_key")
    frames = [
        {"code": "30311", "msg": "调用过于频繁"},
        {"code": "200", "data": client_mod._annotate_video_status({
            "videoTranslateOrderNo": "VO1", "status": 1, "step": 1,
            "progress": {"progress": 10.0},
        })},
    ]

    async def detail(order_no):
        return frames.pop(0) if len(frames) > 1 else frames[0]

    c.get_video_translate_detail = detail
    result = await c.wait_for_video_translation("VO1", timeout=0.2)
    # 限流那一帧被吞掉了，等待继续，最后交出的是真实进度
    assert result["code"] == "202" and "10.0%" in result["msg"]
    assert not frames[1:]


@pytest.mark.asyncio
async def test_a_dead_key_stops_the_wait(monkeypatch):
    """密钥问题不能靠等：立刻把话交出去，别在那儿空转"""
    monkeypatch.setattr(client_mod, "_VIDEO_POLL_SECONDS", 0.01)
    c = TranslationClient("test_api_key")
    c.get_video_translate_detail = lambda o: asyncio.sleep(
        0, result={"code": "30308", "msg": "密钥已过期"}
    )
    result = await c.wait_for_video_translation("VO1", timeout=5)
    assert result["code"] == "30308"


@pytest.mark.parametrize("code,action,keyword", [
    ("30006", "user", "补充翻译额度"),
    ("30013", "user", "OCR"),
    ("30002", "file", "PDF"),
    ("30003", "file", "视频"),
    ("31001", "file", "空"),
    ("31002", "file", "大小"),
    ("31009", "file", "时长"),
    ("31010", "user", "10 分钟"),
    ("31004", "reupload", "没有文件"),
    ("31005", "reupload", "失效"),
    ("31006", "task", "语音识别"),
    ("31007", "retry", "瞬时故障"),
    ("31008", "wait", "终态"),
    ("403", "check", "不属于"),
])
def test_business_codes_say_what_to_do_next(code, action, keyword):
    """每一档的「下一步」完全不同：充额度 / 换文件 / 重传 / 退避 / 等 / 核对单号"""
    out = annotate_key_error({"code": code, "msg": "上游原话"})
    assert out["errorAction"] == action
    assert keyword in out["msg"]
    assert out["retryable"] is (action == "retry")


def test_quota_and_format_errors_forbid_blind_retry():
    """额度不足、格式不支持提一万次也是同样结果，每次还要重走一遍上传"""
    assert "不要自己重提" in annotate_key_error({"code": "30006"})["msg"]
    assert "不要自己改参数重试" in annotate_key_error({"code": "30002"})["msg"]
    assert "重新调 upload_document / upload_video" in annotate_key_error({"code": "31005"})["msg"]


def test_business_error_message_follows_the_locale():
    token = i18n.use_locale("ja")
    try:
        assert "クレジット" in annotate_key_error({"code": "30006"})["msg"]
    finally:
        i18n.reset_locale(token)


def test_codes_we_generate_ourselves_are_left_alone():
    """500 是我们自己给「任务已终止」用的码，别被当成上游错误覆盖掉"""
    mine = {"code": "500", "msg": "视频任务已终止：失败……", "data": {"failed": True}}
    assert annotate_key_error(dict(mine)) == mine
    # 400 同理（参数被拒）
    assert annotate_key_error({"code": "400", "msg": "x"}) == {"code": "400", "msg": "x"}


def test_wrapper_texts_are_localized_too():
    """包在上游 msg 外面的那几段：事实部分也要跟着 locale 走，
    只有「别重试 / 该重传」那半句留中文（那是给模型的）"""
    from trans_mcp.client import _not_found

    token = i18n.use_locale("en")
    try:
        note = _not_found("/external/video/x")["msg"]
        assert "does not exist at the current service address" in note
        assert "/external/video/x" in note
        assert "重试没有意义" in note  # 给模型的指令仍是中文
        assert "no longer available" in annotate_key_error({"code": "30014"})["msg"]
    finally:
        i18n.reset_locale(token)
    assert "上传的文件已失效" in annotate_key_error({"code": "30014"})["msg"]
    assert annotate_key_error({"code": "30014"})["errorAction"] == "reupload"
