"""MCP 工具定义"""

import contextvars
import sys
from contextlib import asynccontextmanager
from typing import Optional
from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ListToolsResult,
    CallToolResult,
    CallToolRequestParams,
    PaginatedRequestParams,
)
from .client import (
    TranslationClient,
    TASK_STATUS_DONE,
    VIDEO_STATUS_DONE,
    annotate_key_error,
    comparison_offer,
    offered_variants,
    key_never_uploaded,
    slim_video_record,
    strip_long_urls,
    _URL_VERBATIM_NOTE,
    _build_video_task_param,
    _expires_at,
    _expiry_note,
    _format_duration,
    DOUBLE_DECK_NOTE,
    is_pdf as client_is_pdf,
    ocr_of as client_ocr_of,
    video_duration_over_limit,
    video_products,
    confirm_fingerprint,
    issue_submit_confirm,
    peek_submit_confirm,
    rewrite_fingerprint,
    recent_quota_calc,
    recent_video_submit,
    take_submit_confirm,
)
from . import i18n



VOICE_ROLE_CHOICES = ("No", "clone")

# check_pdf_ocr 最多等多久再回「还在测」。分类要下载整个 PDF，等满没有意义。
_OCR_CHECK_WAIT = 8.0

# 当前这次 tools/call 的 ServerRequestContext。工具处理器签名里只有 args，拿不到
# 会话，而 elicitation（服务端反过来向用户提问）必须走 ctx.session。handle_call_tool
# 进处理器前塞进来，stdio 和 HTTP（Streamable HTTP）两种传输都有。
_REQUEST_CTX: contextvars.ContextVar = contextvars.ContextVar("trans_mcp_request_ctx", default=None)


async def _probe_elicitation(args) -> dict:
    """自检：这个客户端到底吃不吃 elicitation。不提交任务、不扣额度。

    user_confirmed / retry_confirmed 这类布尔量永远是模型自己填的，服务端无法
    验证背后有没有真实问过用户。elicitation 是唯一能让服务端直接问到人的通道，
    但它要求客户端声明能力、且传输层有回传通道——两者都得实测。
    """
    ctx = _REQUEST_CTX.get()
    if ctx is None:
        return {"code": "200", "data": {
            "transport": "unknown",
            "backChannel": False,
            "elicitationUsable": False,
            "verdict": (
                "这次调用没拿到会话上下文，服务端无法向客户端发起提问。"
                "正常的 stdio 和 Streamable HTTP 都不该走到这里，请把这句原样反馈。"
            ),
        }}

    session = ctx.session
    params = getattr(session, "client_params", None)
    caps = getattr(params, "capabilities", None)
    # 2.x 的 pydantic 模型走蛇形字段名，别照着协议里的 clientInfo 取
    info = getattr(params, "client_info", None)
    declared = bool(caps and getattr(caps, "elicitation", None))
    out = {
        # 传输层给每条消息挂了它自己的 HTTP 请求；stdio 上没有这东西
        "transport": "streamable-http" if getattr(ctx, "request", None) is not None else "stdio",
        "backChannel": True,
        "clientInfo": info.model_dump(exclude_none=True) if info else None,
        "protocolVersion": getattr(session, "protocol_version", None),
        "clientCapabilities": caps.model_dump(exclude_none=True) if caps else None,
        "elicitationDeclared": declared,
    }
    if not declared:
        out["elicitationUsable"] = False
        out["verdict"] = (
            "客户端在 initialize 里没有声明 elicitation 能力，服务端无法向它发起提问，"
            "只能退回现在这套参数守卫（user_confirmed / retry_confirmed 自我认证）。"
        )
        return {"code": "200", "data": out}

    try:
        result = await session.elicit_form(
            message=(
                "[trans-mcp 自检] 这条是服务端主动发起的提问，不会提交任何任务、不扣额度。"
                "请随便选一个，用来验证回答能不能传回服务端："
            ),
            requested_schema={
                "type": "object",
                "properties": {
                    "voice": {
                        "type": "string",
                        "title": "配音",
                        "enum": ["No", "clone"],
                        "description": "No=不配音只做字幕；clone=克隆原声配音（真实场景下额度翻倍）",
                    }
                },
                "required": ["voice"],
            },
        )
        out["elicitResult"] = result.model_dump(exclude_none=True)
        out["elicitationUsable"] = True
        out["verdict"] = (
            "elicitation 可用：上面 elicitResult 里的选择是用户本人给的，"
            "可以用它替掉 translate_video 的 user_confirmed 自我认证。"
        )
    except Exception as e:
        out["elicitationUsable"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        out["verdict"] = "客户端声明了 elicitation，但真发起时失败了（见 error）。"
    return {"code": "200", "data": out}


def _progress_reporter():
    """把 MCP 的进度通知接出来给等待类工具。

    等一个视频要几分钟，两次返回之间客户端界面是完全静止的——用户既看不到
    进度也看不到已经等了多久。notifications/progress 是这中间唯一能出声的通道。
    只有客户端在请求 _meta 里带了 progressToken 才有权推送（没带就返回 None，
    白推是违反协议的）。stdio 和 Streamable HTTP 都有回推通道，两边一样能推。
    """
    ctx = _REQUEST_CTX.get()
    session = getattr(ctx, "session", None)
    if session is None or not hasattr(session, "send_progress_notification"):
        return None
    # ctx.meta 是 TypedDict（运行时就是个 dict）；键名按 SDK 版本可能是下划线
    # 也可能是驼峰，两个都认一下，认不出就当客户端没要进度。
    meta = getattr(ctx, "meta", None)
    token = None
    if isinstance(meta, dict):
        token = meta.get("progress_token") or meta.get("progressToken")
    elif meta is not None:
        token = getattr(meta, "progress_token", None) or getattr(meta, "progressToken", None)
    if token is None:
        return None
    request_id = getattr(ctx, "request_id", None)

    async def report(progress: float, total=None, message=None) -> None:
        # related_request_id 让通知走本次请求的流，客户端才知道这条进度属于
        # 哪个工具调用（streamable HTTP 下尤其重要）。
        await session.send_progress_notification(
            token, progress, total=total, message=message, related_request_id=request_id
        )

    return report


def _elicitation_session():
    """能直接问到真人的会话；客户端没声明 elicitation 能力就返回 None。"""
    ctx = _REQUEST_CTX.get()
    if ctx is None:
        return None
    session = ctx.session
    caps = getattr(getattr(session, "client_params", None), "capabilities", None)
    if not (caps and getattr(caps, "elicitation", None)):
        return None
    return session


async def _ask_user_to_pick(session, args, calc: dict, options: list, warning: str = ""):
    """服务端直接问用户要哪一格。

    返回选中的那一格；用户放弃返回 "declined"；弹窗发不出去返回 None（交给两步握手）。
    """
    choices = [
        i18n.t("menu.option", index=opt["index"], label=opt["label"],
               quota=opt["quota"], note="", mark="")
        for opt in options
    ]
    give_up = i18n.t("menu.give_up", index=len(options) + 1)
    try:
        result = await session.elicit_form(
            message=(
                i18n.t("menu.head.submit") + "\n"
                + _file_brief(args, calc, warning)
                + "\n\n" + i18n.t("menu.question")
            ),
            requested_schema={
                "type": "object",
                "properties": {
                    "choice": {
                        "type": "string",
                        "title": i18n.t("elicit.pick.title"),
                        "enum": choices + [give_up],
                        "description": i18n.t("elicit.pick.desc"),
                    }
                },
                "required": ["choice"],
            },
        )
    except Exception:
        # 客户端声明了能力但真发起时失败——当作不可用，退回两步握手，
        # 绝不能因为问不到人就默认放行。
        return None
    if result.action != "accept":
        return "declined"
    choice = (result.content or {}).get("choice")
    if choice == give_up:
        return "declined"
    for opt, label in zip(options, choices):
        if choice == label:
            return opt
    # 答复对不上任何一项（客户端自由填写等），当作没问到，退回两步握手
    return None


def _file_brief(args, calc: dict, warning: str = "") -> str:
    """给用户看的那几行事实。时长取自试算记录，不是模型转述的数字。"""
    duration_ms = calc.get("durationMs") or 0
    lines = [
        i18n.t("brief.file", name=args.get("video_file_name")),
        i18n.t("brief.duration", duration=_format_duration(duration_ms / 1000)),
        i18n.t("brief.languages", source=args.get("source_language"),
               target=args.get("target_language")),
    ]
    if warning:
        lines.append(warning)
    return "\n".join("· " + line for line in lines)


async def _build_options(client, args, voice_role: str, subtitle_type, calc: dict) -> list:
    """配音 × 字幕 的整张表，每一格都带自己的额度和确认码。

    模型手里那一组只是它的建议。用户可能想要双字幕、想要配音，这些都直接改变
    扣费和产出，所以选择权连同数字一起摆出去，让用户挑哪一格。确认码按格签发，
    绑定各自的参数指纹：用户挑了哪一格，模型就只能提交那一格。
    """
    matrix = await client.video_translate_quota_matrix(calc.get("durationMs") or 0)
    options = []
    for index, row in enumerate(matrix, 1):
        options.append({
            "index": index,
            "label": row["label"],
            "voiceRole": row["voiceRole"],
            "subtitleType": row["subtitleType"],
            "quota": row["quota"],
            "quotaSource": row["source"],
            "proposed": row["voiceRole"] == voice_role and row["subtitleType"] == subtitle_type,
            "confirmToken": issue_submit_confirm(
                confirm_fingerprint(
                    args["source_file_object_key"], args["target_language"],
                    row["voiceRole"], row["subtitleType"],
                ),
                {"voiceRole": row["voiceRole"], "subtitleType": row["subtitleType"]},
            ),
        })
    return options


def _slim_options(options) -> list:
    """交出去的菜单只留 序号 / 额度 / 确认码 三样

    配音和字幕不必再列一遍：确认码自己记着它代表哪一格，提交时以它为准。文字
    说明在 userPrompt 里，机读列表再抄一份就是七行重复——一次菜单七格，省下来
    的是实打实的一屏。
    """
    if not options:
        return options
    return [
        {k: opt[k] for k in ("index", "quota", "confirmToken")}
        for opt in options
    ]


def _option_lines(options: list) -> list:
    lines = []
    for opt in options:
        note = i18n.t("menu.option.rule_note") if opt["quotaSource"] == "rule" else ""
        mark = i18n.t("menu.option.default") if opt["proposed"] else ""
        lines.append(i18n.t("menu.option", index=opt["index"], label=opt["label"],
                            quota=opt["quota"], note=note, mark=mark))
    lines.append(i18n.t("menu.give_up", index=len(options) + 1))
    return lines


def _menu_prompt(head: str, args, calc: dict, options: list, warning: str = "") -> str:
    return (
        head + "\n" + _file_brief(args, calc, warning)
        + "\n\n" + i18n.t("menu.question") + "\n"
        + "\n".join(_option_lines(options))
    )


# 订单号自带家族前缀：文档翻译是 TR、视频翻译是 VO。拿视频单号去调文档接口，
# 上游只回一句 403「访问被拒绝」，模型会把它当权限问题——实测提交完视频紧接着
# 调 wait_for_translation 就是这样卡住的，用户看到的是「遇到访问限制」。前缀对
# 不上就在这里拦下，顺带把该调的工具名说出来。前缀不认识（改写单等）一律放行，
# 交给上游判。
_DOC_ORDER_TOOLS = {
    "get_document_translation_status",
    "get_document_translation_result",
    "wait_for_translation",
}
_VIDEO_ORDER_TOOLS = {
    "get_video_translation_status",
    "wait_for_video_translation",
    "cancel_video_translation",
    "get_video_subtitles",
    "rewrite_video_subtitles",
}
# 两条线上功能对得上的那几个，直接报出替代工具名
_ORDER_TOOL_SWAP = {
    "wait_for_translation": "wait_for_video_translation",
    "get_document_translation_status": "get_video_translation_status",
    "get_document_translation_result": "get_video_translation_status",
    "wait_for_video_translation": "wait_for_translation",
    "get_video_translation_status": "get_document_translation_status",
}


def _order_family_error(tool_name: str, args: dict) -> Optional[dict]:
    """订单号的家族和工具对不上就别送上去换一句 403"""
    order_no = args.get("order_no")
    if not isinstance(order_no, str):
        return None
    prefix = order_no[:2].upper()
    if tool_name in _DOC_ORDER_TOOLS and prefix == "VO":
        got, want = "视频", "文档"
    elif tool_name in _VIDEO_ORDER_TOOLS and prefix == "TR":
        got, want = "文档", "视频"
    else:
        return None
    instead = _ORDER_TOOL_SWAP.get(tool_name)
    return {
        "code": "400",
        "data": None,
        "orderFamily": "video" if prefix == "VO" else "document",
        "msg": (
            f"没有查询：{order_no} 是{got}翻译的订单号（{prefix} 开头），"
            f"而 {tool_name} 只认{want}翻译的订单号。"
            + (f"请改调 {instead}，参数照旧。" if instead
               else f"这个工具没有{got}翻译的对应版本，请改用{got}翻译那一组工具。")
            + "两条线的订单号不通用，硬送上去只会拿到一句 403「访问被拒绝」——"
              "那不是权限问题，重试、换密钥、换单号都没有用。"
        ),
    }


async def _reject(msg: str) -> dict:
    """参数校验不过就返回一条模型看得懂的错误，而不是抛异常——
    让它回去问用户，而不是把栈信息糊给用户看。"""
    return {"code": "400", "msg": msg, "data": None}


async def _ask_user_to_confirm(session, message: str) -> Optional[bool]:
    """服务端直接问一句「做还是不做」。发不出去返回 None，交给两步握手。"""
    yes, no = i18n.t("rewrite.yes"), i18n.t("rewrite.no")
    try:
        result = await session.elicit_form(
            message=message,
            requested_schema={
                "type": "object",
                "properties": {
                    "choice": {
                        "type": "string",
                        "title": i18n.t("rewrite.title"),
                        "enum": [yes, no],
                        "description": i18n.t("rewrite.desc"),
                    }
                },
                "required": ["choice"],
            },
        )
    except Exception:
        return None
    if getattr(result, "action", None) != "accept":
        return False
    return (getattr(result, "content", None) or {}).get("choice") == yes


async def _rewrite_quota(client, order_no: str) -> dict:
    """改写要花多少额度。免费，扣费前应当先问一次。"""
    result = await client.video_rewrite_quota_calculate(order_no)
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    quota = data.get("translateQuota")
    result = dict(result)
    result["data"] = {"quota": quota}
    result["msg"] = (
        i18n.t("rewrite.quota", quota=quota)
        + "改写是一单新的扣费任务，不是免费返工——请把这个数字告诉用户，"
        "得到明确同意后再调 rewrite_video_subtitles。"
    )
    return result


async def _submit_video_rewrite(client, args) -> dict:
    """改写会真实扣费，闸门和 translate_video 同一套：先试算、再由用户本人确认。

    之前这条路只有工具描述里一句「提交前请把预计消耗告诉用户」。同样的祈使句在
    translate_video 上已经被实测证伪过——模型会试算完直接提交。何况走到改写这一步
    的场景恰恰是「用户刚校对完字幕想重新生成」，正是最容易连点两下的时候。
    """
    order_no = args["order_no"]
    source_txt = args["source_subtitles_txt"]
    target_txt = args["target_subtitles_txt"]
    fingerprint = rewrite_fingerprint(order_no, source_txt, target_txt)
    token = args.get("confirm_token")

    if token:
        accepted, why = take_submit_confirm(token, fingerprint)
        if not accepted:
            return await _reject(why + "（本次没有提交、没有扣费）")
        return await client.submit_video_rewrite(
            order_no, source_txt, target_txt, args.get("video_task_param")
        )

    calc = await _rewrite_quota(client, order_no)
    if str(calc.get("code")) != "200":
        return calc
    quota = (calc.get("data") or {}).get("quota")
    prompt = i18n.t("rewrite.prompt", quota=quota)

    session = _elicitation_session()
    if session is not None:
        answer = await _ask_user_to_confirm(session, prompt)
        if answer is False:
            return {
                "code": "400",
                "data": {"submitted": False, "charged": False, "userDecision": "declined"},
                "msg": (
                    "用户在确认框里选择了放弃。没有提交、没有扣费。请如实告诉用户已取消，"
                    "不要换个说法再问一遍，也不要再调用本工具。"
                ),
            }
        if answer is True:
            return await client.submit_video_rewrite(
                order_no, source_txt, target_txt, args.get("video_task_param")
            )

    # 问不到真人（客户端没声明 elicitation 能力、或发起失败）：退回两步握手
    return {
        "code": "409",
        "data": {
            "submitted": False,
            "charged": False,
            "quota": quota,
            "confirmToken": issue_submit_confirm(fingerprint),
            "userPrompt": prompt,
        },
        "msg": (
            "还没有提交、没有扣费。请把 data.userPrompt 原样发给用户，然后停下来等答复。"
            "同意了就带 data.confirmToken 作为 confirm_token 重新调用本工具，"
            "并且 order_no 和两份字幕正文都要和这一次完全一致——确认码绑定的就是"
            "用户看过的那一版字幕，改一行都会被拒。用户说不做就到此为止。"
            "确认码一次性、15 分钟有效。"
        ),
    }


async def _submit_video_translate(client, args):
    """translate_video 会真实扣费，所以确认、配音选择、重复提交都在这里硬拦，
    光靠工具描述里的祈使句拦不住（实测模型会试算完直接提交，任务失败后也会
    自己换个参数直接重提）。"""
    voice_role = args.get("voice_role")
    # 带着确认码回来时，配音/字幕以码为准：用户点的是菜单里那一格，模型手上
    # 那一组只是它自己的建议，两者不一致时该让步的是模型。
    picked_meta = (peek_submit_confirm(args.get("confirm_token")) or {}).get("meta") or {}
    if picked_meta:
        voice_role = picked_meta.get("voiceRole", voice_role)
        args = {**args, "voice_role": voice_role, "subtitle_type": picked_meta.get("subtitleType")}
    if voice_role not in VOICE_ROLE_CHOICES:
        return await _reject(
            "voice_role 必须显式传 No 或 clone，不能省略。请先问用户是否开启同声翻译"
            "（配音）：No=不配音、保留原声只做字幕；clone=克隆原说话人音色配音，"
            "且 subtitle_type≠0 时额度翻倍。拿到用户的选择后再重新调用本工具。"
        )
    if args.get("user_confirmed") is not True:
        return await _reject(
            "本次提交会真实扣减额度，必须先用 calculate_video_translation_quota 试算、"
            "把预计消耗和配音选项一并告诉用户并得到明确同意，再带 user_confirmed=true "
            "重新调用。请不要替用户做决定。"
        )

    # 试算和提交必须是同一组参数，否则报给用户的预算和实扣可以差一倍。
    subtitle_type = args.get("subtitle_type", 1)
    calc = recent_quota_calc(voice_role, subtitle_type)
    if calc is None:
        return await _reject(
            f"找不到和本次提交对得上的试算记录（配音={voice_role}、字幕={subtitle_type}）。"
            "请先用文件的真实时长（ffprobe 读出来的，不能按文件大小猜）调 "
            "calculate_video_translation_quota，voice_role / subtitle_type 传成和本次提交"
            "完全一样的值，把试算结果告诉用户之后再提交。没有试算就提交，等于没跟用户"
            "说过要扣多少。"
        )

    # 时长上限的判据和服务端逐字一致（VideoTranslateServiceImpl：
    # videoDuration/1000 > videoDurationLimit*60）。放在这里拦，是因为再往下就是
    # 上传好的文件真的要提交了；查不到限额就放行，让服务端自己去拒。
    snapshot = await client.account_snapshot()
    duration_ms = calc.get("durationMs")
    over = video_duration_over_limit(duration_ms, snapshot)
    if over:
        vip = (snapshot.get("vip") or {}).get("name") or "当前档位"
        return await _reject(
            "没有提交、没有扣费。"
            + i18n.t("limit.over", duration=_format_duration((duration_ms or 0) / 1000),
                     vip=vip, limit=over)
            + "请把这句话告诉用户，由他决定是剪短再传还是升级会员——不要自己改参数重试。"
        )
    # 余额不够只提示、不拦：免费额度另有按月重置和月度时长两本账，本地算不全，
    # 真正说了算的是服务端。把数字摆给用户，让他自己判断。
    quota_warning = ""
    available = (snapshot.get("quota") or {}).get("wallet")
    needed = calc.get("quota")
    if available is not None and needed is not None and available < needed:
        quota_warning = i18n.t("warn.quota", available=available, needed=needed)

    object_key = args["source_file_object_key"]
    fingerprint = confirm_fingerprint(
        object_key, args["target_language"], voice_role, subtitle_type
    )
    token = args.get("confirm_token")
    # 带着确认码回来的是用户已经挑过的那一格，不用再摆一次菜单（也省掉 7 次试算）
    options = None if token else await _build_options(client, args, voice_role, subtitle_type, calc)

    # 同一份文件、同一目标语言短时间内再提就是重做，不管上一单是失败、取消还是
    # 想换参数——都会再扣一次费。必须显式认领上一单的单号才放行。重做的确认码在
    # 这里一并发出去：失败原因只有模型知道（上游常常不给），得由它转述，所以这条
    # 不走服务端弹窗；但用户的答复照样要用确认码带回来，省得重做和提交问两遍。
    prev = recent_video_submit(object_key, args["target_language"])
    if prev and prev.get("orderNo") and (
        args.get("retry_confirmed") is not True
        or args.get("retry_of_order_no") != prev["orderNo"]
    ):
        return {
            "code": "409",
            "data": {
                "submitted": False,
                "charged": False,
                "previousOrderNo": prev["orderNo"],
                "estimatedQuota": calc.get("quota"),
                "options": _slim_options(options),
                "userPrompt": _menu_prompt(
                    i18n.t("menu.head.retry", order=prev["orderNo"]),
                    args, calc, options, quota_warning,
                ),
            },
            "msg": (
                f"没有提交、没有扣费。这份文件刚提交过一单（{prev['orderNo']}，目标语言 "
                f"{args['target_language']}，扣了 {prev['quota']} 额度）。请先把上一单的结果或"
                "失败原因如实告诉用户（不知道就说不知道，不要推测），再把 data.userPrompt 原样"
                "发给他，然后停下来等答复——重做与否、以及重做成什么样，都由用户决定。用户挑了"
                "第几项，就用 data.options 里那一项的 voiceRole / subtitleType / confirmToken 三个值"
                f"（必须同属一项）加上 retry_of_order_no=\"{prev['orderNo']}\"、retry_confirmed=true "
                "重新调用本工具——只要 confirmToken 传对，配音和字幕会按那一格自动定；"
                "选放弃就到此为止。菜单以外的组合不要自己造。"
            ),
        }

    # 到这里参数都合法了，最后一道：确认必须来自用户本人。user_confirmed 只证明
    # 模型愿意声称问过，所以这里要么服务端亲自问（elicitation），要么先拒一次、
    # 把整张菜单交出去，逼模型把选项转述给用户再回来。
    if token:
        accepted, why = take_submit_confirm(token, fingerprint)
        if not accepted:
            return await _reject(why + "（本次没有提交、没有扣费）")
    else:
        session = _elicitation_session()
        picked = None
        if session is not None:
            picked = await _ask_user_to_pick(session, args, calc, options, quota_warning)
        if picked == "declined":
            return {
                "code": "400",
                "data": {"submitted": False, "charged": False, "userDecision": "declined"},
                "msg": (
                    "用户在确认框里选择了放弃（或直接关掉了确认框）。任务没有提交、没有扣费。"
                    "请如实告诉用户已取消，不要换个参数再试一次，也不要再调用本工具。"
                ),
            }
        if isinstance(picked, dict):
            # 用户当场挑的那一格为准，模型带上来的那组只是建议
            voice_role = picked["voiceRole"]
            subtitle_type = picked["subtitleType"]
        else:
            # 问不到真人（客户端没声明 elicitation 能力、或发起失败）：
            # 退回两步握手，把整张菜单和每格的确认码还给用户。
            return {
                "code": "409",
                "data": {
                    "submitted": False,
                    "charged": False,
                    "options": _slim_options(options),
                    "userPrompt": _menu_prompt(
                        i18n.t("menu.head.submit"),
                        args, calc, options, quota_warning,
                    ),
                },
                "msg": (
                    "还没有提交、没有扣费。请把 data.userPrompt 原样发给用户，然后停下来等他挑——"
                    "配音和字幕做成什么样、扣多少额度，是用户的决定，不是你的。用户挑了第几项，"
                    "就把 data.options 里那一项的 confirmToken 传回来重新调用本工具——"
                    "配音和字幕按那一格自动定，不用你去对；选放弃就到此为止、不要再调。"
                    "菜单以外的组合不要自己造，确认码一次性、15 分钟有效。"
                ),
            }

    result = await client.submit_video_translate(
        args["source_language"],
        args["target_language"],
        object_key,
        args["video_file_name"],
        _build_video_task_param(
            voice_role,
            subtitle_type,
            args.get("video_task_param"),
        ),
    )

    # 上游的 31004「文件上传失败」指的是这个 objectKey 底下没有文件，错其实发生在
    # 两步之前——链接签发了，字节从没传。实测模型拿到预签名链接就宣布「文件已上传」
    # 直接来提交，然后对着 31004 束手无策。这里把话说到点子上。
    if str(result.get("code")) == "31004" and key_never_uploaded(object_key):
        result = dict(result)
        result["diagnosis"] = (
            "这个 objectKey 只签发过上传链接，本服务从没经手过它的字节——文件其实"
            "没有传上去。回到上一步：执行那条 uploadCommand（只替换文件路径），看到"
            "「上传结果 HTTP 200」之后再回来提交。如果命令报 Failed to connect 或 "
            "Could not resolve host，那是你这侧的沙箱在拦网络，请求根本没发出去，"
            "换个 objectKey 重来也一样——把同一条命令用提升权限的方式再跑一次。"
            "注意：光拿到预签名链接不等于文件已上传，不要把它当成上传成功。"
        )
    return result


# 预签名回执里真正要交出去的就这几项。签名 URL 已经原样嵌在 uploadCommand 里，
# 再单独给一遍就是七百字符的重复；contentDisposition / encodeFileName / storageType
# 也只是为了拼那条命令而存在，命令拼好了它们就没人看了。
_PRESIGNED_KEEP = ("fileName", "objectKey", "uploadCommand", "progressLogPath", "uploadNote")


def _slim_presigned(result: dict) -> dict:
    """upload_document / upload_video 的返回投影

    和详情类接口一样只能在这一层裁：uploadCommand 是在 client 那两个方法里拼的，
    要用到 persignedUploadUrl / encodeFileName，在里面裁就没法拼命令了。
    """
    items = result.get("data")
    if not isinstance(items, list):
        return result
    result = dict(result)
    result["data"] = [
        {k: item[k] for k in _PRESIGNED_KEEP if item.get(k) is not None}
        if isinstance(item, dict) else item
        for item in items
    ]
    return result


async def _upload_presigned(client, file_name_list: list, kind: str) -> dict:
    getter = (
        client.video_batch_presigned_upload_url
        if kind == "video"
        else client.doc_batch_presigned_upload_url
    )
    return _slim_presigned(await getter(file_name_list))


async def _video_quota(client, duration_ms: float, voice_role: str, subtitle_type: int) -> dict:
    """试算回执瘦身：上游给的五个数里四个是同一件事的不同说法

    translateQuota / videoDurationTranslateQuota / thirtySecondQuota 在不配音时
    是同一个值，videoDuration 在这里指的是计费单位数（不是时长，叫这个名字纯属
    上游的坑），quotaCoefficient 是倍率。要点全在 quotaNote 那句话里了。
    """
    result = await client.video_translate_quota_calculate(duration_ms, voice_role, subtitle_type)
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    result = dict(result)
    result["data"] = {
        "quota": data.get("translateQuota"),
        "billingUnits": data.get("videoDuration"),
    }

    # 试算是上传之前就会做的一步，超限在这里说最省事——否则用户先传几十 MB，
    # 提交时才被服务端以 VIDEO_DURATION_LIMIT 拒掉。
    snapshot = await client.account_snapshot()
    limit = video_duration_over_limit(duration_ms, snapshot)
    if limit:
        result["limitWarning"] = (
            f"这个视频 {_format_duration(duration_ms / 1000)}，超过了当前会员档位的"
            f"单个视频上限 {limit} 分钟，现在提交会被服务端直接拒绝。请先把这件事"
            "告诉用户，让他决定是剪短还是升级会员，不要先上传再说。"
        )
    quota_info = snapshot.get("quota") or {}
    available = quota_info.get("wallet")
    needed = data.get("translateQuota")
    if available is not None and needed is not None and available < needed:
        result["quotaWarning"] = (
            f"账户可用额度 {available}，这一单要 {needed}，不够。请照实告诉用户，"
            "不要提交——提交会被服务端拒绝。"
        )
    return result


async def _check_pdf_ocr(client, file_object_key: str, file_name: str = "") -> dict:
    """上传后立刻问一次「是不是扫描件」，结果留给 translate_document 复用"""
    if file_name and not client_is_pdf(file_name):
        return {
            "code": "400",
            "msg": f"{file_name} 不是 PDF。扫描件这个概念只对 PDF 成立，其他格式不用检测，直接提交翻译即可。",
        }
    # 分类要把整个 PDF 下下来，大文件能跑一分钟。发出去之后只等一小会儿，
    # 等不到就先回一句「在测了」——调用方正好去问语言和模型，别在这儿干耗。
    client.start_ocr_detection(file_object_key, file_name)
    info = await client.detect_ocr_for_key(file_object_key, file_name, timeout=_OCR_CHECK_WAIT)
    if info is None and not client_ocr_of(file_object_key):
        return {
            "code": "202",
            "msg": (
                "还在检测这份 PDF 是不是扫描件（服务端要把整个文件下下来分析，大文件要一会儿）。"
                "不用等：可以先和用户确认语言、模型，提交 translate_document 时会自动带上结果；"
                "想现在知道就过几秒再调一次本工具。"
            ),
            "data": {"fileObjectKey": file_object_key, "detecting": True},
        }
    info = info or client_ocr_of(file_object_key)
    if not info:
        return {
            "code": "500",
            "msg": (
                "没测出来（检测接口没答上来）。这不影响提交，照常调 translate_document 就行；"
                "如果译文出来是空白，那多半是扫描件，带 is_ocr=1 重提一次。"
            ),
        }
    scanned = info.get("isOcr") == 1
    return {
        "code": "200",
        "msg": (
            "这是扫描件，翻译会自动走 OCR——扣的是 OCR 额度，和普通翻译不是同一本账，"
            "请把这句告诉用户。提交时不用管 is_ocr，服务端已经记住了。"
            if scanned
            else "这是文本版 PDF，按普通翻译走，不会动 OCR 额度。提交时不用管 is_ocr。"
        )
        + (DOUBLE_DECK_NOTE if info.get("isDoubleDeck") == 1 else ""),
        "data": info,
    }


async def _account_status(client, refresh: bool = False) -> dict:
    """额度 + 会员权益 + 各项限额，一次说清"""
    snapshot = await client.account_snapshot(refresh=refresh)
    if not snapshot.get("ok"):
        return {
            "code": "500",
            "msg": (
                "查不到账户信息："
                + (snapshot.get("error") or "上游没有返回")
                + "。请如实告诉用户查不到，不要拿之前的数字或者估算值顶上。"
            ),
            "data": snapshot,
        }

    quota = snapshot.get("quota") or {}
    vip = snapshot.get("vip") or {}
    limits = snapshot.get("limits") or {}
    parts = []
    if quota:
        # 两个数分开报，不给合计：translateQuota 本来就含了今日免费的消耗，
        # 加一次就是重复计（实测最多虚报 1200）。
        parts.append(f"可用额度 {quota.get('wallet')}")
        parts.append(f"今日免费已用 {quota.get('freeUsed')}/{quota.get('freeTotal')}")
        # 上游把 OCR 这组额度单独发一份，但 2026-09-04 在测试环境实测：一页扫描件
        # （isOcr=1）和一页文本 PDF（isOcr=0）各翻一单，四个字段的增减一模一样，
        # 连非 OCR 那单都把 useFreeOcrTranslateQuota 加了 1。数照报，但它不是
        # 另一份能加上去的余额。
        if quota.get("ocrWallet") is not None:
            parts.append(
                f"OCR 额度 {quota.get('ocrWallet')}"
                f"（今日免费已用 {quota.get('ocrFreeUsed')}/{quota.get('ocrFreeTotal')}）"
            )
        if quota.get("advanced") is not None:
            parts.append(f"高级模型额度 {quota.get('advanced')}")
    if vip.get("name"):
        parts.append(
            f"会员 {vip['name']}"
            + (f"，{vip['expiresOn']} 到期" if vip.get("expiresOn") else "")
        )
    if limits.get("videoDurationMinutes"):
        parts.append(f"单个视频最长 {limits['videoDurationMinutes']} 分钟")
    if limits.get("videoConcurrency"):
        parts.append(f"视频任务同时最多 {limits['videoConcurrency']} 个")
    msg = (
        "；".join(parts)
        + "。这些数字请原样转述，一个都不要相加：可用额度那个数已经含了今日免费的"
        "消耗，报「还剩多少」就报它；「今日免费已用」是用量计数，不是另一份余额，"
        "OCR 那组同理（实测和可用额度同步增减，走不走 OCR 都一样）。额度是账户余额，"
        "和某一单的实扣不是一回事。免费用户另有「每月累计视频时长」上限，"
        "本接口看不到，超了要到提交时才会被拒。"
    )
    # 上游偶尔会把免费额度回成对不上的数。原样转述的要求在前，这里必须把
    # 「这一项不可信」一并说出去，否则模型会照着念一个自相矛盾的余额。
    if snapshot.get("quotaSuspect"):
        msg += "⚠️ 但免费额度这一项这次对不上：" + snapshot["quotaSuspect"] + (
            "报余额时请把这句一并告诉用户，不要只报数字，也不要拿它去替用户判断"
            "够不够翻下一单。"
        )
    return {"code": "200", "msg": msg, "data": snapshot}


async def _video_status(client, order_no: str) -> dict:
    """get_video_translation_status 的返回投影

    裁剪只能放在这一层：client.get_video_translate_detail 还被 wait 拿去读
    sourceFileUrl / objectKey 这些字段，在它里面裁会把自己人的东西也砍掉。
    """
    result = await client.get_video_translate_detail(order_no)
    data = result.get("data")
    if not isinstance(data, dict):
        return result

    done = data.get("status") == VIDEO_STATUS_DONE
    # 做过字幕改写的，产物在改写子记录里，顶层那份是改写前的
    urls, _rewrite = video_products(data)
    target = urls.get("targetFileUrl")
    slim = slim_video_record(data)
    if done and target:
        slim["expiresAt"] = _expires_at(target)
        slim["downloadNote"] = (
            "targetFileUrl 是译制后的视频，targetSubtitlesUrl 是译文字幕。"
            + _URL_VERBATIM_NOTE
            + _expiry_note(target)
        )
        slim["targetFileUrl"] = target
        if urls.get("targetSubtitlesUrl"):
            slim["targetSubtitlesUrl"] = urls["targetSubtitlesUrl"]

    result = dict(result)
    result["data"] = slim
    return result


_DOC_URL_NOTE = (
    "已略去记录里的下载地址（{fields}）：那是上游默认生成的带水印版本，"
    "而水印开关只对 get_document_translation_result 生效。要下载就拿 "
    "translateOrderNo 调它，地址是现签发的，不会过期。"
)


async def _doc_status(client, order_no: str) -> dict:
    """get_document_translation_status 的返回投影，同样只能在这一层裁"""
    result = await client.get_translate_file_detail(order_no)
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    slim, dropped = strip_long_urls(data)
    if dropped:
        slim["downloadNote"] = _DOC_URL_NOTE.format(fields="、".join(dropped))
    result = dict(result)
    result["data"] = slim
    # 完成的 PDF / EPUB 还能取对照版式。这条查询也是交付现场之一，同样要主动报，
    # 不能只在 wait_for_translation 那一条路上说。
    if data.get("status") == TASK_STATUS_DONE:
        variants = offered_variants(data)
        offer = comparison_offer(data, variants)
        if offer:
            slim["comparisonVariants"] = list(variants)
            result["msg"] = (
                offer + "——这一句请主动告诉用户（他不问也要说）。他要哪一版，"
                "就用 get_document_translation_result 按对应 url_type 取："
                "3=横向对照（左右并排）、4=纵向对照（上下排列）。"
            )
    return result


def _strip_doc_records(result: dict) -> dict:
    """列表是拿来浏览的：十条记录挂着几十条几百字符的签名地址，没有一条用得上"""
    page = result.get("data")
    # 分页接口给的是 {"records": [...]}，按批次号查的直接就是一个数组
    records = page.get("records") if isinstance(page, dict) else page
    if not isinstance(records, list) or not records:
        return result
    dropped = set()
    slimmed = []
    for record in records:
        slim, gone = strip_long_urls(record)
        dropped.update(gone)
        slimmed.append(slim)
    if isinstance(page, dict):
        page["records"] = slimmed
    else:
        result["data"] = slimmed
    if dropped:
        result["listNote"] = _DOC_URL_NOTE.format(fields="、".join(sorted(dropped)))
    return result


async def _list_documents(client, page_num: int, page_size: int, status) -> dict:
    return _strip_doc_records(
        await client.search_translate_file_page(page_num, page_size, status)
    )


async def _batch_documents(client, batch_no: str) -> dict:
    return _strip_doc_records(await client.search_translate_file_by_batch_no(batch_no))


def _blocks(result) -> list:
    """工具结果拆成内容块

    绝大多数工具就一个 JSON 块。带 sayToUser 的（目前是文档翻译完成时那句「还能
    要对照版」）多发一个纯文案块，且排在 JSON 前面：这句话夹在 msg 里、或者挂在
    几百字符签名链接后面的 downloadNote 尾巴上，实测被模型丢过两次。
    """
    say = result.pop("sayToUser", None) if isinstance(result, dict) else None
    blocks = []
    if say:
        blocks.append(TextContent(type="text", text=say))
    blocks.append(TextContent(type="text", text=_to_json(result)))
    return blocks


def _to_json(result) -> str:
    """工具结果统一序列化为 JSON（中文不转义），避免输出 Python repr"""
    import json
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


# 工具定义（stdio 与 HTTP 两种传输共用）
TOOLS = [
    Tool(
        name="get_supported_languages",
        description="获取支持的语言列表（语言码 -> 显示名）。请在调用 translate_document 之前调用此工具，让用户选择源语言和目标语言。",
        inputSchema={
            "type": "object",
            "properties": {
                "display_locale": {
                    "type": "string",
                    "description": "显示名使用的界面语言，默认 zh。可选 en、zh、zh-Hant、ja、ko、fr、ru、de、ar"
                }
            }
        }
    ),
    Tool(
        name="get_model_list",
        description="获取当前用户可用的翻译模型列表。请在调用 translate_document 之前调用此工具，并让用户选择一个模型。返回的 data 是对象列表：model 是提交时要填的模型名，coefficient 是计费倍率——倍率 3 的模型翻同样的量扣三倍额度，请把倍率一并告诉用户再让他选。locked 里是当前会员档位还用不了的模型，不要拿它们去提交。",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="get_account_status",
        description="查询账户的可用额度、会员档位和各项限额。要报余额、要判断「够不够翻这一单」时用它——额度数字必须来自本工具，不要从之前某一单的实扣去推算，两者不是一回事（那是本次消耗，不是余额）。返回里 quota.wallet 是可用额度，报余额就报它——它已经含了今日免费的消耗，别再拿 freeUsed/freeTotal 去加。freeUsed/freeTotal 是今日免费额度的用量计数，ocrWallet/ocrFreeUsed 是上游单独发的一组 OCR 数字（实测和 wallet 同步增减，走不走 OCR 都一样），这两组都不是另一份余额。advanced 是高级模型额度（get_model_list 里 coefficient>1 的那几个），归属还没实测过。limits 是当前会员档的硬限制：videoDurationMinutes 单个视频最长多少分钟、videoConcurrency 视频任务能同时跑几个、uploadFileSizeMB 单文件多大。这些限制服务端会真的按它拒绝提交，所以准备翻一个长视频之前先看一眼。注意免费用户另有「每月累计视频时长」上限，本接口看不到，只有提交时才会撞上。查不到时返回 code=500，请如实告诉用户查不到，不要拿估算值顶上。",
        inputSchema={
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "跳过 5 分钟缓存重新查。刚扣过费想看最新余额时传 true，平时不用传。"
                }
            }
        }
    ),
    Tool(
        name="upload_document",
        description="取文档的预签名上传链接。拿到链接后原样执行返回的 uploadCommand（只替换其中的文件路径，Content-Disposition 一个字符都不能改，否则 S3 报 SignatureDoesNotMatch）。上传是访问外网，你那边默认没有网络权限的话，第一次执行就把联网权限一起要上，别先试一次失败再补申请。上传成功后用返回的 objectKey 作为 fileObjectKey 调 translate_document。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_name_list": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "文件名列表，如 ['contract.pdf', 'doc.docx']"
                }
            },
            "required": ["file_name_list"]
        }
    ),
    Tool(
        name="check_pdf_ocr",
        description="判断一个已经传上去的 PDF 是不是扫描件（图片版）。**PDF 传完就调它**——看到「上传结果 HTTP 200」之后、提交翻译之前，把 objectKey 传进来。只有 PDF 需要，其他格式不用调。返回 isOcr=1 表示扫描件、翻译要走 OCR（扣的是 OCR 额度，和普通翻译不是同一本账，请把这点告诉用户），isOcr=0 是文本版 PDF；isDoubleDeck=1 表示双层 PDF（扫描图上盖了一层文字）——这种直接翻会翻到那层往往是错的文字上，返回的 msg 里会给出拍平工具的链接，请原样转述给用户，让他先拍平再重新上传。结果会被记住，随后 translate_document 直接复用、不会重复检测，也不用你把 is_ocr 填回去（检测只负责把扫描件标出来，不会去关掉别人显式打开的 OCR）。大文件可能要等十几秒到一分钟，那是服务端在下载并分析整个 PDF，属正常。测不出来时返回 code=500——那不影响提交，照常翻就是了。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_object_key": {
                    "type": "string",
                    "description": "上传时拿到的 objectKey"
                },
                "file_name": {
                    "type": "string",
                    "description": "文件名，只用于返回里的可读说明，可不传"
                }
            },
            "required": ["file_object_key"]
        }
    ),
    Tool(
        name="translate_document",
        description="提交文档翻译任务。请先调用 get_model_list 获取可用模型，调用 get_supported_languages 获取支持的语言列表，然后让用户选择模型和目标语言。返回中的 orders[].translateOrderNo 即订单号，直接用它调 wait_for_translation，无需再查列表。图片（png/jpg/jpeg）也走这个工具——服务端把它们当 IMAGE 类型，按 1 页计费，且一律走 OCR（扣的是 OCR 额度），不需要另外的图片翻译接口。支持的格式：PDF / DOCX / PPTX / XLSX / TXT / EPUB / 图片。提交前本工具会对 file_list 里的 PDF 自动判定是不是扫描件（只有 PDF 有这个概念），据此填 OCR 开关，结果在返回的 msg 和 ocrDetection 里——请把「走没走 OCR」原样告诉用户，那关系到扣哪一本额度。**若返回 code=202，表示判定还没出来，本次没有提交、没有扣费**（data.submitted / data.charged 都是 false，data.detecting 是还在测的文件）：等十几秒用完全相同的参数再调一次本工具即可，不要重新上传文件、也不要改参数。这一步挡着是因为判错两边都要付代价：扫描件按普通 PDF 翻会出一片空白，OCR 又扣另一本额度。确实等不及、或者反复 202 一直不出结果，就显式传 is_ocr（0=按普通 PDF 翻，1=强制整批走 OCR）绕过它。若返回非 200（如 600 系统繁忙），说明是翻译服务侧的问题而非上传问题：用相同参数重试本工具即可，不要重新上传文件。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_list": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fileName": {"type": "string"},
                            "fileObjectKey": {"type": "string"}
                        }
                    },
                    "description": "文件列表，每个文件包含 fileName 和 fileObjectKey"
                },
                "source_language": {
                    "type": "string",
                    "description": "源语言代码，如 'en', 'zh-CN', 'ja', 'AnyLanguage'。如果用户未指定，请让用户从 get_supported_languages 返回的列表中选择。"
                },
                "target_language": {
                    "type": "string",
                    "description": "目标语言代码，如 'zh-CN', 'en', 'ja'。这是必填项，如果用户未指定，请让用户从 get_supported_languages 返回的列表中选择。"
                },
                "model": {
                    "type": "string",
                    "description": "翻译模型。这是必填项，请先调用 get_model_list 获取当前用户可用的模型列表，然后让用户选择。"
                },
                "is_ocr": {
                    "type": "integer",
                    "description": "是否启用 OCR，0=否，1=是。**不要自己判断，正常情况下不要传**：提交前服务端会对每个 PDF 调分类接口，按真实结果逐个标记，比看文件名靠谱得多。这个参数的真实含义是「强制整批走 OCR」——服务端见到 1 就把批次里每个 PDF 都按 OCR 记账，不再看检测结果，文本版那几份等于白扣 OCR 额度。所以只有用户明确要求强制 OCR 时才传 1；传了不会被改掉，但返回里会提醒你这和检测结果不符。注意 OCR 扣的是 OCR 额度，和普通翻译不是同一本账。"
                },
                "terminology_collection_id": {
                    "type": "string",
                    "description": "术语表 ID（选填）。带上之后，这一批文件里凡是命中术语表的词都按表里指定的译法翻。**不要自己编，也不要猜**：这个 ID 只能由用户提供——他登录 belindoc.com 网页端、在术语库页面拿到。本服务没有列出术语表的工具，因为上游管理术语表的那几个接口认的是网页登录态而不是 API Key。用户没主动提术语表就别传这个参数，更不要为了它去打断用户。另外上游收到这个 ID 既不校验归属也不校验存在：写错或写了个不存在的，提交照样成功、翻译照常跑，只是术语表静默不生效，事后没有任何地方看得出来——所以只转述用户给的原值，一个字符都不要改。"
                }
            },
            "required": ["file_list", "source_language", "target_language", "model"]
        }
    ),
    Tool(
        name="get_document_translation_status",
        description="查询文档翻译任务状态。返回里不带下载地址——详情给的是上游默认生成的带水印版本，水印开关只对 get_document_translation_result 生效，要下载一律走那个工具。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="get_document_translation_result",
        description="获取文档翻译结果下载链接。url_type 决定版式：1=原文、2=纯译文（默认）、3=横向对照（左右并排，仅 PDF）、4=纵向对照（原文与译文上下排列，仅 PDF 与 EPUB）。对照版是取的时候现合成的，第一次取可能要多等一会儿——慢是正常的，别当成失败去重试，更不要因此改回纯译文；也正因为要合成，翻译完成时不会替用户预先取好，用户点名要哪一版再来调。要译文不要传 1。返回的 url 走 CloudFront，url2 为国内兜底线路。两条链接都带签名参数，转述给用户时必须连问号后面的 Signature/Key-Pair-Id/expires/sign 一起原样给全，截断或缩短会导致 403 MissingKey。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                },
                "url_type": {
                    "type": "integer",
                    "description": "1=原文, 2=纯译文(默认), 3=横向对照/左右并排(仅 PDF), 4=纵向对照/上下排列(仅 PDF 与 EPUB)"
                },
                "is_watermark": {
                    "type": "integer",
                    "description": "0=无水印（默认），1=带水印。无水印需要账号权限，若返回失败提示权限不足再改传 1。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="list_document_translations",
        description="查询文档翻译任务列表。只用来找单号和看状态，不带下载地址；要下载用 get_document_translation_result，它才认 is_watermark，链接也是现签发的。",
        inputSchema={
            "type": "object",
            "properties": {
                "page_num": {
                    "type": "integer",
                    "description": "页码，默认 1"
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页数量，默认 10"
                },
                "status": {
                    "type": "integer",
                    "description": "任务状态过滤，可选"
                }
            }
        }
    ),
    Tool(
        name="get_document_translation_by_batch",
        description="通过批次号查询翻译任务",
        inputSchema={
            "type": "object",
            "properties": {
                "batch_no": {
                    "type": "string",
                    "description": "批次号"
                }
            },
            "required": ["batch_no"]
        }
    ),
    Tool(
        name="upload_video",
        description="取视频的预签名上传地址。拿到地址后原样执行返回的 uploadCommand（只替换文件路径，Content-Disposition 一个字符都不能改，否则 S3 报 SignatureDoesNotMatch）。预签名地址仅 10 分钟有效，取到就传。注意视频和文档走的是不同端点、不同存储路径，视频不能用 upload_document 取链接。上传成功后用返回的 objectKey 作为 source_file_object_key 调 translate_video。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_name_list": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "视频文件名列表"
                }
            },
            "required": ["file_name_list"]
        }
    ),
    Tool(
        name="translate_video",
        description="提交视频翻译任务。⚠️ 本工具会真实扣减账户额度并计入调用次数，所以提交是一次**两步**调用，第一次一定不会提交：\n(1) 先用文件的真实时长调 calculate_video_translation_quota 试算，voice_role / subtitle_type 必须和接下来提交的值完全一致（对不上会被拒绝提交）；\n(2) 带 voice_role 和 user_confirmed=true 调本工具，但不带 confirm_token——本工具此时不提交、不扣费，只返回 409、一段 data.userPrompt 和一张 data.options 菜单（配音 × 字幕的全部组合，每格带自己的额度和 confirmToken）；\n(3) 把 data.userPrompt 原样发给用户，等他在菜单里挑一项或选放弃。做成什么样、扣多少额度是用户的决定，不要替他选，也不要只转述你自己那一组；\n(4) 用户挑了第几项，就用 data.options 里那一项的 voiceRole / subtitleType / confirmToken 三个值（必须同属一项，不能混、不能造菜单外的组合）重调一次，这一次才真的提交扣费；用户选放弃就到此为止。\n（客户端支持 elicitation 时服务端会直接弹窗问用户，此时省去 3-4 步，一次调用即可。）\nvoice_role 与 subtitle_type 决定这次翻译到底做什么：两者都关（voice_role 传 No 且 subtitle_type=0）等于既不配音也不嵌字幕，产出的视频和原片没有区别，但一样扣费——上游不拦这个组合，请在提交前自行拦下并问用户。返回 data.videoTranslateOrderNo 是后续所有查询用的订单号。同一份文件、同一目标语言 30 分钟内再次提交会被直接拒绝，除非带上 retry_of_order_no（上一单单号）和 retry_confirmed=true——任务失败后不要自己改个参数就重提，先把失败原因告诉用户、问过再说。限制：免费用户单个视频最长 10 分钟、每月累计 10 分钟、单文件 200MB、同时只能有 1 个进行中的任务（Pro 为 60 分钟/1024MB/2 个）；这些是默认档位的值，账号实际的限额用 get_account_status 查，本工具提交前也会拿真实限额比一次，超了会直接拒绝（不提交、不扣费），到那时再重传剪短的文件就白传了一次。若 target_language 传 ar（阿拉伯语）且账号不是付费会员，上游要求人机验证 token，外部调用无法提供，会直接失败。",
        inputSchema={
            "type": "object",
            "properties": {
                "source_language": {
                    "type": "string",
                    "description": "源语言代码"
                },
                "target_language": {
                    "type": "string",
                    "description": "目标语言代码"
                },
                "source_file_object_key": {
                    "type": "string",
                    "description": "视频文件对象 key"
                },
                "video_file_name": {
                    "type": "string",
                    "description": "视频文件名"
                },
                "voice_role": {
                    "type": "string",
                    "description": "配音开关，只有两个取值：No=不配音、只做字幕翻译（默认，与产品前端一致）；clone=克隆原说话人音色做配音。clone 且 subtitle_type≠0 时**额度翻倍**——10 分钟视频 80 额度会变成 160，所以要不要配音必须先问用户，不要替他决定。服务端对本字段只校验非空，填其他值不会报错但会白扣额度，不要传这两个之外的值。"
                },
                "subtitle_type": {
                    "type": "integer",
                    "description": "0=不嵌入字幕, 1=翻译字幕(默认), 2=原始字幕, 3=翻译+原始字幕"
                },
                "user_confirmed": {
                    "type": "boolean",
                    "description": "用户已经看到试算额度、并明确选择了是否开启同声翻译（配音）后才传 true。为空或 false 时直接拒绝提交。注意它只是入场券：填了 true 也不会直接提交，还要走 confirm_token 这一步（或服务端弹窗）拿到用户本人的确认。"
                },
                "video_task_param": {
                    "type": "object",
                    "description": "完整的生成参数，覆盖 voice_role / subtitle_type 这两个快捷参数。配音：voiceRate 语速、volume 音量、pitch 音调（均为 +0% / +0Hz 这类字符串）、voiceAutorate 语音自动变速、videoAutorate 视频自动变速（默认都 true）。字幕样式：fontsize 字号(默认14)、fontname 字体、fontcolor 颜色(#RRGGBB)、fontbold 加粗、subtitlePosX 水平位置 5-95(50居中)、subtitlePosY 底边距 0-90、fontbordercolor 描边色、outline 描边宽 0-10(0关闭)、shadow 阴影 0-10(0关闭)、backgroundcolor 背景框色、borderStyle 1普通描边/3逐行矩形背景框。不传的字段走服务端默认值。注意服务端对这些字段一个都不校验，填了非法值不会报错、会照常扣费然后在生成阶段失败，不确定就别传。"
                },
                "confirm_token": {
                    "type": "string",
                    "description": "确认码，来自本工具上一次调用返回的 data.options 里用户挑中的那一项。只有在把 data.userPrompt 原样给用户看过、用户明确选了某一项之后才带上它——带着它这一次就会真的扣费。首次调用不要传，也不要自己编一个。确认码自己记着它代表菜单里的哪一格：带上它时，配音和字幕按那一格定，你传的 voice_role / subtitle_type 会被忽略，所以不用去对，也别想用它换一格。一次性、15 分钟有效。"
                },
                "retry_of_order_no": {
                    "type": "string",
                    "description": "重做时必填：上一单的 videoTranslateOrderNo。同一份文件、同一目标语言 30 分钟内再次提交会被拒绝，除非带上这个单号并把 retry_confirmed 置 true。"
                },
                "retry_confirmed": {
                    "type": "boolean",
                    "description": "重做时必填 true：表示已经把上一单的结果或失败原因告诉用户、并得到用户明确同意再扣一次费。不要自己填。"
                }
            },
            "required": [
                "source_language", "target_language", "source_file_object_key",
                "video_file_name", "voice_role", "user_confirmed"
            ]
        }
    ),
    Tool(
        name="calculate_video_translation_quota",
        description="试算视频翻译要消耗多少额度，不扣费。提交 translate_video 前应当先调这个并把结果告诉用户。video_duration 必须是从文件里真实读出来的时长（ffprobe 等），不能按文件大小猜——服务端核实不了这个输入，猜错就等于给用户报了个假预算。计费规则：按 30 秒为一个计费单位向上取整，每单位 4 额度；voice_role 为 clone 且 subtitle_type≠0 时额度翻倍（实测 10 分钟视频：不配音 80 额度，开克隆配音 160 额度）。返回里如果带 limitWarning（视频超出会员档的时长上限）或 quotaWarning（余额不够），请先把那句话原样告诉用户再往下走——这两种情况提交上去会被服务端直接拒，白传一次文件。",
        inputSchema={
            "type": "object",
            "properties": {
                "video_duration": {
                    "type": "number",
                    "description": "视频时长，单位是**毫秒**（不是秒）。例如 2 分 5 秒要传 125000，传成秒会让试算额度远低于实际扣费。必须是文件的真实时长：用 ffprobe（ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 <文件>，得到的是秒，乘 1000）或其他媒体信息工具读出来。严禁按文件大小估算——码率差异极大，实测有把 21 秒的 4.8MB 视频猜成 5 分钟的，报给用户的预算因此差了十倍。读不到真实时长就别试算，先告诉用户你读不到。"
                },
                "voice_role": {
                    "type": "string",
                    "description": "配音开关，只有两个取值：No=不配音、只做字幕翻译（默认，与产品前端一致）；clone=克隆原说话人音色做配音。clone 且 subtitle_type≠0 时**额度翻倍**——10 分钟视频 80 额度会变成 160，所以要不要配音必须先问用户，不要替他决定。服务端对本字段只校验非空，填其他值不会报错但会白扣额度，不要传这两个之外的值。"
                },
                "subtitle_type": {
                    "type": "integer",
                    "description": "0=不嵌入字幕, 1=翻译字幕(默认), 2=原始字幕, 3=翻译+原始字幕"
                }
            },
            "required": ["video_duration"]
        }
    ),
    Tool(
        name="get_video_translation_status",
        description="查询单个视频翻译任务的状态。上游是 SSE 流，本工具取第一帧数据就返回，不会挂住。状态：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消——注意 2 就是完成，和文档翻译的状态码不是一套，别混用。进行中时 step 表示阶段（1 语音识别 / 2 字幕翻译 / 3 语音生成）。任务通常要几分钟，建议 10-30 秒查一次，并把进度转述给用户。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译任务订单号"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="wait_for_video_translation",
        description="等待视频翻译任务完成。提交 translate_video 后就用它跟进，不要自己反复调 get_video_translation_status。上游只能轮询、无法推送，本工具有两个返回时机：进度一有变化就立刻返回，否则等满本轮的等待时长（不传 timeout 时由本工具自适应：10 秒起，进度一直不动就逐轮翻倍到 45 秒封顶，省掉那些什么都说不出来的空转往返）。视频任务通常要几分钟。返回里 msg 是念给用户听的那一行、agentNote 是给你的操作指令：要转述就转述 msg，agentNote 一个字都不要念出去。finished=false 表示仍在处理，照 agentNote 的要求办：进度有变化就把 msg 那行原样告诉用户，和上次完全一样时一个字都不要输出（连「继续等待」这类过场话也不要），直接再次调用本工具继续等待，任务不会因此中断。完成后返回里带 translatedVideoUrl / targetSubtitlesUrl，要把它们原样完整交给用户——问号后面的签名参数一字都不能改；有效期以返回的 expiresAt / downloadNote 为准，不要按经验说成一小时。描述产物时请原样照抄 msg 或 outputNote 里那句产出说明（例如「未配音（保留原声），已嵌入译文字幕」），不要凭之前传过的参数自己推断有没有配音。该任务做过字幕改写的话，返回里给的就是改写后那一版（带 rewriteOrderNo），outputNote 会注明，别再回头用改写前的链接。任务失败或被取消时返回 code=500 且 data.failed=true，reason 是原因——请先告诉用户，问过之后再决定是否重新提交，重提会再次扣费。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译订单号，即 translate_video 返回的 videoTranslateOrderNo"
                },
                "timeout": {
                    "type": "integer",
                    "description": "本次最多等待的秒数。**正常情况不要传**：不传时本工具自己掌握节奏——起步 10 秒，好让第一次进度尽快回到用户面前，之后进度每连着一轮没变就把等待翻一倍（10→20→40，上限 45 秒），进度一变又回到 10 秒。这么做是因为进度不动的那些轮次你什么都不该输出，可你每回来一次都是一整轮往返、上下文重发一遍，界面上还多一行空回合。传了本参数就按你给的秒数严格执行、不再自适应；传大值不会多打上游（轮询间隔是内部定的，与本参数无关），但超过你那端的工具调用超时会让本次调用直接报错——那是客户端超时，不是任务失败，任务还在跑，重新调本工具接着等即可。到点未完成会返回当前进度而非报错，可再次调用继续等待。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="list_video_translations",
        description="分页查询视频翻译任务列表，只返回最近 15 天的记录。status 过滤值：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消。列表只用来找单号和看状态，不带下载地址（每条几百字符的签名链接，十条就上万字符，且大多用不上）。要交付某一单的产出，拿它的 videoTranslateOrderNo 调 get_video_translation_status 取链接，那边是现签发的，不必担心列表里的地址过期。",
        inputSchema={
            "type": "object",
            "properties": {
                "page_num": {
                    "type": "integer",
                    "description": "页码"
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页数量"
                },
                "status": {
                    "type": "integer",
                    "description": "状态过滤"
                }
            }
        }
    ),
    Tool(
        name="cancel_video_translation",
        description="取消视频翻译任务。只能取消 status=0（未开始）的任务，已经开始的会返回 31008。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译任务订单号"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="wait_for_translation",
        description="等待翻译任务完成。上游只能轮询、无法推送，本工具有两个返回时机：进度一有变化就立刻返回，否则等满本轮的等待时长（不传 timeout 时由本工具自适应：10 秒起，进度一直不动就逐轮翻倍到 45 秒封顶，省掉那些什么都说不出来的空转往返）。返回 finished=true 时附带 downloadUrl（纯译文，CloudFront）与 downloadUrlCN（同一文件的国内兜底线路），其他版式用 get_document_translation_result 取。这两条链接带签名，转述时必须把问号后面的参数一起原样给全，截断会 403。返回里 msg 是念给用户听的那一行、agentNote 是给你的操作指令：要转述就转述 msg，agentNote 一个字都不要念出去。finished=false 表示仍在处理，照 agentNote 的要求办：进度有变化就把 msg 那行原样告诉用户，和上次完全一样时一个字都不要输出，直接再次调用本工具继续等待，任务不会因此中断。若任务被服务端取消或失败，返回 code=500 且 data.failed=true，reason 是原因（如 BACKEND_CANCEL）——请先把原因告诉用户，问过用户之后再决定是否用相同参数重试 translate_document，文件不需要重新上传。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                },
                "timeout": {
                    "type": "integer",
                    "description": "本次最多等待的秒数。**正常情况不要传**：不传时本工具自己掌握节奏——起步 10 秒，好让第一次进度尽快回到用户面前，之后进度每连着一轮没变就把等待翻一倍（10→20→40，上限 45 秒），进度一变又回到 10 秒。这么做是因为进度不动的那些轮次你什么都不该输出，可你每回来一次都是一整轮往返、上下文重发一遍，界面上还多一行空回合。传了本参数就按你给的秒数严格执行、不再自适应；传大值不会多打上游（轮询间隔是内部定的，与本参数无关），但超过你那端的工具调用超时会让本次调用直接报错——那是客户端超时，不是任务失败，任务还在跑，重新调本工具接着等即可。到点未完成会返回当前进度而非报错，可再次调用继续等待。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="get_video_subtitles",
        description="获取视频的原文与译文字幕下载地址。任务 status 必须是 2（成功），否则返回 31008「文件翻译中」。若该任务已有改写记录，返回的是最近一次改写后的字幕。地址有有效期，以返回里的说明为准。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译任务订单号"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="calculate_rewrite_quota",
        description="试算「用改好的字幕重新生成视频」要花多少额度，不扣费。改写是一单新的翻译任务、按视频时长计价，和第一次翻译同价，不是免费返工。校对字幕之前就可以先问这一句，把数字告诉用户。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "原视频翻译订单号（videoTranslateOrderNo）"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="rewrite_video_subtitles",
        description="用编辑后的字幕重新生成视频。⚠️ 真实扣费，所以和 translate_video 一样是**两步**调用：第一次不带 confirm_token，本工具不提交、不扣费，只回 409 加一段 data.userPrompt（里面有试算出来的真实额度）和一个 data.confirmToken；把 userPrompt 原样发给用户，等他明确同意，再带上那个 confirmToken 重新调用才会真的提交。客户端支持服务端弹窗时，本工具会直接问用户，同意即提交。确认码绑定「订单号 + 这两份字幕正文」，字幕改一行都要重新确认——用户同意的是他看过的那一版。原任务的 status 必须是 2（成功）。返回 data.videoTranslateRewriteOrderNo 是改写订单号，查进度用 get_video_rewrite_status。改写完成后请重新用原视频订单号去取产物（wait_for_video_translation 或 get_video_translation_status），它们会自动给改写后的那一版；改写前拿到的旧链接仍然有效，别再拿它当最终产物给用户。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译任务订单号"
                },
                "source_subtitles_txt": {
                    "type": "string",
                    "description": "源语言字幕文本"
                },
                "target_subtitles_txt": {
                    "type": "string",
                    "description": "目标语言字幕文本"
                },
                "video_task_param": {
                    "type": "object",
                    "description": "可选，完整生成参数，覆盖首次提交时的参数；不传则沿用原任务的参数。字段同 translate_video 的 video_task_param。"
                },
                "confirm_token": {
                    "type": "string",
                    "description": "确认码，来自本工具上一次调用返回的 data.confirmToken。只有把 data.userPrompt 原样给用户看过、用户明确同意之后才带上它——带着它这一次就会真的扣费。首次调用不要传，也不要自己编。一次性、15 分钟有效，且绑定当时那两份字幕正文。"
                }
            },
            "required": ["order_no", "source_subtitles_txt", "target_subtitles_txt"]
        }
    ),
    Tool(
        name="probe_elicitation",
        description="连通性自检工具，不翻译、不提交任务、不扣任何额度。用来验证服务端能否通过 MCP elicitation 直接向用户提问（而不是靠模型自己填 user_confirmed 声称问过了）。调用后会返回客户端声明的能力，并在支持时真的弹一次提问。只在排查这个问题时调用，正常翻译流程不要调。",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="get_video_rewrite_status",
        description="查询字幕改写任务的进度。上游是 SSE 流，本工具取第一帧数据就返回。状态含义同视频翻译：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消。改写成功后回到原视频订单号调 wait_for_video_translation 或 get_video_translation_status 取产物，那边会带上改写版并说明产出。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "改写任务订单号"
                }
            },
            "required": ["order_no"]
        }
    ),
]


# 用户可见的产出串（状态、产出说明、进度行、失败原因、下载说明）按 locale 输出，
# 九种语言见 i18n.py。工具描述和「把这行原样告诉用户」这类操作指令仍是中文——
# 那是写给模型的。只给输出会被念给用户听的工具挂这个参数，其余不必加。
_LOCALE_AWARE_TOOLS = (
    # 提交/改写会把整张确认菜单和弹窗交给用户，是最该说他语言的地方
    "translate_video",
    "rewrite_video_subtitles",
    "calculate_rewrite_quota",
    "wait_for_translation",
    "wait_for_video_translation",
    "get_document_translation_status",
    "get_video_translation_status",
    "get_document_translation_result",
    "list_document_translations",
    "list_video_translations",
)
_LOCALE_PROPERTY = {
    "type": "string",
    "enum": list(i18n.SUPPORTED_LOCALES),
    "description": (
        "把要念给用户听的那部分文案（任务状态、产出说明、进度行、失败原因、下载说明）"
        "输出成哪种语言。按用户当前说话的语言填，不确定就别传——"
        "不传取服务端 MCP_LOCALE，再没有就是 zh。"
    ),
}
for _tool in TOOLS:
    if _tool.name in _LOCALE_AWARE_TOOLS:
        _tool.input_schema.setdefault("properties", {})["locale"] = dict(_LOCALE_PROPERTY)


def build_tool_handlers(client: TranslationClient):
    """按传入的 client 构建工具处理器映射"""
    handlers = {
        "get_supported_languages": lambda args: client.get_language_enum(args.get("display_locale", "zh")),
        "get_model_list": lambda args: client.get_model_list(),
        "upload_document": lambda args: _upload_presigned(client, args["file_name_list"], "document"),

        "translate_document": lambda args: client.batch_submit_translate_task(
            args["file_list"],
            args["source_language"],
            args["target_language"],
            args.get("model", "Gemini-2.5-Flash"),
            # 不填默认值：None 表示「调用方没主张」，交给服务端检测
            args.get("is_ocr"),
            args.get("terminology_collection_id"),
        ),
        "get_account_status": lambda args: _account_status(client, args.get("refresh", False)),
        "check_pdf_ocr": lambda args: _check_pdf_ocr(
            client, args["file_object_key"], args.get("file_name", "")
        ),
        "get_document_translation_status": lambda args: _doc_status(client, args["order_no"]),
        "get_document_translation_result": lambda args: client.get_translate_s3_download_url(
            args["order_no"],
            args.get("url_type", 2),
            args.get("is_watermark", 0)
        ),
        "list_document_translations": lambda args: _list_documents(
            client,
            args.get("page_num", 1),
            args.get("page_size", 10),
            args.get("status"),
        ),
        "get_document_translation_by_batch": lambda args: _batch_documents(client, args["batch_no"]),
        "upload_video": lambda args: _upload_presigned(client, args["file_name_list"], "video"),
        "translate_video": lambda args: _submit_video_translate(client, args),
        "calculate_video_translation_quota": lambda args: _video_quota(
            client,
            args["video_duration"],
            args.get("voice_role", "No"),
            args.get("subtitle_type", 1),
        ),
        "get_video_translation_status": lambda args: _video_status(client, args["order_no"]),
        "wait_for_video_translation": lambda args: client.wait_for_video_translation(
            args["order_no"], args.get("timeout"), on_progress=_progress_reporter()
        ),
        "list_video_translations": lambda args: client.search_video_translate_page(
            args.get("page_num", 1),
            args.get("page_size", 10),
            args.get("status")
        ),
        "cancel_video_translation": lambda args: client.cancel_video_translate(args["order_no"]),

        "get_video_subtitles": lambda args: client.get_video_subtitles(args["order_no"]),
        "calculate_rewrite_quota": lambda args: _rewrite_quota(client, args["order_no"]),
        "rewrite_video_subtitles": lambda args: _submit_video_rewrite(client, args),
        "get_video_rewrite_status": lambda args: client.get_video_rewrite_detail(args["order_no"]),
        "probe_elicitation": _probe_elicitation,
        "wait_for_translation": lambda args: client.wait_for_translation(
            args["order_no"], args.get("timeout"), on_progress=_progress_reporter()
        ),
    }

    def with_locale(name, fn):
        """每次调用先按 args.locale 定好语言，再进真正的处理器。
        contextvar 在 asyncio 里每个 Task 一份，HTTP 模式并发也不会串。"""
        async def run(args):
            token = i18n.use_locale(args.get("locale"))
            try:
                # 文档单号 / 视频单号走错工具，上游只回 403，在这儿就拦下来
                mismatch = _order_family_error(name, args)
                if mismatch is not None:
                    return mismatch
                # 密钥层面的失败每个接口都可能回，统一在出口翻成可执行的一句话，
                # 免得二十个处理器各自漏一遍
                return annotate_key_error(await fn(args))
            finally:
                i18n.reset_locale(token)
        return run

    return {name: with_locale(name, fn) for name, fn in handlers.items()}


# 工具名的唯一真相是处理器表本身，别再手抄一份。client 只在 lambda 体里用到，
# 传 None 建一张表纯粹为了取键名。
TOOL_NAMES = frozenset(build_tool_handlers(None))


def register_tools(server: Server, client: TranslationClient):
    """注册所有 MCP 工具（stdio 模式：全程就一条上游连接）"""

    @asynccontextmanager
    async def borrow(ctx):
        yield client

    register_tools_per_request(server, borrow)


def register_tools_per_request(server: Server, borrow):
    """注册所有 MCP 工具，上游连接每次调用现借。

    HTTP 模式下 API Key 是每个请求自己带的（服务器不存密钥），所以 client 不能在
    注册时定死。borrow(ctx) 是个异步上下文管理器：进去拿到本次该用的 client，
    出来把它还回池子。
    """

    async def handle_list_tools(ctx, params: PaginatedRequestParams) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        tool_name = params.name
        args = params.arguments or {}

        if tool_name not in TOOL_NAMES:
            return CallToolResult(
                content=[TextContent(type="text", text=f"未知工具: {tool_name}")]
            )

        # elicitation 和进度通知都要从 ctx 拿回传通道，进处理器前先放好
        token = _REQUEST_CTX.set(ctx)
        try:
            async with borrow(ctx) as client:
                result = await build_tool_handlers(client)[tool_name](args)
            return CallToolResult(content=_blocks(result), isError=False)
        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {str(e)}")],
                isError=True
            )
        finally:
            _REQUEST_CTX.reset(token)

    # add_request_handler 要的是 params 模型（RequestParams 的子类），
    # 不是 ListToolsRequest / CallToolRequest 这种 Request 模型。
    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)

    # stdio 模式下 stdout 是 JSON-RPC 通道，任何多余输出都会污染协议流
    print(f"已注册 {len(TOOLS)} 个工具", file=sys.stderr, flush=True)
