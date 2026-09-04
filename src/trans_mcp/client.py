"""API 客户端"""

import asyncio
import calendar
import json
import math
import os
import re
import hashlib
import secrets
import sys
import time
import httpx
from typing import Optional

# 用户可见的产出串走消息表（九种语言），给模型看的指令仍是中文
from .i18n import t

# 上游 API 地址。默认测试环境；切生产（https://belindoc.com/api）只需要设
# BELINDOC_API_BASE_URL，不用再改这一行——部署包是 tar 解出来的，改源码等于
# 每次升级都要重新改一遍。
DEFAULT_API_BASE_URL = "http://internal-test-host:6101"
API_BASE_URL = os.environ.get("BELINDOC_API_BASE_URL", "").strip().rstrip("/") or DEFAULT_API_BASE_URL
DOC_PREFIX = "/external/translate"
# 上游瞬时错误码，重试即可（见 docs/document-translation-api-guide.md 常见错误）：
#   600   System is busy
#   30010 并发任务超过限制，等在跑的任务完成后重试
#   30012 任务重复提交，稍后重试
#   30311 调用过于频繁，退避后重试
TRANSIENT_CODES = {"600", "30010", "30012", "30311"}
# 密钥层面的失败同样是 HTTP 200，业务码在响应体里（文档「错误码」一节）。原来
# 这几个码一路原样透传，调用方看到的只是一句上游的 msg，于是把「密钥过期」当成
# 网络故障反复重发、或者改个参数再试一遍——这几条重试一万次也不会变。
# 分支一律认 code：msg 会随 language 头变语种，拿它做判断迟早误判。
KEY_ERROR_CODES = {
    "30306": "error.key.30306",   # 无效的 API 密钥
    "30307": "error.key.30307",   # 密钥已被禁用
    "30308": "error.key.30308",   # 密钥已过期
    "30309": "error.key.30309",   # 调用 IP 不在白名单内
    "30311": "error.key.30311",   # 调用过于频繁
    "30312": "error.key.30312",   # 密钥被管理员封禁
}
# 这一条是限流，退避后重试有意义；其余五条重试没有意义
KEY_ERROR_RETRYABLE = {"30311"}

# 业务错误码（文档两张表的并集）。分档的意义在于「下一步该干什么」完全不同：
# 充额度 / 换文件 / 重新上传 / 退避重试 / 等任务跑完 / 核对单号。原来这些一路
# 原样透传，调用方拿到的只是一句上游 msg，最常见的收场是改个参数再提一次——
# 而额度不足、格式不支持这些，提一万次也是同样的结果，每次还要重走一遍上传。
_CODE_ACTIONS = {
    "user": "——这要用户去处理，重试、换参数都不会变。请把上面这句原样告诉用户，"
            "问过他之后再决定下一步，不要自己重提。",
    "file": "——换文件之前重提没有意义。请把上面这句原样告诉用户，由他决定换一个文件"
            "还是调整这一个，不要自己改参数重试。",
    "reupload": "——重试提交没有用：必须重新调 upload_document / upload_video 取预签名地址、"
                "把文件重新传上去，再用新的 objectKey 提交。",
    "retry": "——这是瞬时故障，退避几秒后用相同参数重试即可，文件不用重新上传。",
    "wait": "——这不是错误，是任务还没到终态。请先用状态查询工具确认，等完成之后再来取。",
    "check": "——请核对订单号是不是当前账号的，重试同一个单号不会有别的结果。",
    "task": "——重新提交是一单新任务、会再扣一次费。请先把这句如实告诉用户，"
            "问过之后再决定要不要重做，不要自己直接重提。",
}
BUSINESS_ERROR_CODES = {
    "30002": "file",      # 不支持的文件类型
    "30003": "file",      # 视频文件类型不支持
    "30006": "user",      # 翻译额度不足
    "30013": "user",      # OCR 额度不足
    "31001": "file",      # 视频文件为空
    "31002": "file",      # 视频文件大小超出限制
    "31004": "reupload",  # 视频文件上传失败（这个 key 底下没文件）
    "30014": "reupload",  # 上传文件已失效（文档侧）
    "31005": "reupload",  # 视频文件已失效
    "31006": "task",      # 语音识别失败
    "31007": "retry",     # 任务提交失败
    "31008": "wait",      # 文件翻译中
    "31009": "file",      # 视频时长超出限制
    "31010": "user",      # 免费用户每月 10 分钟
    "403": "check",       # 无访问权限
}
# 需要重新上传、重试提交没用的错误码
REUPLOAD_CODES = {"30014": "上传的文件已失效，需要重新获取预签名地址并上传"}
# 任务状态，见文档 5.2。注意 1=解析中：漏了它会把正常的中间态当成未知状态。
TASK_STATUS_TEXT = {
    0: "未开始",
    1: "解析中",
    2: "翻译中",
    3: "翻译完成",
    4: "翻译失败",
    5: "已取消",
}
TASK_STATUS_PENDING = (0, 1, 2)
TASK_STATUS_DONE = 3
TASK_STATUS_FAILED = {4: "翻译失败", 5: "已取消"}
# 下载版式。实测各文件类型的支持情况：
#   PDF   1 2 3 4        EPUB  1 2 4（流式排版无法左右并排）
#   DOCX  1 2
# 上游对不支持的组合只回一句 "Failed to generate file."，这里补上可读的解释。
URL_TYPE_LABELS = {
    1: "原文",
    2: "纯译文",
    3: "横向对照（左右并排）",
    4: "纵向对照（原文与译文上下排列）",
}
URL_TYPE_SUPPORT = {
    3: "仅 PDF",
    4: "仅 PDF 与 EPUB",
}
# 各版式在 getTranslateFileDetail 详情里对应的字段。任务完成后这些地址就已经
# 在详情里了，不必再为了拿链接单独调 getTranslateS3DownloadUrl。
# 注意只有纯译文有 2 号兜底地址，对照版式没有——别对它们承诺国内线路。
# 实测上游详情里对照版的字段是 xcomparisonS3Url / ycomparisonS3Url（小写 c，
# 对应的 objectKey 字段还拼成了 ObjectKye）。驼峰那版一次都匹配不上，所以两种
# 写法都认——认漏了就等于「详情里已经有地址」这条线索白丢。
URL_TYPE_FIELDS = {
    2: ("targetFileUrl",),  # 国内兜底线路 targetFileUrl2 单独取，别混进版式表
    3: ("xcomparisonS3Url", "xComparisonS3Url"),
    4: ("ycomparisonS3Url", "yComparisonS3Url"),
}
VIDEO_PREFIX = "/external/videoTranslate"
# 账户信息（钱包额度 / 订阅权益）。上游 2026-09-02 才开放到 /external，
# 在此之前 MCP 里那个 getQuota 是个从来没存在过的接口。
USER_PREFIX = "/external/user"
# 视频任务状态。和文档翻译完全不是一套：这里 2 是「成功」，而文档翻译里
# 2 是「翻译中」、3 才是完成。照搬会把成功读成进行中。
VIDEO_STATUS_TEXT = {
    0: "未开始",
    1: "进行中",
    2: "成功",
    3: "失败",
    4: "已取消",
}
VIDEO_STATUS_DONE = 2
VIDEO_STATUS_TERMINAL = (2, 3, 4)
VIDEO_STEP_TEXT = {1: "语音识别", 2: "字幕翻译", 3: "语音生成"}
VIDEO_STEP_STATUS_TEXT = {0: "未开始", 1: "进行中", 2: "完成", 3: "失败"}

# 产出到底做了什么，只有 paramJson 说了算。模型隔了十几轮再回忆自己传过什么，
# 很容易把「不配音+嵌字幕」说成「英文配音」，所以这里直接还原成一句人话。
VIDEO_SUBTITLE_TEXT = {
    0: "未嵌入字幕",
    1: "已嵌入译文字幕",
    2: "已嵌入原文字幕",
    3: "已嵌入译文+原文字幕",
}
VIDEO_SUBTITLE_TYPE = {
    0: "不嵌入字幕",
    1: "翻译字幕",
    2: "原始字幕",
    3: "翻译+原始字幕",
}
# 视频时长按 30 秒一个计费单位向上取整，每单位 4 额度
VIDEO_QUOTA_UNIT_MS = 30_000
VIDEO_QUOTA_PER_UNIT = 4

# 配音 × 字幕 的可选组合。这两项决定这次翻译到底做出什么东西，也决定扣多少，
# 所以要摆成一张表让用户挑，而不是模型自己定一组、只问「提交还是放弃」。
# ("No", 0) 不列：既不配音也不嵌字幕，产出和原片没区别，但一样扣费。
VIDEO_COMBOS = (
    ("No", 1),
    ("No", 3),
    ("No", 2),
    ("clone", 1),
    ("clone", 3),
    ("clone", 2),
    ("clone", 0),
)
VIDEO_COMBO_LABEL = {
    ("No", 1): "原声 + 译文字幕",
    ("No", 3): "原声 + 双语字幕",
    ("No", 2): "原声 + 原文字幕",
    ("clone", 1): "克隆配音 + 译文字幕",
    ("clone", 3): "克隆配音 + 双语字幕",
    ("clone", 2): "克隆配音 + 原文字幕",
    ("clone", 0): "克隆配音 + 不嵌字幕（只换声音）",
}


def quota_by_rule(duration_ms: float, voice_role: str, subtitle_type: int) -> int:
    """按文档写死的计费规则推一个额度。只在上游试算问不到时兜底，用了要标出来。"""
    units = max(1, math.ceil((duration_ms or 0) / VIDEO_QUOTA_UNIT_MS))
    quota = units * VIDEO_QUOTA_PER_UNIT
    if voice_role == "clone" and subtitle_type != 0:
        quota *= 2
    return quota


# videoTaskParam 的字段。后端把整个对象序列化存库，再原样反序列化成
# PyVideoGenerateRequest 透传给下游 Python，服务端对内部字段一个都不校验，
# 所以注释里的取值范围是给下游的契约，传错不会报错、只会白扣额度。
VIDEO_TASK_PARAM_KEYS = {
    # 配音
    "voiceRole", "voiceRate", "volume", "pitch", "voiceAutorate", "videoAutorate", "ttsType",
    # 字幕
    "subtitleType", "fontsize", "fontname", "fontcolor", "fontbold",
    "subtitlePosX", "subtitlePosY", "fontbordercolor", "backgroundcolor",
    "outline", "shadow", "borderStyle",
    # 未公开但会透传的
    "appendVideo", "isSeparate", "onlyVideo", "recognType", "modelName",
    "translateType", "splitType", "isCuda",
}


def _build_video_task_param(voice_role: str, subtitle_type: int, extra: Optional[dict]) -> dict:
    """合并快捷参数与完整的 videoTaskParam，显式传入的 extra 优先"""
    param = {"voiceRole": voice_role, "subtitleType": subtitle_type}
    for key, value in (extra or {}).items():
        if value is not None:
            param[key] = value
    return param


def _check_video_task(param: dict) -> Optional[str]:
    """拦掉「既不配音也不嵌字幕」——产出的视频和原片没区别，但一样扣费。
    产品前端明确拒绝这个组合（free-pdf-translate/src/store/video.ts:684），
    而 /external 后端只校验非空，不拦。"""
    if str(param.get("voiceRole", "")).lower() == "no" and int(param.get("subtitleType", 1) or 0) == 0:
        return (
            "voiceRole=No 且 subtitleType=0 等于既不配音也不嵌字幕，"
            "产出的视频和原片没有区别，但一样扣额度。请先问用户到底要配音还是要字幕，"
            "再重新提交：要字幕就把 subtitle_type 设为 1（翻译字幕），要配音就把 voice_role 设为 clone。"
        )
    return None


def annotate_error(result):
    """把上游的业务码翻成「这是什么 + 下一步干什么」

    含义按 locale 给用户看（要去充额度、换文件、改控制台的是他），后面那句
    「别重试 / 该重传」是给模型的，留中文。
    分支一律认 code：msg 会随 language 头变语种，拿它做判断迟早误判。
    """
    if not isinstance(result, dict):
        return result
    code = str(result.get("code") or "")

    if code in KEY_ERROR_CODES:
        retryable = code in KEY_ERROR_RETRYABLE
        result["msg"] = t(KEY_ERROR_CODES[code]) + (
            "——这是限流，不是参数错。退避几秒再调一次即可，别改参数、别重新上传文件。"
            if retryable else
            "——这是密钥本身的问题：重试、换参数、重新上传都没有用。请把上面这句话"
            "原样告诉用户（这是他去控制台能处理的事），然后停下来，不要再调别的工具。"
        )
        result["keyError"] = True
        result["retryable"] = retryable
        return result

    if code in BUSINESS_ERROR_CODES:
        action = BUSINESS_ERROR_CODES[code]
        result["msg"] = t(f"error.biz.{code}") + _CODE_ACTIONS[action]
        result["errorAction"] = action
        result["retryable"] = action == "retry"
    return result


# 老名字：早先只处理密钥那六个码，留个别名免得漏改
annotate_key_error = annotate_error


def _not_found(path: str) -> dict:
    """404 的可读说明。上游对未部署的接口直接回 404，httpx 抛的是一句英文
    HTTPStatusError，调用方会当成网络故障反复重试、或告诉用户「服务器维护中」。"""
    return {
        "code": "404",
        "msg": (
            t("error.biz.404", path=path, base=API_BASE_URL)
            + "——重试没有意义。请把上面这句原样告诉用户，不要反复重试，"
              "也不要改用其他工具凑合。"
        ),
    }


def video_combo_label(voice_role: str, subtitle_type) -> str:
    """确认菜单里那一格的名字。VIDEO_COMBO_LABEL 留作中文原文，对外按 locale 拼——
    这张菜单是用户拿来做决定的，比任何一句进度都更该说他的语言。"""
    voice = t("combo.voice.clone" if voice_role == "clone" else "combo.voice.no")
    key = f"combo.subtitle.{subtitle_type}"
    subtitle = t(key)
    if subtitle == key:
        return f"{voice_role}/{subtitle_type}"
    return t("combo.label", voice=voice, subtitle=subtitle)


def video_status_text(status) -> str:
    """视频任务状态，按调用方要求的语言给。上面那几张 dict 常量留作中文原文
    （内部对照、注释里引用），对外一律走消息表。"""
    key = f"status.video.{status}"
    text = t(key)
    return t("status.unknown", value=status) if text == key else text


def video_step_text(step) -> str:
    key = f"status.step.{step}"
    text = t(key)
    return t("status.unknown", value=step) if text == key else text


def video_step_status_text(value) -> str:
    # 步骤状态只有「完成」这一档和任务状态的说法不同，其余复用
    return t("status.step.done") if value == 2 else video_status_text(value)


def doc_status_text(status) -> str:
    key = f"status.doc.{status}"
    text = t(key)
    return t("status.unknown", value=status) if text == key else text


def _annotate_video_status(record: dict) -> dict:
    """给视频记录补上状态中文说明，别让调用方拿文档翻译那套状态码去套"""
    if not isinstance(record, dict):
        return record
    if "status" in record:
        record["statusText"] = video_status_text(record["status"])
    if record.get("step") is not None:
        record["stepText"] = video_step_text(record["step"])
    if record.get("stepStatus") is not None:
        record["stepStatusText"] = video_step_status_text(record["stepStatus"])
    # videoDuration 是毫秒（文档 §3.4），字段名里看不出来。原样透出去，模型就当秒
    # 念给用户听——实测 21134 毫秒的片子被报成「21134 秒，约 5.87 小时」。改成带
    # 单位的字段名，另外补一行人话时长，让它照抄而不是自己换算。
    if "videoDuration" in record:
        duration_ms = record.pop("videoDuration")
        record["videoDurationMs"] = duration_ms
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            record["videoDurationText"] = _format_duration(duration_ms / 1000)
    note = _output_note(record)
    rewrite = record.get("videoTranslateRewrite")
    if isinstance(rewrite, dict) and rewrite.get("status") == VIDEO_STATUS_DONE:
        # 改写有自己的一份 paramJson（字幕样式可能被改过），有就用它的
        note = _output_note(rewrite) or note
        note = (note + t("video.note_join") if note else "") + t("video.rewritten")
    if note:
        record["outputNote"] = note
    return record


def _output_note(record: dict) -> str:
    """从 paramJson 还原本次产出：有没有配音、嵌了什么字幕"""
    try:
        param = json.loads(record.get("paramJson") or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(param, dict):
        return ""
    voice = param.get("voiceRole")
    if voice is None:
        return ""
    voice_text = t("video.voice.none") if voice == "No" else t("video.voice.clone", voice=voice)
    subtitle_key = f"video.subtitle.{param.get('subtitleType')}"
    subtitle_text = t(subtitle_key)
    if subtitle_text == subtitle_key:
        subtitle_text = t("video.subtitle.unknown")
    return t("video.output_note", voice=voice_text, subtitle=subtitle_text)


# 详情 / 列表 / 提交回执这类只读接口原样透传上游记录：一条二十多个字段，十几个
# 是 null，末尾还挂着几条几百字符的签名地址，pageSize=10 的列表一次上万字符。
# 真正要看的就下面这些，其余是噪声。地址一律不带：要交付走
# get_video_translation_status，它现查现签，比列表里那条随时会过期的旧地址靠谱。
_VIDEO_RECORD_KEEP = (
    "videoTranslateOrderNo",
    "videoFileName",
    "status",
    "statusText",
    "step",
    "stepText",
    "stepStatusText",
    "outputNote",
    "sourceLanguage",
    "targetLanguage",
    "videoDurationMs",
    "videoDurationText",
    "freeTranslateQuota",
    "walletTranslateQuota",
    "progress",
    "errorMessage",
    "createTime",
    "startTime",
    "endTime",
)


def slim_video_record(record, urls: tuple = ()) -> dict:
    """按白名单裁掉视频记录里的 id、objectKey、paramJson 和签名地址

    paramJson 里唯一有用的信息（有没有配音、嵌了什么字幕）已经由
    _annotate_video_status 提炼成 outputNote，原文没必要再占一份。
    """
    if not isinstance(record, dict):
        return record
    # 顺手滤掉 null：刚提交的任务有一半字段是空的，留着只是占地方
    slim = {k: record[k] for k in _VIDEO_RECORD_KEEP if record.get(k) is not None}
    rewrite = record.get("videoTranslateRewrite")
    if isinstance(rewrite, dict) and rewrite.get("videoTranslateRewriteOrderNo"):
        # 只留摘要：有没有改写、改写成没成功。地址由 video_products 统一挑，
        # 不在这里再甩一遍几百字符的签名链接。
        slim["rewrite"] = {
            "orderNo": rewrite.get("videoTranslateRewriteOrderNo"),
            "status": rewrite.get("status"),
            "statusText": video_status_text(rewrite.get("status")),
        }
    for key in urls:
        if record.get(key):
            slim[key] = record[key]
    return slim


def strip_long_urls(record: dict) -> tuple:
    """摘掉记录里的长签名地址，只留字段名。文档侧记录的字段名没有视频那边稳定，
    不敢上白名单，就只砍最占地方的那部分——一条对照版式地址就是四五百字符，
    而它们几乎总是转手就废：真要下载得走 get_document_translation_result，
    那里才有水印控制，直接用详情里的地址反而会拿到带水印的版本。"""
    if not isinstance(record, dict):
        return record, []
    kept, dropped = {}, []
    for key, value in record.items():
        if isinstance(value, str) and len(value) > 120 and "://" in value:
            dropped.append(key)
            continue
        kept[key] = value
    return kept, dropped


def video_products(data: dict) -> tuple:
    """任务当前的产物地址，以及它是不是字幕改写后的那一版

    做过字幕改写并且改写成功之后，新视频和新字幕在 videoTranslateRewrite 这个
    子记录里，父记录的 targetFileObjectKey 不会更新——服务端
    VideoTranslateRewriteServiceImpl 另存了一份 entity。只读顶层就会把改写前那份
    当成最终产物交出去：用户刚花额度把字幕校对完、重新生成，拿到的还是旧的，
    而且从返回里看不出来。
    """
    urls = {
        field: data.get(field)
        for field in (
            "targetFileUrl", "targetSubtitlesUrl",
            "sourceSubtitlesUrl", "sourceFileUrl",
        )
    }
    rewrite = data.get("videoTranslateRewrite")
    if not isinstance(rewrite, dict) or rewrite.get("status") != VIDEO_STATUS_DONE:
        return urls, {}
    for field in ("targetFileUrl", "targetSubtitlesUrl"):
        if rewrite.get(field):
            urls[field] = rewrite[field]
    return urls, {
        "rewritten": True,
        "rewriteOrderNo": rewrite.get("videoTranslateRewriteOrderNo"),
    }


# 双语对照版式的支持范围。判据是**文件类型**，不是详情里有没有那条地址——对照版
# 是按需生成的，详情里没地址不代表这个文件取不到。网页端 free-pdf-translate 的
# 五处 UI（TaskActions / TransCard / TransHistoryTable / PreviewContent /
# TranslateClient）都是同一套判定：横向仅 PDF，纵向 PDF 与 EPUB。
COMPARISON_BY_TYPE = {"PDF": (3, 4), "EPUB": (4,)}


# 认得出的文件类型。上游这个字段不保证是扩展名（见过给编号、给中文说法的），
# 认不出就别硬用——那会让一份 PDF 被当成「没有对照版式的类型」，
# 用户在交付现场压根不知道还能要左右/上下对照。
KNOWN_DOC_TYPES = {
    "PDF", "EPUB", "DOCX", "DOC", "PPTX", "PPT", "XLSX", "XLS", "TXT",
    "PNG", "JPG", "JPEG", "IMAGE",
}


def doc_file_type(data: dict) -> str:
    """这份文件是什么类型：认得的 fileType 优先，认不出就按文件名后缀兜底"""
    file_type = str(data.get("fileType") or "").strip().upper()
    if file_type in KNOWN_DOC_TYPES:
        return file_type
    name = data.get("sourceFileName") or ""
    suffix = name.rsplit(".", 1)[-1].upper() if "." in name else ""
    return suffix or file_type


def comparison_variants(data: dict) -> tuple:
    """这份文件能取哪几种对照版式（url_type）"""
    return COMPARISON_BY_TYPE.get(doc_file_type(data), ())


def offered_variants(data: dict) -> tuple:
    """实际报得出的对照版式：详情里已经有地址的 ∪ 这个类型本来就支持的

    只按类型猜，遇上认不出的 fileType 就会漏报；只看详情里的地址，任务刚完成
    时对照版可能还没生成。两边取并集，哪一边知道都算数。
    """
    found = set(_available_variants(data)) | set(comparison_variants(data))
    return tuple(sorted(found - {2}))


def comparison_offer(data: dict, variants=None) -> str:
    """完成时主动报出还能要哪种对照版

    网页端把这两项明摆在下载菜单里，接口这边不说，用户就永远不知道有这功能。
    """
    variants = comparison_variants(data) if variants is None else tuple(variants)
    if not variants:
        return ""
    return t("download.doc.layouts.pdf" if 3 in variants else "download.doc.layouts.epub")


def _available_variants(data: dict) -> dict:
    """详情里实际存在的版式 -> 地址。比按文件类型猜准，因为这是上游真给了的。"""
    found = {}
    for url_type, fields in URL_TYPE_FIELDS.items():
        for field in fields:
            if field and data.get(field):
                found[url_type] = data[field]
                break
    return found

# 下载链接是带签名的：CloudFront 靠 Signature/Key-Pair-Id，国内线路靠 sign，
# 全挂在问号后面。实测把 ? 之后截掉会直接 403 MissingKey。转述时顺手把长链接
# 截短是很自然的动作，所以每条返回都得把这句话摆在明面上。
_URL_VERBATIM_NOTE = (
    "链接原样完整给出：问号后的签名参数一个字符都不能删改或缩短，否则 403 MissingKey。"
)


def _url_expiry(url: str):
    """从签名链接里解析剩余有效期（秒）。CloudFront 用 Expires、国内线路用
    expires，都是 epoch 秒。解析不出来返回 None。"""
    if not url:
        return None
    match = re.search(r"[?&][Ee]xpires=(\d+)", url)
    if not match:
        return None
    return max(0, int(match.group(1)) - time.time())


def _sigv4_expiry(url: str):
    """S3 预签名 PUT 的剩余有效期（秒）

    SigV4 把过期写成 X-Amz-Date + X-Amz-Expires 两段，和 CloudFront 的
    Expires=<epoch> 不是一套——_url_expiry 的正则也匹配不到它（前面隔着
    X-Amz-）。解析不出来返回 None。
    """
    if not url:
        return None
    issued_at = re.search(r"[?&]X-Amz-Date=(\d{8}T\d{6}Z)", url)
    ttl = re.search(r"[?&]X-Amz-Expires=(\d+)", url)
    if not (issued_at and ttl):
        return None
    try:
        issued = calendar.timegm(time.strptime(issued_at.group(1), "%Y%m%dT%H%M%SZ"))
    except ValueError:
        return None
    return max(0, issued + int(ttl.group(1)) - time.time())


def _upload_expiry_note(url: str) -> str:
    """上传链接什么时候作废，说成绝对时刻

    实测只有 10 分钟。中间去查个时长、排个网络问题就过了，拿旧链接重试只会
    403，而模型看不到期限就会一直重试同一条、并把 403 归给「网络问题」。
    """
    remaining = _sigv4_expiry(url)
    if remaining is None:
        return "上传链接有有效期，过期后重调本工具取新的再传。"
    if remaining <= 0:
        return "这条上传链接已经过期了，重调本工具取新的再传。"
    at = time.localtime(time.time() + remaining)
    fmt = "%H:%M" if at.tm_yday == time.localtime().tm_yday else "%m-%d %H:%M"
    return (
        f"这条上传链接 {time.strftime(fmt, at)}（服务端本地时间）过期，"
        f"还剩 {_format_duration(remaining)}——排查问题耗掉的时间也算在里面；"
        "过了就别拿它重试（只会 403），重调本工具取新链接。"
    )


def _expires_at(url: str) -> str:
    """签名链接的绝对过期时刻。相对时长在对话里会腐坏：模型说完「还有 60 分钟」，
    用户过几分钟才读到，转发给同事时更久，最后照着那个数去下载已经过期了。
    绝对时间点不会腐坏，所以两个都给，让调用方念时间点。"""
    remaining = _url_expiry(url)
    if remaining is None:
        return ""
    at = time.localtime(time.time() + remaining)
    fmt = "%H:%M" if at.tm_yday == time.localtime().tm_yday else "%m-%d %H:%M"
    return time.strftime(fmt, at)


def _expiry_note(url: str) -> str:
    """把真实有效期写出来。只说一句「有有效期」的话，调用方会自己编一个
    「约 1 小时」——实测只有 11 分钟，用户照着那个数去下载就已经过期了。"""
    remaining = _url_expiry(url)
    if remaining is None:
        return "链接有有效期，过期后重调本工具取新的。"
    if remaining <= 0:
        return "链接已过期，请重调本工具取新的。"
    return (
        f"{_expires_at(url)}（服务端本地时间）过期，还剩 {_format_duration(remaining)}"
        "——把这个时间点告诉用户，别按经验说成一小时；过期后重调本工具取新链接。"
    )


def _variant_hint(file_type: str | None) -> str:
    """按文件类型说明可用的版式，避免调用方去试注定失败的取值

    只在拿不到详情、无从确认时用。手上有详情就用 _available_variants，
    那是上游真给了地址的版式，比按类型猜准。
    """
    ft = (file_type or "").upper()
    if ft == "PDF":
        return "url_type=1 原文、3 横向对照（左右并排）、4 纵向对照（原文与译文上下排列）。"
    if ft == "EPUB":
        return "url_type=1 原文、4 纵向对照（原文与译文上下排列）；本类型不支持横向对照。"
    return f"url_type=1 原文；{ft or '该类型'} 不支持对照版式（横向仅 PDF，纵向仅 PDF 与 EPUB）。"


def _upload_progress_log(object_key: str) -> str:
    """后台上传时进度日志的落点。按 objectKey 区分，多个文件同时传不会互相覆盖。"""
    tail = (object_key or "upload").rsplit("/", 1)[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:80] or "upload"
    return f"/tmp/trans-mcp-upload-{safe}.log"


# 认得出视频就能自动挑对预签名端点——视频和文档走的是两套端点、两个存储路径，
# 传错了上游不报错，直到提交翻译时才说 objectKey 不对。
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".3gp",
}


def is_video_file(file_name: str) -> bool:
    return os.path.splitext(file_name or "")[1].lower() in VIDEO_EXTENSIONS


def _next_step_hint(kind: str) -> str:
    if kind == "video":
        return "用 objectKey 作为 source_file_object_key 调 translate_video"
    return "用 objectKey 作为 fileObjectKey 调 translate_document"


# 只签发了预签名链接、本服务没有见过字节落地的 objectKey。实测模型会在拿到链接后
# 直接宣布「文件已上传」然后去提交翻译，上游回 31004「文件上传失败」——错在两步之
# 前，光看 31004 根本读不出来。这里记一笔，好在报错时把话说准。
_ISSUED_KEYS: dict[str, float] = {}
_ISSUED_KEEP = 200


def note_issued_key(object_key: str) -> None:
    if not object_key:
        return
    if len(_ISSUED_KEYS) > _ISSUED_KEEP:
        for k in list(_ISSUED_KEYS)[: len(_ISSUED_KEYS) - _ISSUED_KEEP]:
            _ISSUED_KEYS.pop(k, None)
    _ISSUED_KEYS[object_key] = time.time()


# isOcr 接口要求显式传 storageType（1 私有化存储 / 2 aws / 3 oss），而这个值在
# 预签发那一步上游就给了。记在这里，免得为了它把 storageType 塞回工具返回、
# 让调用方去转述一个它根本不该关心的实现细节。
_KEY_STORAGE: dict = {}
_KEY_STORAGE_KEEP = 100


def note_storage_type(object_key: str, storage_type) -> None:
    if not object_key or storage_type is None:
        return
    _KEY_STORAGE[object_key] = storage_type
    if len(_KEY_STORAGE) > _KEY_STORAGE_KEEP:
        for key in list(_KEY_STORAGE)[:-_KEY_STORAGE_KEEP]:
            _KEY_STORAGE.pop(key, None)


def storage_type_of(object_key: str) -> int:
    """默认按 aws 算：测试与线上环境都是 2，私有化部署才是 1。
    猜错的后果只是这一次检测失败，而检测失败是放行的，不会拦住提交。"""
    return _KEY_STORAGE.get(object_key or "", 2)


# 检测结果按 objectKey 记一份：上传后就检测，提交时直接取，不必为同一个文件
# 打两趟上游（那一趟要下载并分析整个 PDF，不便宜）。
_KEY_OCR: dict = {}
_KEY_OCR_KEEP = 100


def remember_ocr(object_key: str, info: dict) -> None:
    if not object_key or not info:
        return
    _KEY_OCR[object_key] = info
    if len(_KEY_OCR) > _KEY_OCR_KEEP:
        for key in list(_KEY_OCR)[:-_KEY_OCR_KEEP]:
            _KEY_OCR.pop(key, None)


def ocr_of(object_key: str):
    return _KEY_OCR.get(object_key or "")


# 正在后台跑的检测。网页端是文件一传完就 void 掉一个请求、不阻塞用户继续填表
# （UploadFileSection 的 axios.put.then 里），这边照做：上传返回不等它，等到
# 提交翻译时再来收结果，那会儿多半早就回来了。
_OCR_TASKS: dict = {}

# isOcr 那一趟自己的预算。上游要把整个 PDF 下下来交给分类器，大文件常跑 > 30 秒
# （网页端为此把 Next dev 代理的 proxyTimeout 从默认 30 秒提到 120 秒，见
# free-pdf-translate next.config.js）。给到 5 分钟：这趟一律在后台跑，没有人挂着
# 等它，跑多久都不占任何一次工具调用的时间；给短了则大文件永远测不出来，而测不
# 出来的代价是扫描件按普通 PDF 提交、翻出一片空白还扣错账。
_OCR_UPSTREAM_TIMEOUT = 300.0

# 提交时最多等检测多久——这是「调用方愿意等多久」，和上面那个是两码事。
# 这一步夹在 translate_document 里面，等满就等于让调用方超时重试，而重试会真的
# 多出一单（见 _detect_ocr）。等不到就 fail-open：后台那趟还在跑，结果会落进
# 缓存；服务端的自动 OCR 也会在受理时自己再判一次（实测：一单以 is_ocr=0 提交，
# 落库仍是 isOcr=1）。
_SUBMIT_OCR_WAIT = 15.0


# 双层 PDF = 图片上盖了一层文字（多半是别处 OCR 过一遍留下的）。直接翻会翻到
# 那层文字上，而那层往往是错字；拍平成纯图片再交给这边的 OCR 反而更准。
DOUBLE_DECK_NOTE = (
    "⚠️ 这是双层 PDF（扫描图上盖了一层文字，多半是别处 OCR 过留下的）。"
    "直接翻译会翻到那层文字上，它往往是错的。建议先用 "
    "https://belindoc.com/zh/tools/flatten-pdf 把它拍平，再重新上传翻译。"
    "这句请原样告诉用户，把链接完整给出，由用户决定拍不拍平。"
)


def is_pdf(file_name: str) -> bool:
    return (file_name or "").lower().endswith(".pdf")


def key_never_uploaded(object_key: str) -> bool:
    """本服务发过这个 key 的链接，但从没经手过它的字节"""
    return (object_key or "") in _ISSUED_KEYS


def _attach_upload_command(result: dict, kind: str = "document") -> None:
    """给预签名结果补一条可直接执行的上传命令

    Content-Disposition 必须与预签名时的取值逐字一致，否则 S3 返回
    SignatureDoesNotMatch。这里直接给出正确形式，避免调用方自行拼错。
    """
    if result.get("code") != "200":
        return
    for item in result.get("data") or []:
        url = item.get("persignedUploadUrl")
        encoded = item.get("encodeFileName")
        if not url or not encoded:
            continue
        item["contentDisposition"] = f"attachment; filename*=UTF-8''{encoded}"
        note_issued_key(item.get("objectKey", ""))
        note_storage_type(item.get("objectKey", ""), item.get("storageType"))
        # --progress-bar 把进度写到 stderr，并且用 \r 原地覆盖。调用方把输出捕获成
        # 文本时，\r 会让整段进度挤成一行乱码，等于没有进度。转成 \n 之后每一档
        # 各占一行：边跑边刷的终端能实时滚动，事后翻日志也读得懂。
        # -w: S3 成功时返回空 body，没有这行就无从判断结果
        curl = (
            f"curl -X PUT --progress-bar --upload-file '<本机文件的完整路径>' "
            f"-H \"Content-Disposition: attachment; filename*=UTF-8''{encoded}\" "
            f"'{url}' "
            f"-w '\\n上传结果 HTTP %{{http_code}} | %{{size_upload}} 字节 | "
            f"%{{time_total}}s | %{{speed_upload}} B/s\\n'"
        )
        log_path = _upload_progress_log(item.get("objectKey", ""))
        item["uploadCommand"] = curl + " 2>&1 | tr '\\r' '\\n'"
        item["progressLogPath"] = log_path
        # 后台版原来是单独一个字段，等于把七百字符的签名 URL 又抄了一遍，而十次里
        # 有九次用不上。改成在说明里给出包法，要用的时候自己套一层就是了。
        item["uploadNote"] = (
            "这条命令要连外网（把文件 PUT 到 S3）。你那边默认不给网络权限的话，"
            "第一次就带着联网/提权跑，别先试一遍、等它报 Could not resolve host 再去申请——"
            "那一趟纯属白跑，用户还得多批一次。"
            "原样跑 uploadCommand，只替换文件路径。Content-Disposition 一个字符都不能改"
            "（改了 S3 报 SignatureDoesNotMatch）；结尾的 -w 和 tr 也不能删、不能把管道"
            "改成 `| tail`（实测这么改之后屏幕上只剩进度条，结果那一行没了）——"
            "-w 是整条命令里唯一能看到结果的地方。"
            "PDF 传完（看到 HTTP 200）之后先调 "
            "check_pdf_ocr（把 objectKey 传进去）问一下是不是扫描件，再去提交翻译。"
            "超过 100MB 就把整条命令包起来放后台："
            f"`( <uploadCommand> > {log_path} 2>&1 ) &`，再 `tail -n 3 {log_path}` 看进度。"
            "--progress-bar 打出来的 #=#=# 只是进度条，不是结果：出现「上传结果 HTTP 200」"
            "这一行才算成功。没有这一行就是没验证过，不要宣布上传完成，更不要接着提交"
            "翻译——提交会真扣费，而这个 objectKey 底下可能根本没有文件。"
            f"成功后{_next_step_hint(kind)}。"
            + _upload_expiry_note(url)
            # 实测：上传命令被客户端沙箱挡住（curl 无输出地挂住、nslookup 报
            # bind: Operation not permitted），模型据此断定「用户网络受限、DNS
            # 解析失败」，让用户去开 VPN 换机器——而用户的网络好好的，是跑命令
            # 那一端没有联网权限。原因不明就别替用户诊断他的网络。
            + "没出现「上传结果 HTTP 200」时先分清是哪一种，不要笼统说成「网络不通」："
            "(a) 命令挂住、没有任何输出，或报 Operation not permitted / nice failed"
            "——这是**跑命令的那一端**被沙箱挡了外网，不是用户的网络坏了、更不是 DNS，"
            "请如实这么说，并以带网络权限的方式重跑同一条命令，不要建议用户开 VPN 或换机器；"
            "(b) HTTP 403 或 SignatureDoesNotMatch——链接过期或 Content-Disposition 被改过，"
            "重调本工具取新链接再传，别拿旧链接反复重试；"
            "(c) 其他 HTTP 码，把那一行原样告诉用户，别自己解释成网络问题。"
        )


# ============ 账户快照 ============
# HTTP 模式下每个请求都会新建一个 TranslationClient，缓存只能挂在模块级；
# 按 api key 的指纹分桶，别把 key 本身当字典键。
_ACCOUNTS: dict = {}
_ACCOUNT_TTL = 300
_ACCOUNT_FAIL_TTL = 60
_ACCOUNT_KEEP = 20


def _account_key(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode()).hexdigest()[:16]


def _prune_accounts() -> None:
    if len(_ACCOUNTS) <= _ACCOUNT_KEEP:
        return
    for key in sorted(_ACCOUNTS, key=lambda k: _ACCOUNTS[k]["at"])[:-_ACCOUNT_KEEP]:
        _ACCOUNTS.pop(key, None)


def _envelope_data(result) -> Optional[dict]:
    """从上游信封里取 data，取不到（异常、非 200）返回 None"""
    if isinstance(result, BaseException) or not isinstance(result, dict):
        return None
    if str(result.get("code")) not in ("200",):
        return None
    data = result.get("data")
    return data if isinstance(data, dict) else None


def _epoch_day(value) -> str:
    """上游的时间戳是毫秒 epoch"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(value) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


_OCR_WALLET_KEYS = (
    "ocrTranslateQuota", "totalFreeOcrTranslateQuota", "useFreeOcrTranslateQuota",
)


def _free_used(total, used):
    """今日免费额度用了多少 / 一共多少，外加一句「这数据不可信」

    原先这里算的是「剩余」，然后拿它和钱包相加当可用额度。2026-09-04 实测推翻了
    那个前提：一单全走免费额度（上游记 freeTranslateQuota=1、walletTranslateQuota=0），
    translateQuota 照样减了 1——它本来就是含免费在内的总数，加一次等于重复计，
    最多能虚报 1200。现在只把这两个数原样报出去，不再相加。

    实测上游还把已用量回成过负数（-4，正好等于上一单的实扣）。这种数不能原样念，
    标出来让下游连同「这一项以平台页面为准」一起说。
    """
    total = total or 0
    used = used or 0
    if used < 0 or used > total:
        return used, total, (
            f"上游回的今日免费已用是 {used}，总量 {total}，这个数说不通。"
            "免费这一项以平台页面为准。"
        )
    return used, total, ""


def _build_account_snapshot(wallet_result, sub_result) -> dict:
    """把钱包和订阅两份响应压成一屏能读完的样子

    免费额度是「用了多少 / 总共多少」两个数，得自己减；钱包额度才是余额本身。
    两个都拿不到就 ok=False，绝不拿半份数据去做限额判断。
    """
    wallet = _envelope_data(wallet_result)
    sub = _envelope_data(sub_result)
    if wallet is None and sub is None:
        reason = ""
        for result in (wallet_result, sub_result):
            if isinstance(result, BaseException):
                reason = f"{type(result).__name__}: {result}"
                break
            if isinstance(result, dict) and result.get("msg"):
                reason = str(result.get("msg"))
                break
        return {"ok": False, "error": reason or "上游没有返回账户信息"}

    snapshot: dict = {"ok": True}
    if wallet is not None:
        free_used, free_total, suspect = _free_used(
            wallet.get("totalFreeTranslateQuota"), wallet.get("useFreeTranslateQuota"),
        )
        if suspect:
            snapshot["quotaSuspect"] = suspect
        # translateQuota 就是可用额度本身（已含今日免费的消耗），不要再加什么
        purse = wallet.get("translateQuota") or 0

        ocr_free_used, ocr_free_total, ocr_suspect = _free_used(
            wallet.get("totalFreeOcrTranslateQuota"), wallet.get("useFreeOcrTranslateQuota"),
        )
        if ocr_suspect and "quotaSuspect" not in snapshot:
            snapshot["quotaSuspect"] = "OCR " + ocr_suspect
        ocr_purse = wallet.get("ocrTranslateQuota") or 0

        snapshot["quota"] = {
            "wallet": purse,
            "freeUsed": free_used,
            "freeTotal": free_total,
            # 高级模型（getModelList 里 coefficient>1 的那几个）的额度，归属未实测
            "advanced": wallet.get("advancedTranslateQuota"),
        }
        # 一个 OCR 字段都没回（老版本服务端）就整组不报——报成 0 会被念成
        # 「OCR 额度用完了」，那是编出来的。
        if any(k in wallet for k in _OCR_WALLET_KEYS):
            snapshot["quota"].update({
                "ocrWallet": ocr_purse,
                "ocrFreeUsed": ocr_free_used,
                "ocrFreeTotal": ocr_free_total,
            })
    if sub is not None:
        snapshot["vip"] = {
            "name": sub.get("vipName"),
            "type": sub.get("vipType"),
            "expiresOn": _epoch_day(sub.get("endTime")),
        }
        limits = {
            "videoDurationMinutes": sub.get("videoDurationLimit"),
            "videoFileSizeMB": sub.get("videoFileSize"),
            "uploadFileSizeMB": sub.get("uploadFileSize"),
            "videoConcurrency": sub.get("videoTranslateConcurrency"),
            "docConcurrency": sub.get("concurrenceTask"),
        }
        snapshot["limits"] = {k: v for k, v in limits.items() if v is not None}
    return snapshot


def video_duration_over_limit(duration_ms, snapshot: dict):
    """超没超会员档的单个视频时长上限。判据和服务端逐字一致：
    VideoTranslateServiceImpl 里是 videoDuration/1000 > videoDurationLimit*60，
    单位是分钟。查不到限额就返回 None——放行，让服务端自己去拒。"""
    limit = (snapshot.get("limits") or {}).get("videoDurationMinutes")
    if not snapshot.get("ok") or not limit or not duration_ms:
        return None
    if duration_ms / 1000 <= limit * 60:
        return None
    return limit


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return t("duration.seconds", seconds=max(1, seconds))
    return t("duration.minutes", minutes=seconds // 60, seconds=seconds % 60)


def _human_duration(seconds: float) -> str:
    """估算值用，带个「约」字；已经实测出来的时长请直接用 _format_duration"""
    return f"约 {_format_duration(seconds)}"


# ============ 翻译等待的跨调用记账 ============
# wait_for_translation 每次调用都重新起算，只报本次等了多久。而退避序列是固定的
# 2,3,4,5,6,7,8,8,8——累计到第 9 跳正好越过 45 秒，于是每次调用都在同一个位置超时，
# 返回逐字节相同的响应。调用方拿不到任何新信息，只能反复复述「还在等」。
# 这里按订单号记总账，让每次返回至少「累计时长」这一项是新的。
_WAITS: dict[str, dict] = {}
_WAIT_KEEP = 50
# 进度刚变就立刻返回的下限：太快返回会让调用方原地打转
_WAIT_MIN_SECONDS = 5
# 上游各接口的时间字段名不统一
_SUBMIT_TIME_KEYS = ("createTime", "createdTime", "gmtCreate", "submitTime", "createDate")


def _submitted_at(data: dict):
    """取任务提交时间。实测上游给的是 epoch 毫秒（1788229749506 = 2026-09-01
    10:29:09 +08，与订单号 TR20260901102909 对得上）。"""
    for key in _SUBMIT_TIME_KEYS:
        value = data.get(key)
        if value:
            return value
    return None


def _submitted_info(data: dict) -> tuple:
    """返回 (原始提交时间, 距今秒数)。epoch 不带时区，可以放心换算；
    换不出来就只透出原值，绝不瞎猜格式——报错的时长比不报更误导人。"""
    raw = _submitted_at(data)
    if raw is None:
        return None, None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return raw, None
    if ts > 1e11:  # 毫秒
        ts /= 1000.0
    age = time.time() - ts
    if age < 0 or age > 86400 * 30:  # 明显对不上就别报了
        return raw, None
    return raw, age


# 任务被终止的标记。上游遇到这类情况会把 status 直接换成字符串（实测见过
# "BACKEND_CANCEL"），不再是 0/2/3 的整数，只按数字判断会把它当成「未知状态」，
# 报出来的话像是我们自己的代码出了问题，而不是任务被服务端取消了。
_TERMINAL_MARKERS = ("CANCEL", "FAIL", "ERROR", "TIMEOUT", "REJECT", "ABORT")
_TERMINAL_KEYS = (
    "statusName", "statusDesc", "taskStatus",
    "errorMsg", "errorMessage", "failReason",
)


def _terminal_reason(task_status, data: dict):
    """认出「任务已经结束、但不是成功」的情况，返回可读原因；正常状态返回 None

    两种形态都要认：文档定义的终止码是整数 4/5，而实测上游也会把 status 直接
    换成字符串（见过 "BACKEND_CANCEL"）。只判字符串会漏掉前者，只判数字会漏掉
    后者。
    """
    reason = None
    if isinstance(task_status, int) and task_status in TASK_STATUS_FAILED:
        reason = f"{TASK_STATUS_FAILED[task_status]}（status={task_status}）"
    else:
        for value in [task_status] + [data.get(k) for k in _TERMINAL_KEYS]:
            if isinstance(value, str) and any(m in value.upper() for m in _TERMINAL_MARKERS):
                reason = value
                break
    if reason is None:
        return None
    # 文档说 status=4 时看 errorCode，它是数字，上面的字符串扫描抓不到
    code = data.get("errorCode")
    if code:
        reason = f"{reason}，errorCode={code}"
    return reason



def _wait_key(snapshot: dict) -> tuple:
    """判定「进度变了没有」的依据。百分比取整，免得 0.1% 的抖动把调用方吵醒。"""
    try:
        percent = int(float(str(snapshot.get("progress", "0")).rstrip("%")))
    except ValueError:
        percent = 0
    return (
        snapshot.get("statusText"),
        percent,
        snapshot.get("queueRank"),
        snapshot.get("queueTotal"),
    )


def _prune_waits() -> None:
    """只留最近的订单，长跑的服务不该把记账攒成内存泄漏"""
    if len(_WAITS) <= _WAIT_KEEP:
        return
    for k in list(_WAITS)[: len(_WAITS) - _WAIT_KEEP]:
        _WAITS.pop(k, None)


# 等待期间的活口。上游只能轮询，而一次调用要等几十秒到几分钟，这中间客户端
# 界面上只有一行不动的「正在调用 wait_for_...」——没有进度、也没有计时，用户
# 分不清是在跑还是卡死了。MCP 的 progress notification 正是干这个的：客户端在
# 请求里带了 progressToken 就能收到，没带就是空转。推送是锦上添花，失败一律
# 吞掉，绝不能让它把等待本身弄挂。
_HEARTBEAT_SECONDS = 5
# 撞上 30311 限流时退避多久再查。限流不是任务失败，不该把这次等待掐断——
# 掐断了调用方多半立刻再调一次，正好又撞在枪口上。
_RATE_LIMIT_BACKOFF = 15
# 轮询起步间隔（之后各自递增退避）。提出来是为了测试能把等待压短。
_DOC_POLL_SECONDS = 2
_VIDEO_POLL_SECONDS = 5
# 调用方没指定 timeout 时，本次调用自己等多久。进度不动的那些回合，调用方
# 什么都说不出来（agentNote 明确要求闭嘴），可它每回来一次仍是一整轮对话：
# 上下文重发一遍、界面上多一行空回合。所以没变一次就把下一轮翻一倍，
# 10 → 20 → 40 → 45（上限），进度一变立刻回到 10 秒。
# 上限压在 45 是因为不少客户端的工具调用超时就在 60 秒上下，等满 60 会被它
# 判成调用失败——而任务其实好好的。
_WAIT_TIMEOUT_BASE = 10
_WAIT_TIMEOUT_MAX = 45


def _auto_timeout(record: dict) -> int:
    """按「进度连着几轮没动」决定本次等多久"""
    return min(_WAIT_TIMEOUT_BASE * 2 ** record.get("silent", 0), _WAIT_TIMEOUT_MAX)


async def _emit_progress(on_progress, percent: float, message: str) -> None:
    if on_progress is None:
        return
    try:
        await on_progress(percent, 100.0, message)
    except Exception as exc:  # 通道断了、客户端不认这个通知，都不该影响等待
        print(f"进度推送失败（不影响等待）: {exc}", file=sys.stderr, flush=True)



class _ProgressReader:
    """包住文件对象，httpx 每读一块就记一次账

    必须转发 fileno()：httpx 靠它算出 Content-Length，否则会退化成 chunked 传输，
    而 S3 预签名 PUT 不接受 chunked。
    """

    def __init__(self, fh, state: dict):
        self._fh = fh
        self._state = state

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        self._state["uploadedBytes"] += len(chunk)
        return chunk

    def fileno(self) -> int:
        return self._fh.fileno()

    def __iter__(self):
        # 只为让 httpx 认出这是个 Iterable，实际取数走上面的 read()
        return iter(lambda: self.read(65536), b"")


# ============ 重复提交拦截 ============
# 提交视频翻译是真扣费的。实测模型在任务失败后会自己换个参数直接重提，返回里
# 「问过用户之后再决定是否重新提交」那句祈使句拦不住它——和提交前的确认是同一
# 种失效方式，所以同样得在服务端硬拦。按 (objectKey, 目标语言) 记账：窗口内再
# 提必须显式带上一单的单号，逼调用方先把上一单的结果说清楚。
_VIDEO_SUBMITS: dict[tuple, dict] = {}
# 同一份文件隔多久之后再提就不算重复了
_VIDEO_SUBMIT_WINDOW = 30 * 60
_VIDEO_SUBMIT_KEEP = 50


def recent_video_submit(object_key: str, target_language: str):
    """窗口内是否已经为同一份文件、同一目标语言提交过。顺手清掉过期记录。"""
    now = time.time()
    for key, rec in list(_VIDEO_SUBMITS.items()):
        if now - rec["at"] > _VIDEO_SUBMIT_WINDOW:
            _VIDEO_SUBMITS.pop(key, None)
    return _VIDEO_SUBMITS.get((object_key or "", target_language or ""))


def _record_video_submit(object_key: str, target_language: str, data: dict) -> None:
    data = data or {}
    if len(_VIDEO_SUBMITS) > _VIDEO_SUBMIT_KEEP:
        for key in list(_VIDEO_SUBMITS)[: len(_VIDEO_SUBMITS) - _VIDEO_SUBMIT_KEEP]:
            _VIDEO_SUBMITS.pop(key, None)
    _VIDEO_SUBMITS[(object_key or "", target_language or "")] = {
        "orderNo": data.get("videoTranslateOrderNo"),
        "quota": (data.get("freeTranslateQuota") or 0) + (data.get("walletTranslateQuota") or 0),
        "at": time.time(),
    }


# ============ 提交前的真实确认 ============
# user_confirmed 是模型自己填的布尔量，服务端核实不了它背后有没有真人点过头——
# 实测就是试算完直接带 user_confirmed=true 提交，用户全程没被问过，钱已经扣了。
# 反过来看，任务失败后那道重做闸门是有效的：工具先拒一次、把「重做 / 放弃」两个
# 选项摆到返回里，模型才会停下来问人。这里把同一个形状搬到首次提交上：
#   1) 客户端支持 elicitation 时，服务端直接问真人，拿到的答复不经过模型转述；
#   2) 不支持时退回两步握手——第一次调用一律不提交，只发确认码，模型必须把选项
#      转述给用户，拿到答复后带确认码再调一次。
# 确认码一次性、绑定参数指纹：换了文件、目标语言、配音或字幕，之前那次同意就不
# 作数，得重新问。
_PENDING_CONFIRMS: dict[str, dict] = {}
_CONFIRM_TTL = 15 * 60
_CONFIRM_KEEP = 40


def confirm_fingerprint(object_key: str, target_language: str, voice_role: str, subtitle_type) -> str:
    """一次同意只对这一组参数有效。配音和字幕都进指纹——它们直接决定扣多少。"""
    return "|".join([object_key or "", target_language or "", voice_role or "", str(subtitle_type)])


def rewrite_fingerprint(order_no: str, source_txt: str, target_txt: str) -> str:
    """改写的一次同意，绑到「哪一单 + 哪一版字幕」上

    字幕正文也进指纹：用户点头同意的是他刚校对完的那一版，模型拿着这个码再去
    提交另一版（哪怕只差一行），扣的钱和产出就都不是用户同意过的那个了。
    正文可能几十 KB，取摘要即可。
    """
    digest = hashlib.sha256(
        ((source_txt or "") + "\x00" + (target_txt or "")).encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"rewrite|{order_no or ''}|{digest}"


def issue_submit_confirm(fingerprint: str, meta: Optional[dict] = None) -> str:
    now = time.time()
    for token, rec in list(_PENDING_CONFIRMS.items()):
        if now - rec["at"] > _CONFIRM_TTL:
            _PENDING_CONFIRMS.pop(token, None)
    if len(_PENDING_CONFIRMS) > _CONFIRM_KEEP:
        for token in list(_PENDING_CONFIRMS)[: len(_PENDING_CONFIRMS) - _CONFIRM_KEEP]:
            _PENDING_CONFIRMS.pop(token, None)
    token = "CONFIRM-" + secrets.token_hex(3).upper()
    # meta 记的是「这个码代表菜单里的哪一格」。提交时以它为准，调用方回传的
    # voice_role / subtitle_type 只作参考——用户点的是那一格，不是模型说的那一格。
    _PENDING_CONFIRMS[token] = {"at": now, "fingerprint": fingerprint, "meta": meta or {}}
    return token


def peek_submit_confirm(token: str) -> Optional[dict]:
    """看一眼确认码代表哪一格，不核销。核销仍然走 take_submit_confirm。"""
    rec = _PENDING_CONFIRMS.get(token or "")
    if not rec or time.time() - rec["at"] > _CONFIRM_TTL:
        return None
    return rec


def take_submit_confirm(token: str, fingerprint: str) -> tuple[bool, str]:
    """核销确认码。一次性：同一个码不能拿去提交第二单。"""
    rec = _PENDING_CONFIRMS.get(token or "")
    if not rec:
        return False, (
            f"确认码 {token!r} 不存在、已过期或已经用掉了——它一次只能提交一单。"
            "请重新调用本工具（不带 confirm_token）拿新的确认码，并把选项重新问一遍用户。"
        )
    if time.time() - rec["at"] > _CONFIRM_TTL:
        _PENDING_CONFIRMS.pop(token, None)
        return False, "确认码已过期（有效期 15 分钟）。请重新发起确认，把预算再跟用户说一遍。"
    if rec["fingerprint"] != fingerprint:
        return False, (
            "这个确认码是给另一组参数发的：文件、目标语言、配音、字幕里至少有一项和当时不一样。"
            "这些都会改变扣费和产出，用户同意的是当时那一组，不能拿来放行现在这一组——"
            "请不带 confirm_token 重新发起确认。"
        )
    _PENDING_CONFIRMS.pop(token, None)
    return True, ""


# 试算免费、提交真扣费，两边的 voiceRole / subtitleType 必须是同一套，否则
# 「预算 4 额度」和「实扣 8 额度」之间没有任何东西拦着（clone 配音就是二倍）。
# 这里把最近的试算记下来，提交时按 (配音, 字幕) 回查，对不上不放行。
_QUOTA_CALCS: list[dict] = []
_QUOTA_CALC_WINDOW = 30 * 60
_QUOTA_CALC_KEEP = 20


def record_quota_calc(duration_ms: float, voice_role: str, subtitle_type, data: dict) -> None:
    data = data or {}
    _QUOTA_CALCS.append({
        "durationMs": duration_ms,
        "voiceRole": voice_role,
        "subtitleType": subtitle_type,
        "quota": data.get("translateQuota"),
        "units": data.get("videoDuration"),
        "at": time.time(),
    })
    del _QUOTA_CALCS[:-_QUOTA_CALC_KEEP]


def recent_quota_calc(voice_role: str, subtitle_type):
    """窗口内最近一次、且配音/字幕与本次提交完全一致的试算。"""
    now = time.time()
    _QUOTA_CALCS[:] = [c for c in _QUOTA_CALCS if now - c["at"] <= _QUOTA_CALC_WINDOW]
    for calc in reversed(_QUOTA_CALCS):
        if calc["voiceRole"] == voice_role and calc["subtitleType"] == subtitle_type:
            return calc
    return None

# 上游 language 头的取值文档没有列举，按常见写法映射；上游不认时会退回它的
# 默认语种，不影响我们的逻辑（分支只认 code）。
_UPSTREAM_LANGUAGE = {
    "zh": "zh-CN", "zh-Hant": "zh-TW", "en": "en-US", "ja": "ja-JP", "ko": "ko-KR",
    "de": "de-DE", "fr": "fr-FR", "ru": "ru-RU", "ar": "ar-SA",
}


async def _set_upstream_language(request) -> None:
    from .i18n import current as _current_locale
    request.headers["language"] = _UPSTREAM_LANGUAGE.get(_current_locale(), "zh-CN")


class TranslationClient:
    """翻译 API 客户端"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
                # 不传这个头，上游的错误信息默认回英文。真实取值由
                # _set_upstream_language 按本次调用的 locale 逐请求覆盖。
                "language": "zh-CN",
            },
            timeout=httpx.Timeout(30.0),  # 30秒超时
            # 逐请求改 language 头：上游的 msg 按这个头返回语种，而 locale 是
            # 每次工具调用才知道的。我们自己的分支只认 code，所以就算上游不认
            # 某个取值、退回默认语种，也只影响透传给用户的那句话。
            event_hooks={"request": [_set_upstream_language]},
        )
    
    async def close(self):
        await self.client.aclose()
    
    async def _post(self, path: str, payload: dict, timeout=None) -> dict:
        """POST 并把 404 翻译成人话

        上游对未部署的接口直接回 404，httpx 抛出来的是一句英文 HTTPStatusError，
        调用方会当成网络故障，反复重试或者告诉用户「服务器维护中」——实测视频
        那批接口就是这样被误判的。实际是该接口在当前服务地址上不存在，重试无用。
        """
        kwargs = {"timeout": timeout} if timeout is not None else {}
        response = await self.client.post(path, json=payload, **kwargs)
        if response.status_code == 404:
            return _not_found(path)
        response.raise_for_status()
        return response.json()

    async def _post_sse(self, path: str, payload: dict, timeout: float = 45.0) -> dict:
        """POST 一个 SSE 接口，拿到第一帧带数据的事件就返回

        上游会一路推到任务终态（可能几分钟），一次状态查询没必要挂在那儿。
        另外鉴权失败时上游回的是普通 JSON 而不是 SSE（文档 3.5 的告警），
        两种形态都得认——之前按普通 JSON 解析之所以“看起来能用”，正是因为
        只跑通了这条错误路径。
        """
        try:
            async with self.client.stream(
                "POST", path, json=payload,
                timeout=httpx.Timeout(timeout, connect=30.0),
            ) as response:
                # 不要加 Accept: text/event-stream。实测带上它，上游在错误路径
                # 直接回 HTTP 500 空 body；不带反而能正常拿到 JSON 错误体，
                # 成功时照样推 SSE。
                if response.status_code == 404:
                    return _not_found(path)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    body = await response.aread()
                    try:
                        return json.loads(body)
                    except ValueError:
                        pass
                    return {
                        "code": str(response.status_code),
                        "msg": (
                            f"上游返回 HTTP {response.status_code}，响应体既不是 SSE 也不是 JSON"
                            f"{'（body 为空）' if not body else ''}。"
                            "可改用 list_video_translations 查这个任务的状态。"
                        ),
                        "raw": body[:500].decode("utf-8", "replace"),
                    }

                event = None
                async for line in response.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk:
                        continue          # [PROCESS] 首帧的 data 是空的
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        continue
                    if event == "[ERROR]":
                        return {"code": "500", "msg": "上游返回 [ERROR] 事件",
                                "sseEvent": event, "data": parsed}
                    # 已经是标准信封就原样返回，否则包一层
                    if isinstance(parsed, dict) and "code" in parsed:
                        parsed.setdefault("sseEvent", event)
                        return parsed
                    return {"code": "200", "sseEvent": event, "data": parsed}
        except httpx.TimeoutException:
            return {
                "code": "504",
                "msg": (
                    f"{timeout:.0f} 秒内没有收到 SSE 数据帧。任务可能仍在进行，"
                    "可改用 list_video_translations 轮询查看状态。"
                ),
            }
        return {"code": "500", "msg": "SSE 流已结束但没有取到任何数据帧"}


    # ============ 语言相关 ============
    
    async def get_language_enum(self, display_locale: str = "zh") -> dict:
        """获取支持的语言列表

        上游按界面语言返回 9 份完全等价的语种表（同样的语言码，只是显示名不同），
        全部返回会白白占掉上万字符的上下文。这里只保留其中一份。
        """
        response = await self.client.post(f"{DOC_PREFIX}/getLanguageEnum", json={})
        response.raise_for_status()
        result = response.json()
        
        data = result.get("data")
        if isinstance(data, dict) and data:
            # 语言码是各份共有的，显示名按 display_locale 选一份即可
            for locale in (display_locale, "zh", "en"):
                if locale in data:
                    result["data"] = data[locale]
                    result["displayLocale"] = locale
                    break
            else:
                first = next(iter(data))
                result["data"] = data[first]
                result["displayLocale"] = first
            result["msg"] = (
                "键为语言码（用于 source_language / target_language），值为显示名。"
                "AnyLanguage 表示自动识别源语言。"
            )
        
        return result
    
    # ============ 模型相关 ============
    
    async def get_model_list(self) -> dict:
        """获取翻译模型列表，按当前账号的会员档位筛出真正能用的

        上游把全量模型都回下来，每条自带 vipType（用它需要的最低档位）。这里
        原先写死 vipType<=0 当作「可用」，等于假定谁都是免费用户：实测 Ultimate
        （vipType=20）账号十个模型只看得见两个，GPT-5.5、Gemini-3.1-Pro、
        Claude-haiku-4-5 这些买了的全被自己滤掉了。改成拿账号自己的档位去比。

        查不到档位（老服务端没有 /external/user、或者网络抖）就全量返回并说明
        情况——宁可把一个可能不可用的模型摆出来让服务端去拒，也不能反过来把
        用户已经付过钱的模型藏起来。
        """
        response = await self.client.post(f"{DOC_PREFIX}/getModelList", json={})
        response.raise_for_status()
        result = response.json()
        if result.get("code") != "200":
            return result

        models = result.get("data") or []
        snapshot = await self.account_snapshot()
        tier = (snapshot.get("vip") or {}).get("type") if snapshot.get("ok") else None

        available, locked = [], []
        for m in models:
            need = m.get("vipType") or 0
            entry = {"model": m.get("version"), "coefficient": m.get("coefficient") or 1}
            if tier is None or need <= tier:
                available.append(entry)
            else:
                locked.append({**entry, "requiresVipType": need})

        # coefficient 是计费倍率：同样一页，倍率 3 的模型扣三倍额度。用户挑模型
        # 时这是要摆在台面上的信息，不能只报个名字。
        msg = "请让用户从 data 里选一个模型。coefficient 是计费倍率，3 表示同样的量扣三倍额度。"
        if tier is None:
            msg += "（没查到本账号的会员档位，这里给的是上游全量模型；档位不够的提交时会被服务端拒掉。）"
        elif locked:
            msg += f"另有 {len(locked)} 个模型当前档位（vipType={tier}）用不了，列在 locked 里，别拿它们去提交。"

        out = {"code": "200", "data": available, "msg": msg}
        if locked:
            out["locked"] = locked
        if tier is not None:
            out["vipType"] = tier
        return out
    
    # ============ 配额相关 ============
    
    async def get_wallet_info(self) -> dict:
        """钱包额度。/external/user 是上游 2026-09-02 才开放的开放平台接口"""
        return await self._post(f"{USER_PREFIX}/getMyWalletInfo", {})

    async def get_subscription_info(self) -> dict:
        """订阅信息：会员档位、到期时间，以及各项限额"""
        return await self._post(f"{USER_PREFIX}/getMySubscriptionInfo", {})

    async def account_snapshot(self, refresh: bool = False) -> dict:
        """额度 + 权益的合并快照，带 5 分钟缓存

        提交前的限额校验每次都要用它，不能每次都打两趟上游。拿不到（老版本
        服务端没这两个接口、或者网络抖）就返回 ok=False，调用方一律放行——
        限额校验是锦上添花，不能因为查不到就把用户的正常提交拦下来。
        """
        key = _account_key(self.api_key)
        cached = _ACCOUNTS.get(key)
        now = time.time()
        if cached and not refresh and now - cached["at"] < cached["ttl"]:
            return cached["snapshot"]

        wallet, sub = await asyncio.gather(
            self.get_wallet_info(), self.get_subscription_info(),
            return_exceptions=True,
        )
        snapshot = _build_account_snapshot(wallet, sub)
        _ACCOUNTS[key] = {
            "at": now,
            "ttl": _ACCOUNT_TTL if snapshot.get("ok") else _ACCOUNT_FAIL_TTL,
            "snapshot": snapshot,
        }
        _prune_accounts()
        return snapshot
    
    # ============ 文档翻译 ============
    
    async def doc_batch_presigned_upload_url(self, file_name_list: list[str]) -> dict:
        """批量获取文档预签名上传 URL"""
        response = await self.client.post(
            f"{DOC_PREFIX}/batchPresignedUploadUrl",
            json={"fileNameList": file_name_list, "businessType": 1}
        )
        response.raise_for_status()
        result = response.json()
        _attach_upload_command(result)
        return result
    
    async def wait_for_translation(self, order_no: str, timeout=None, on_progress=None) -> dict:
        """等待翻译任务完成，自动轮询状态

        上游只在轮询时给出进度，无法主动推送。两个返回时机：进度一变就立刻返回，
        否则最多等 timeout 秒。之前不管上游动没动都要熬满超时，连着调几次拿到的是
        逐字节相同的响应，调用方只好反复复述「还在等」。
        超时不算失败。
        """
        import asyncio

        record = _WAITS.setdefault(
            order_no, {"totalElapsed": 0.0, "calls": 0, "lastKey": None, "silent": 0}
        )
        record["calls"] += 1
        _prune_waits()
        if timeout is None:
            timeout = _auto_timeout(record)

        start_time = asyncio.get_event_loop().time()
        last_logged = -1
        interval = _DOC_POLL_SECONDS  # 轮询间隔，逐步放宽到 8 秒，避免高频打上游
        snapshot = {}
        # 同 wait_for_video_translation：跟上一次返回的那行比，别跟本轮基线比
        entry_key = record["lastKey"]

        def elapsed_now() -> float:
            return asyncio.get_event_loop().time() - start_time

        def live_percent() -> float:
            try:
                return float(str(snapshot.get("progress", "0")).rstrip("%"))
            except ValueError:
                return 0.0

        def status_line(total_seconds: float, polls: bool = False) -> str:
            """要念给用户听的那一行：状态 · 百分比 · 排队 · 已提交多久 · 已等多久。
            整行都走消息表——它会被原样转述给用户，混着中文就成了半截译文。"""
            parts = [snapshot.get("statusText") or t("progress.querying")]
            if snapshot.get("progress"):
                parts.append(snapshot["progress"])
            if snapshot.get("queueRank"):
                parts.append(t("progress.queue", rank=snapshot["queueRank"],
                               total=snapshot["queueTotal"]))
            if snapshot.get("sinceSubmit"):
                parts.append(t("progress.since_submit", duration=snapshot["sinceSubmit"]))
            duration = _format_duration(total_seconds)
            parts.append(
                t("progress.polls", calls=record["calls"], duration=duration)
                if polls else t("progress.waited", duration=duration)
            )
            return " · ".join(parts)

        def live_line() -> str:
            return status_line(record["totalElapsed"] + elapsed_now())

        async def heartbeat(seconds: float) -> None:
            """轮询间隔照旧，但计时每 _HEARTBEAT_SECONDS 秒就走一格"""
            deadline = elapsed_now() + seconds
            while True:
                left = deadline - elapsed_now()
                if left <= 0 or elapsed_now() > timeout:
                    return
                await asyncio.sleep(min(_HEARTBEAT_SECONDS, left))
                await _emit_progress(on_progress, live_percent(), live_line())

        def pending(elapsed: float, changed: bool) -> dict:
            """排队/翻译中时的返回体，带上跨调用的累计等待"""
            changed = changed or _wait_key(snapshot) != entry_key
            # 连着几轮没动就让下一次调用等得更久，见 _auto_timeout
            record["silent"] = 0 if changed else record.get("silent", 0) + 1
            record["totalElapsed"] += elapsed
            record["lastKey"] = _wait_key(snapshot)
            total = record["totalElapsed"]
            # 不管进度变没变，都要求把当前进度说出来。之前写的是「没变化就不必
            # 复述」，结果调用方在这些回合里什么都不说，界面上只剩一串省略号。
            note = (
                "把 msg 那行原样告诉用户，然后再次调用本工具继续等待。"
                if changed else
                # 同视频等待：没有新东西可说的那一轮不该产生任何输出
                "进度和上次完全一样，没有新东西可告诉用户："
                "**不要输出任何文字**（连「继续等待」这类过场话也不要），"
                "直接再次调用本工具继续等待。等到进度真的变了或任务结束，再开口。"
            )
            # data 原来把 statusText/progress/排队位置又抄一遍，和 msg 说的是同一
            # 件事；一个任务要轮询十几次，两份重复就把对话刷满了。机器要用的只有
            # finished，剩下的话全在 msg 那一行里。
            return {
                "code": "202",
                "msg": status_line(total, polls=True) + "。",
                "agentNote": note,
                "data": {"orderNo": order_no, "finished": False},
            }

        await _emit_progress(on_progress, 0.0, live_line())

        while True:
            # 检查超时
            elapsed = elapsed_now()
            if elapsed > timeout:
                return pending(elapsed, changed=False)

            # 查询翻译状态
            try:
                status = await self.get_translate_file_detail(order_no)
                code = str(status.get("code") or "")
                if code != "200":
                    if code in KEY_ERROR_RETRYABLE:
                        # 限流：退避接着等，等待本身不算失败
                        await heartbeat(max(interval, _RATE_LIMIT_BACKOFF))
                        interval = min(interval + 1, 8)
                        continue
                    _WAITS.pop(order_no, None)
                    return status

                data = status["data"]
                task_status = data["status"]
                progress_info = data.get("taskProgress")
                progress = float(progress_info["progress"]) if progress_info else 0

                # 任务被取消/失败时 status 会变成字符串，先于数字分支判掉，
                # 否则会落进「未知状态」，读起来像是我们的代码没见过这个值
                reason = _terminal_reason(task_status, data)
                if reason:
                    total = record["totalElapsed"] + elapsed
                    _WAITS.pop(order_no, None)
                    return {
                        "code": "500",
                        "msg": f"任务已被终止：{reason}（累计等待 {_format_duration(total)}）。",
                        "agentNote": (
                            "这是翻译服务侧的问题，不是上传或参数出错——文件还在，"
                            "fileObjectKey 依然有效，用相同参数重新调 translate_document 即可重试，"
                            "不要重新上传文件。但请先把失败原因告诉用户、问过用户之后再重试，"
                            "不要自己直接重提。"
                        ),
                        "data": {
                            "orderNo": order_no,
                            "finished": True,
                            "failed": True,
                            "status": task_status,
                            "reason": reason,
                            "fileName": data.get("sourceFileName"),
                            "totalElapsedSeconds": round(total),
                            "totalWaited": _format_duration(total),
                        },
                    }

                # 状态见文档 5.2：0 未开始 / 1 解析中 / 2 翻译中 / 3 完成
                if task_status == TASK_STATUS_DONE:
                    # 必须走下载接口拿地址，不能图省事直接用详情里的 targetFileUrl：
                    # 详情给的是上游默认生成的那份，文件名带 _WM_，是有水印的，而
                    # isWatermark 只对 getTranslateS3DownloadUrl 生效。
                    # 先要无水印，账号没这个权限再退回带水印。
                    download = await self.get_translate_s3_download_url(order_no, 2, 0)
                    watermark = False
                    if not download.get("url"):
                        download = await self.get_translate_s3_download_url(order_no, 2, 1)
                        watermark = True

                    variants = _available_variants(data)
                    target_url = download.get("url")
                    target_url_cn = download.get("url2")
                    if not target_url:
                        # 两次都没拿到，最后退回详情里的地址（带水印）
                        target_url = variants.get(2)
                        target_url_cn = data.get("targetFileUrl2")
                        watermark = True

                    total = record["totalElapsed"] + elapsed
                    _WAITS.pop(order_no, None)

                    # 详情里有地址的版式就是真能取到的，比按文件类型猜准。
                    # 这里只列出有哪些，不带地址——所有下载都走下载接口，
                    # 免得混进没做水印控制的链接。
                    # 详情里已经有地址的 ∪ 这个类型本来就支持的，去掉纯译文自己
                    offered = offered_variants(data)
                    others = [f"url_type={n} {URL_TYPE_LABELS[n]}" for n in offered]
                    payload = {
                        "orderNo": order_no,
                        "finished": True,
                        "fileName": data["sourceFileName"],
                        "fileType": doc_file_type(data),
                        "sourceLanguage": data["sourceLanguage"],
                        "targetLanguage": data["targetLanguage"],
                        "model": data["model"],
                        "textNumber": data.get("textNumber"),
                        "elapsed": round(elapsed),
                        "totalElapsedSeconds": round(total),
                        "totalWaited": _format_duration(total),
                        "watermark": watermark,
                        "expiresAt": _expires_at(target_url),
                        "downloadUrl": target_url,
                        "downloadUrlCN": target_url_cn,
                        "downloadNote": (
                            t("download.doc.watermark" if watermark else "download.doc.clean")
                            + "downloadUrlCN 是同一份文件的国内兜底线路，"
                            "前者慢或不通时改用后者（对照版式也有国内线路，"
                            "get_document_translation_result 会一并返回 url2）。"
                            + _URL_VERBATIM_NOTE
                            + _expiry_note(target_url)
                            + (
                                "本文件还可以取这些版式，用 get_document_translation_result："
                                + "、".join(others) + "；原文用 url_type=1 取。"
                                if others
                                else "本文件没有其他可用版式（原文用 url_type=1 取）。"
                            )
                        ),
                    }
                    if others:
                        payload["availableVariants"] = others
                    offer = comparison_offer(data, offered)
                    done = {
                        "code": "200",
                        # offer 本身是说给用户听的（「还能导出双语对照」），留在 msg；
                        # 「不问也要说」那句是操作指令，走 agentNote。
                        "msg": f"翻译完成（累计等待 {_format_duration(total)}）。" + (offer or ""),
                        "data": payload,
                    }
                    if offer:
                        # 实测两次：这句话跟在「翻译完成…」后面就是活不下来——模型把
                        # msg 改写成自己的卡片，第一句之后全丢，downloadNote 的尾巴
                        # 同样没跟出来。所以再单独发一个 content 块，纯用户文案、
                        # 独占一段，摆在 JSON 前面，别再指望它从长串里把这句捞出来。
                        done["sayToUser"] = offer + t("download.doc.layouts.cost")
                    if offer:
                        done["agentNote"] = (
                            "msg 里那句版式说明请主动告诉用户（他不问也要说：网页端"
                            "把这两项明摆在下载菜单里，接口这边不说他就不知道有）。"
                            "他要哪一版，就用 get_document_translation_result 按"
                            "对应的 url_type 取，别自己替他决定只给纯译文。"
                        )
                    return done
                elif task_status in TASK_STATUS_PENDING:
                    # 未开始/解析中/翻译中，记录快照供返回时带出
                    status_text = doc_status_text(task_status)
                    snapshot = {
                        "statusText": status_text,
                        "progress": f"{progress:.1f}%",
                        "fileName": data.get("sourceFileName"),
                    }
                    submitted, age = _submitted_info(data)
                    if submitted is not None:
                        snapshot["submittedAt"] = submitted
                    if age is not None:
                        # 从提交算起的总时长，比我们自己数的等待秒数更贴近用户的感受
                        snapshot["sinceSubmit"] = _format_duration(age)
                    if progress_info:
                        snapshot["queueRank"] = progress_info.get("taskRanking")
                        snapshot["queueTotal"] = progress_info.get("totalTask")
                        snapshot["predictWaitSeconds"] = progress_info.get("predictWaitTime")

                    await _emit_progress(on_progress, live_percent(), live_line())

                    key = _wait_key(snapshot)
                    if record["lastKey"] is None:
                        # 头一次先立个基线，本身不算变化
                        record["lastKey"] = key
                    elif key != record["lastKey"] and elapsed >= _WAIT_MIN_SECONDS:
                        # 有新东西可说了，不必再把超时熬满
                        return pending(elapsed, changed=True)

                    if int(progress) != last_logged:
                        last_logged = int(progress)
                        print(f"[{status_text}] 进度: {progress:.1f}% (已等待 {int(elapsed)}秒)",
                              file=sys.stderr, flush=True)
                else:
                    # 既不在进行中也没有终止标记，交给调用方复查，别继续空等
                    _WAITS.pop(order_no, None)
                    return {
                        "code": "500",
                        "msg": (
                            f"未知翻译状态: {task_status}。已停止等待，"
                            "请把 data 原样告诉用户，并用 get_document_translation_status 复查。"
                        ),
                        "data": data
                    }

                # 递增退避：2s 起，逐步放宽到 8s 上限。等的过程里每
                # _HEARTBEAT_SECONDS 秒推一次计时，别让界面整段静默。
                await heartbeat(interval)
                interval = min(interval + 1, 8)

            except httpx.HTTPError as e:
                # 只有网络/超时这类瞬时故障才值得重试。原来这里 except Exception，
                # 连 TypeError、KeyError 都被吞掉后无限重试，最后报成「还在等待」
                # ——真正的错误彻底看不见。
                print(f"查询出错: {e}，{interval}秒后重试...", file=sys.stderr, flush=True)
                await asyncio.sleep(interval)

    async def check_file_is_ocr(
        self, file_object_key: str, storage_type=None, timeout: float = _OCR_UPSTREAM_TIMEOUT
    ) -> dict:
        """这份文件是不是扫描件（只对 PDF 有意义）

        上游拿 objectKey 现签一个 30 分钟的地址交给分类器，回 isOcr / isDoubleDeck。
        注意分类器打不通时上游会保守地回 isOcr=1，从响应上分不出「确实是扫描件」
        和「没测成」——所以这个结果只用来定 isOcrFile，不拿去跟用户断言什么。
        """
        return await self._post(
            f"{DOC_PREFIX}/isOcr",
            {
                "fileObjectKey": file_object_key,
                "storageType": storage_type if storage_type is not None else storage_type_of(file_object_key),
            },
            # 上游要把整个 PDF 下下来交给分类器，大文件几分钟都可能。这趟是在
            # 后台跑的，没有人挂着等，所以给足预算；工具调用那边各自有自己的
            # 等待上限，等不到就先走，不会被这个数拖住。
            timeout=timeout,
        )

    def start_ocr_detection(self, object_key: str, file_name: str = "") -> None:
        """文件一落到 S3 就把检测发出去，不等结果

        网页端给这一步留了 120 秒——它要把整个 PDF 下下来分析。同步等着会让上传
        工具凭空多挂十几秒，而这段时间用户本来正在挑模型和语言。
        """
        if not object_key or ocr_of(object_key) or object_key in _OCR_TASKS:
            return
        # 这里必须挂真去打上游的那个协程，不能挂 detect_ocr_for_key：后台任务
        # 一进 _OCR_TASKS，detect_ocr_for_key 开头那句「已经有人在测了，等它」
        # 等到的就是任务自己，死等到超时，上游一趟都没打出去。
        task = asyncio.ensure_future(
            self._run_ocr_detection(object_key, file_name)
        )
        _OCR_TASKS[object_key] = task

        def _done(finished, key=object_key):
            _OCR_TASKS.pop(key, None)
            # 取一下异常，免得事件循环打印 "Task exception was never retrieved"
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(_done)

    async def detect_ocr_for_key(
        self, object_key: str, file_name: str = "", timeout: float = _SUBMIT_OCR_WAIT
    ) -> Optional[dict]:
        """检测一个已经传上去的 PDF，并把结果记下来。等不到返回 None。

        timeout 是**调用方愿意等多久**，不是 isOcr 那一趟的预算。两者以前是同一个
        数，于是提交时那 15 秒直接变成了 isOcr 的 HTTP 超时——而大文件常跑 > 30 秒，
        等于大文件永远测不出来。现在上游那趟一律交给后台任务、拿满
        _OCR_UPSTREAM_TIMEOUT；这里等不及就放手，它照样跑完并把结果写进缓存，
        下一次（提交时、或者调用方再问一次 check_pdf_ocr）直接取。
        """
        cached = ocr_of(object_key)
        if cached:
            return cached
        self.start_ocr_detection(object_key, file_name)
        running = _OCR_TASKS.get(object_key)
        if running is None:
            # 刚好在这中间跑完了
            return ocr_of(object_key)
        # shield：等不及是我们放手，不是把那趟取消掉
        try:
            return await asyncio.wait_for(asyncio.shield(running), timeout)
        except Exception:
            return None

    async def _run_ocr_detection(
        self, object_key: str, file_name: str = "", timeout: float = _OCR_UPSTREAM_TIMEOUT
    ) -> Optional[dict]:
        """真去打上游那一趟并把结果记下来。不看 _OCR_TASKS——后台任务自己就在
        里面，看了就是等自己。"""
        try:
            result = await self.check_file_is_ocr(object_key, timeout=timeout)
        except Exception:
            return None
        if not isinstance(result, dict) or str(result.get("code")) != "200":
            return None
        data = result.get("data") or {}
        if data.get("isOcr") is None:
            return None
        # 网页端把这两个字段声明成 string | number，实测两种都出现过。
        # 拿 == 1 去比字符串 "1" 会静默地判成「不是扫描件」，所以先归一化。
        info = {
            "fileName": file_name,
            "isOcr": 1 if str(data.get("isOcr")) == "1" else 0,
            "isDoubleDeck": 1 if str(data.get("isDoubleDeck")) == "1" else 0,
        }
        remember_ocr(object_key, info)
        return info

    async def _detect_ocr(self, files: list) -> dict:
        """并发检测 file_list 里的 PDF。非 PDF 一律跳过——扫描件这个概念只对 PDF 成立。

        检测失败就当没检测过：宁可按调用方给的值提交，也不能因为一个辅助接口
        抽风把整批任务卡住。

        这里等多久是有讲究的：这一步是**夹在提交里面**的，等太久整个
        translate_document 就在调用方那边超时了，而调用方多半会原样重试一次——
        上游对文档提交不去重，于是同一份文件出两单、扣两次费（2026-09-04 实测：
        一份大 PDF 出了 TR...165354 和 TR...165445 两单，各扣 428）。所以这里
        只给一个短上限，等不到就当没测过往下走。检测本来就是上传那会儿发出去的，
        正常流程走到这儿早回来了。网页端也是这个取向：它给 isOcr 留了 120 秒，
        但那 120 秒在上传的 .then 里烧，从不挡着提交（free-pdf-translate
        src/store/trans.ts checkPdfIsOcrBatch，超时/非 200 一律 fail-open）。
        """
        targets = [
            f for f in files
            if is_pdf(f.get("fileName")) and f.get("fileObjectKey")
        ]
        if not targets:
            return {}
        # 上传那一步大概率已经测过了，命中缓存就不再打上游
        detected = {}
        pending = []
        for item in targets:
            cached = ocr_of(item["fileObjectKey"])
            if cached:
                detected[item["fileObjectKey"]] = cached
            else:
                pending.append(item)
        if not pending:
            return detected
        # detect_ocr_for_key 自己会确保后台在测：等不及把它放掉，那一趟也还在跑，
        # 结果会落进缓存，调用方稍后重试直接取到，不用再等一遍。
        results = await asyncio.gather(
            *[
                self.detect_ocr_for_key(
                    f["fileObjectKey"], f.get("fileName", ""), timeout=_SUBMIT_OCR_WAIT
                )
                for f in pending
            ],
            return_exceptions=True,
        )
        for item, info in zip(pending, results):
            if isinstance(info, dict):
                detected[item["fileObjectKey"]] = info
        return detected

    async def batch_submit_translate_task(
        self,
        file_list: list[dict],
        source_language: str,
        target_language: str,
        model: str = "Gemini-2.5-Flash",
        is_ocr=None,
        terminology_collection_id: Optional[str] = None,
    ) -> dict:
        """批量提交文档翻译任务

        上游偶发返回 600（System is busy），属于瞬时故障，内部自动重试，
        避免调用方误判成上传失败而重新上传文件。
        """
        import asyncio

        # 是不是扫描件，调用方无从知道——它手里只有文件名。之前这个参数是模型
        # 自己拍的：扫描件按普通 PDF 提交会翻出一片空白，而 OCR 走的是另一档
        # 额度（ocrTranslateQuota），拍错哪边都要付代价。上游 2026-09-02 开了
        # isOcr 接口，PDF 一律以它为准，调用方传的值只在两者不一致时拿来对照。
        requested_ocr = is_ocr
        detected = await self._detect_ocr(file_list)
        pdfs = [f for f in file_list if is_pdf(f.get("fileName"))]
        scanned = [d["fileName"] for d in detected.values() if d["isOcr"] == 1]
        undetected = [
            f.get("fileName") for f in pdfs
            if f.get("fileObjectKey") not in detected
        ]
        all_scanned = bool(detected) and not undetected and len(scanned) == len(detected)

        # 批级 isOcr 是「强制整批走 OCR」的开关：服务端 TranslateFileHistoryServiceImpl
        # 里 ocrSwatch==1 时每个 PDF 都按 OCR 记账，压根不看逐文件的检测结果。所以
        # 只有整批 PDF 都是扫描件才敢打开它——混着文本版一起强制，那几份就白扣 OCR
        # 额度了。混合批次只标逐文件的 isOcrFile，由服务端在自动 OCR 模式下逐个定。
        if is_ocr is None:
            is_ocr = 1 if all_scanned else 0

        ocr_note = ""
        if scanned:
            ocr_note = (
                f"服务端判定这些是扫描件：{'、'.join(n for n in scanned if n)}。"
                "扫描件走 OCR，扣的是 OCR 额度，和普通翻译不是同一本账，请告诉用户。"
            )
            if not all_scanned and not is_ocr:
                ocr_note += (
                    "注意这批里既有扫描件也有文本版 PDF，没有整批强制 OCR（那会让文本版"
                    "白扣 OCR 额度）：扫描件到底走不走 OCR，取决于账号的「自动 OCR」开关，"
                    "本接口看不到那个开关。要确保扫描件走 OCR，就把它单独提交一批。"
                )
        elif detected:
            ocr_note = "服务端判定这批 PDF 都是文本版，按普通翻译提交（没有走 OCR）。"
        if requested_ocr == 1 and detected and not scanned:
            ocr_note += (
                "你传了 is_ocr=1，但服务端判定这些 PDF 都是文本版——强制 OCR 会去扣 "
                "OCR 额度，且译文未必更好。除非用户明确要求，否则不要传这个参数。"
            )
        elif requested_ocr == 0 and scanned:
            ocr_note += (
                "你传了 is_ocr=0，但里面有扫描件；已按文件标上 isOcrFile=1，"
                "最终走不走 OCR 由服务端的自动 OCR 模式决定。"
            )
        double_deck = [d["fileName"] for d in detected.values() if d.get("isDoubleDeck") == 1]
        if double_deck:
            ocr_note += f"其中 {'、'.join(n for n in double_deck if n)} ".rstrip() + "：" + DOUBLE_DECK_NOTE
        if undetected:
            ocr_note += (
                f"另外这些 PDF 没能判断是不是扫描件（检测接口没答上来）：{'、'.join(undetected)}，"
                f"按 is_ocr={is_ocr} 提交了。如果译文出来是空白，多半就是扫描件，"
                "带 is_ocr=1 重新提交一次。"
            )

        # 文档 4：启用 OCR 时每个文件也要带 isOcrFile=1；普通文档 isMath/isFlow 传 0
        files = []
        for item in file_list:
            item = dict(item)
            hit = detected.get(item.get("fileObjectKey"))
            if hit is not None:
                item["isOcrFile"] = 1 if hit["isOcr"] == 1 else 0
            elif is_ocr and "isOcrFile" not in item:
                item["isOcrFile"] = 1
            files.append(item)

        payload = {
            "fileList": files,
            "sourceLanguage": source_language,
            "targetLanguage": target_language,
            "model": model,
            "isOcr": is_ocr,
            "isMath": 0,
            "isFlow": 0,
        }
        # 术语表：上游 BatchSubmitTranslateRequest 早就有这个字段，提交时随记录落库，
        # 跑任务时把词条拉出来拼成 {原词: 译词} 交给引擎。空值等同于「不用术语表」，
        # 所以只在真有 id 时才带——带个空串只是白占一个键。
        if terminology_collection_id and terminology_collection_id.strip():
            payload["terminologyCollectionId"] = terminology_collection_id.strip()
        
        result = {}
        for attempt in range(4):
            response = await self.client.post(
                f"{DOC_PREFIX}/batchSubmitTranslateTask", json=payload
            )
            response.raise_for_status()
            result = response.json()
            if result.get("code") not in TRANSIENT_CODES:
                break
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
        
        # 这些错误是上传侧的，重试提交没用，必须重新走预签名+上传
        if result.get("code") in REUPLOAD_CODES:
            # msg 交给出口的 annotate_error 统一翻（那边是九种语言），这里只补
            # 本路径特有的细节——同 31004，用 diagnosis 装，别去抢 msg。
            result["diagnosis"] = (
                f"{REUPLOAD_CODES[result['code']]}。上游原话：{result.get('msg')}。"
                "请重新调 upload_document 取预签名地址并重新上传，再用新的 objectKey "
                "提交，重试本工具没有用。"
            )
            return annotate_error(result)

        # 重试后仍失败：明确告知文件无需重传，避免调用方回头做上传
        if result.get("code") in TRANSIENT_CODES:
            result["msg"] = (
                t("error.biz.transient", code=result.get("code"))
                + "（已自动重试 4 次）——文件已上传成功，fileObjectKey 仍然有效，"
                  "请稍后用相同参数重试 translate_document，不要重新上传文件。"
            )
        
        # 上游不返回订单号，提交后回查一次，避免调用方去翻列表
        if result.get("code") == "200":
            names = {f.get("fileName") for f in file_list}
            try:
                recent = await self.search_translate_file_page(1, max(len(file_list), 5))
                # 记录按新到旧排列，同名文件只取最新的一条
                orders, seen = [], set()
                for r in recent.get("data", {}).get("records", []):
                    name = r.get("sourceFileName")
                    if name in names and name not in seen:
                        seen.add(name)
                        orders.append({
                            "translateOrderNo": r["translateOrderNo"],
                            "fileName": name,
                            "status": r["status"],
                        })
                if orders:
                    result.setdefault("data", {})["orders"] = orders
                    result["msg"] = (
                        "任务已提交。请用返回的 orders[].translateOrderNo "
                        "调用 wait_for_translation 等待完成。"
                    )
            except Exception:
                pass  # 回查失败不影响提交结果

        if ocr_note and str(result.get("code")) == "200":
            result["msg"] = ocr_note + (result.get("msg") or "")
            if detected:
                result["ocrDetection"] = list(detected.values())

        return result
    
    async def search_translate_file_by_batch_no(self, batch_no: str) -> dict:
        """通过批次号查询翻译任务"""
        response = await self.client.post(
            f"{DOC_PREFIX}/searchTranslateFileByBatchNo",
            json={"batchNo": batch_no}
        )
        response.raise_for_status()
        return response.json()
    
    async def search_translate_file_page(
        self,
        page_num: int = 1,
        page_size: int = 10,
        status: Optional[int] = None,
    ) -> dict:
        """查询翻译任务列表"""
        payload = {"pageNum": page_num, "pageSize": page_size}
        if status is not None:
            payload["status"] = status
        response = await self.client.post(
            f"{DOC_PREFIX}/searchTranslateFilePage",
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
    async def get_translate_file_detail(self, order_no: str) -> dict:
        """查询翻译任务详情"""
        response = await self.client.post(
            f"{DOC_PREFIX}/getTranslateFileDetail",
            json={"translateOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
    async def get_translate_s3_download_url(
        self,
        order_no: str,
        url_type: int = 2,
        is_watermark: int = 0,
    ) -> dict:
        """获取翻译文件下载地址

        url_type: 1=原文, 2=纯译文, 3=横向对照, 4=纵向对照。
        默认 2——调用方要的通常是译文，取 1 会拿到原文。

        is_watermark: 0=无水印（默认）, 1=带水印。不传这个参数就是走上游默认值，
        很可能拿到带水印的文件；能不能要无水印取决于账号权限，没权限时上游会回
        [ERROR] 事件，这时改传 1。

        返回的 url 走 CloudFront，url2 走国内中转线路（部分存储类型为 null）。
        """
        response = await self.client.post(
            f"{DOC_PREFIX}/getTranslateS3DownloadUrl",
            json={
                "translateOrderNo": order_no,
                "urlType": url_type,
                "isWatermark": is_watermark,
            }
        )
        response.raise_for_status()

        # SSE 响应：event:[PROCESS] / [DONE] / [ERROR]，data 紧跟在 event 之后。
        # 必须跟着 event 走——[ERROR] 的 data 里是 message，当成结果吞掉的话，
        # 真正的失败原因就丢了，只剩下我们自己猜的「不支持该版式」。
        content = response.text
        result, error_msg, event = {}, None, None
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:') and len(line) > 5:
                json_str = line[5:].strip()
                if not json_str:
                    continue
                try:
                    parsed = json.loads(json_str)
                except ValueError:
                    continue
                if event == '[ERROR]':
                    error_msg = (parsed.get("message") if isinstance(parsed, dict) else None) or json_str
                elif isinstance(parsed, dict):
                    result = parsed

        variant = URL_TYPE_LABELS.get(url_type, f"未知版式({url_type})")

        if error_msg:
            return {
                "code": "500",
                "urlType": url_type,
                "variant": variant,
                "msg": (
                    f"上游生成「{variant}」下载地址失败：{error_msg}。"
                    + ("无水印文件需要相应账号权限，可改传 is_watermark=1 重试。"
                       if is_watermark == 0 else "")
                ),
            }

        if not result:
            return {"code": "500", "msg": "未获取到下载链接", "raw": content[:500]}

        result["urlType"] = url_type
        result["variant"] = variant

        if not result.get("url"):
            # 该文件类型不支持这个版式时，上游只回 "Failed to generate file."
            support = URL_TYPE_SUPPORT.get(url_type)
            result["code"] = "400"
            result["msg"] = (
                f"该文件不支持「{variant}」版式"
                + (f"（{support} 支持）。" if support else "。")
                + "请改用 url_type=2 取纯译文，或 url_type=1 取原文。"
            )
            return result

        result["watermark"] = bool(is_watermark)
        result["expiresAt"] = _expires_at(result.get("url"))
        result["lineNote"] = (
            ("这是带水印的版本。" if is_watermark else "这是无水印版本。")
            + "url 走 CloudFront；url2 为国内中转线路（部分存储类型为 null），"
            "海外线路不通时改用它。" + _URL_VERBATIM_NOTE
            + _expiry_note(result.get("url"))
        )
        return result
    
    # ============ 视频翻译 ============
    
    async def video_batch_presigned_upload_url(self, file_name_list: list[str]) -> dict:
        """批量获取视频预签名上传 URL"""
        result = await self._post(f"{VIDEO_PREFIX}/batchPresignedUploadUrl", {"fileNameList": file_name_list})

        _attach_upload_command(result, "video")
        return result
    
    async def submit_video_translate(
        self,
        source_language: str,
        target_language: str,
        source_file_object_key: str,
        video_file_name: str,
        video_task_param: dict,
    ) -> dict:
        """提交视频翻译任务"""
        invalid = _check_video_task(video_task_param)
        if invalid:
            return {"code": "400", "msg": invalid, "data": {"videoTaskParam": video_task_param}}
        result = await self._post(
            f"{VIDEO_PREFIX}/submitVideoTranslate",
            {
                "sourceLanguage": source_language,
                "targetLanguage": target_language,
                "sourceFileObjectKey": source_file_object_key,
                "videoFileName": video_file_name,
                "videoTaskParam": video_task_param,
            },
        )
        if result.get("code") in ("200", 200):
            _record_video_submit(source_file_object_key, target_language, result.get("data"))
            # 之前这里裸返上游 JSON，一句话都没有。上游其实给了真实时长和真实扣费，
            # 但都埋在几十个字段中间，调用方于是接着用自己之前猜的那个时长往下说
            # （把 21 秒说成 5 分 51 秒、4 额度说成 47 额度、还把本次消耗读成余额）。
            # 数字放进 msg 它就照抄，这一条已经在 outputNote 上验证过了。
            data = result.get("data") or {}
            free = data.get("freeTranslateQuota") or 0
            wallet = data.get("walletTranslateQuota") or 0
            duration_ms = data.get("videoDuration")
            result["msg"] = (
                f"已提交，订单号 {data.get('videoTranslateOrderNo')}。"
                + (f"服务端读到的真实时长是 {_format_duration((duration_ms or 0) / 1000)}"
                   if duration_ms else "服务端未返回时长")
                + f"，本次实扣 {free + wallet} 额度"
                + (f"（免费 {free} + 钱包 {wallet}）" if wallet else "（免费额度）")
                + "。这两个数以本条为准：之前试算用的时长如果是估的，别再拿那个数字往下说；"
                "这里的额度是本次消耗，不是账户余额，本接口不返回余额，不要当成余额报给用户。"
                "接着用 wait_for_video_translation 跟进。"
            )
            # 回执里刚提交的任务还什么都没有：十几个 null 的 objectKey/地址字段、
            # 一串 id、外加一份 paramJson。要点全在 msg 里了，记录只留能对上号的那几项。
            result["data"] = slim_video_record(_annotate_video_status(data))
        return result
    
    async def video_translate_quota_calculate(
        self,
        video_duration: float,
        voice_role: str = "No",
        subtitle_type: int = 1,
    ) -> dict:
        """计算视频翻译配额

        video_duration 的单位是**毫秒**（文档 3.2）。传成秒会让试算结果低到
        离谱——按 30 秒一个计费单位向上取整，60 秒的视频传成 60 会被当作
        0.06 秒，照样只算一个单位，看不出错；但 10 分钟传成 600 就会从 80
        额度变成 4 额度。
        """
        result = await self._post(
            f"{VIDEO_PREFIX}/videoTranslateQuotaCalculate",
            {
                "videoDuration": video_duration,
                "voiceRole": voice_role,
                "subtitleType": subtitle_type,
            },
        )
        if video_duration and video_duration < 1000:
            result["unitWarning"] = (
                f"传入的 video_duration={video_duration:g} 不足 1000 毫秒（1 秒）。"
                "这个参数的单位是毫秒，确认一下是不是把秒当毫秒传了——"
                "传错会让试算额度远低于实际扣费。"
            )
        # 试算的输入是调用方给的，服务端无从核实。实测调用方会按文件大小猜时长
        # （4.8MB 猜成「5-6 分钟」，实际 21 秒），然后把差了十倍的预算报给用户。
        # 算式和这个前提都得写在返回里，不然它连乘法都会自己编一个。
        data = result.get("data") or {}
        units = data.get("videoDuration")
        quota = data.get("translateQuota")
        if units is not None and quota is not None:
            result["quotaNote"] = (
                f"按你传入的 {video_duration / 1000:g} 秒算：不足 30 秒的按 30 秒计，"
                f"共 {units} 个计费单位，配音={voice_role}、字幕={subtitle_type} 下"
                f"合计 {quota} 额度。"
                "注意这个时长是你传进来的，服务端核实不了——必须来自文件的真实时长"
                "（ffprobe 或媒体信息），按文件大小推码率猜出来的数在数量级上就不成立，"
                "拿它报给用户等于报了个假预算。"
            )
            # 提交时要回查这一条：只有和本次提交参数一致的试算才放行，
            # 免得「按不配音报预算、按 clone 配音扣费」。
            record_quota_calc(video_duration, voice_role, subtitle_type, data)
        return result
    
    async def video_translate_quota_matrix(self, video_duration: float, combos=VIDEO_COMBOS) -> list:
        """把每个「配音 × 字幕」组合的额度都向上游问一遍。

        试算免费，所以宁可多问几次，也不要让用户在没有数字的选项里挑。顺带把每
        一格都记进试算台账——用户挑中哪一格，提交时的参数校验就已经有账可查。
        某一格问不到就按计费规则推一个，并在 source 上标成 rule。
        """
        results = await asyncio.gather(
            *[self.video_translate_quota_calculate(video_duration, v, s) for v, s in combos],
            return_exceptions=True,
        )
        rows = []
        for (voice_role, subtitle_type), result in zip(combos, results):
            quota = None
            if isinstance(result, dict):
                quota = (result.get("data") or {}).get("translateQuota")
            source = "upstream"
            if quota is None:
                quota = quota_by_rule(video_duration, voice_role, subtitle_type)
                source = "rule"
                # 上游没答的那一格也要落账，否则用户挑了它反而提交不了
                record_quota_calc(video_duration, voice_role, subtitle_type, {
                    "translateQuota": quota,
                    "videoDuration": max(1, math.ceil((video_duration or 0) / VIDEO_QUOTA_UNIT_MS)),
                })
            rows.append({
                "voiceRole": voice_role,
                "subtitleType": subtitle_type,
                "label": video_combo_label(voice_role, subtitle_type),
                "quota": quota,
                "source": source,
            })
        return rows

    async def search_video_translate_page(
        self,
        page_num: int = 1,
        page_size: int = 10,
        status: Optional[int] = None,
    ) -> dict:
        """查询视频翻译任务列表"""
        params = {"pageNum": page_num, "pageSize": page_size}
        if status is not None:
            params["status"] = status
        result = await self._post(f"{VIDEO_PREFIX}/searchVideoTranslatePage", params)
        page = result.get("data") or {}
        records = page.get("records") or []
        # 列表是拿来浏览的，不是拿来交付的：每条记录原来挂着四条几百字符的签名
        # 地址，十条就上万字符，而其中九条根本用不上。要下载再按单号取。
        if isinstance(page, dict) and records:
            page["records"] = [
                slim_video_record(_annotate_video_status(record)) for record in records
            ]
        if result.get("code") == "200":
            result["statusNote"] = (
                "视频任务状态：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消"
                "——注意 2 就是完成，和文档翻译的状态码不是一套。"
                "只保留最近 15 天的记录。列表不带下载地址：要哪一单的产出，"
                "就拿它的 videoTranslateOrderNo 调 get_video_translation_status 取链接，"
                "那边是现签发的，不用担心列表里的地址过期。"
            )
        return result
    
    async def get_video_translate_detail(self, order_no: str) -> dict:
        """查询视频翻译详情（上游是 SSE 流，取第一帧就返回）"""
        result = await self._post_sse(
            f"{VIDEO_PREFIX}/getVideoTranslateDetail",
            {"videoTranslateOrderNo": order_no},
        )
        _annotate_video_status(result.get("data") or {})
        return result
    
    async def cancel_video_translate(self, order_no: str) -> dict:
        """取消视频翻译任务"""
        result = await self._post(f"{VIDEO_PREFIX}/cancelVideoTranslateHistory", {"videoTranslateOrderNo": order_no})
        return result
    
    async def get_video_subtitles(self, order_no: str) -> dict:
        """获取视频字幕"""
        return await self._post(
            f"{VIDEO_PREFIX}/getVideoTranslateSubtitles",
            {"videoTranslateOrderNo": order_no},
        )
    
    async def submit_video_rewrite(
        self,
        order_no: str,
        source_subtitles_txt: str,
        target_subtitles_txt: str,
        video_task_param: Optional[dict] = None,
    ) -> dict:
        """提交视频字幕改写任务"""
        payload = {
            "videoTranslateOrderNo": order_no,
            "sourceSubtitlesTxt": source_subtitles_txt,
            "targetSubtitlesTxt": target_subtitles_txt,
        }
        if video_task_param:
            payload["videoTaskParam"] = video_task_param
        result = await self._post(f"{VIDEO_PREFIX}/submitVideoRewrite", payload)
        return result
    
    async def get_video_rewrite_detail(self, order_no: str) -> dict:
        """查询视频字幕改写详情（上游是 SSE 流，取第一帧就返回）"""
        result = await self._post_sse(
            f"{VIDEO_PREFIX}/getVideoTranslateRewriteDetail",
            {"videoTranslateRewriteOrderNo": order_no},
        )
        _annotate_video_status(result.get("data") or {})
        return result
    
    async def video_rewrite_quota_calculate(self, order_no: str) -> dict:
        """计算视频字幕改写配额"""
        return await self._post(
            f"{VIDEO_PREFIX}/videoTranslateRewriteQuotaCalculate",
            {"videoTranslateRewriteOrderNo": order_no},
        )

    async def wait_for_video_translation(self, order_no: str, timeout=None, on_progress=None) -> dict:
        """等待视频翻译任务完成

        和 wait_for_translation 同一套路子：进度一变就返回，否则最多等 timeout 秒，
        并按订单号记跨调用的累计等待。
        视频任务通常要几分钟，文档建议 10-30 秒查一次，所以退避比文档翻译宽。
        超时不算失败。
        """
        import asyncio

        record = _WAITS.setdefault(
            order_no, {"totalElapsed": 0.0, "calls": 0, "lastKey": None, "silent": 0}
        )
        record["calls"] += 1
        _prune_waits()
        if timeout is None:
            timeout = _auto_timeout(record)

        start_time = asyncio.get_event_loop().time()
        interval = _VIDEO_POLL_SECONDS  # 逐步放宽到 15 秒，文档建议 10-30 秒一次
        snapshot = {}
        # 上一次返回给用户的那一行长什么样。判「变没变」要跟它比，而不是跟本次
        # 循环里刚立的基线比——否则首次调用也会被判成「和上次一样」，用户第一眼
        # 看到的就是一句「仍在…」的省略话。
        entry_key = record["lastKey"]

        def elapsed_now() -> float:
            return asyncio.get_event_loop().time() - start_time

        def live_percent() -> float:
            try:
                return float(str(snapshot.get("progress", "0")).rstrip("%"))
            except ValueError:
                return 0.0

        def status_line(total_seconds: float, polls: bool = False) -> str:
            """要念给用户听的那一行，整行都走消息表。累计时长从 record 起算，
            跨调用接着数，不然每调一次计时都归零。"""
            parts = [snapshot.get("statusText") or t("progress.querying")]
            if snapshot.get("progress"):
                parts.append(snapshot["progress"])
            if snapshot.get("queueRank"):
                parts.append(t("progress.queue", rank=snapshot["queueRank"],
                               total=snapshot["queueTotal"]))
            duration = _format_duration(total_seconds)
            parts.append(
                t("progress.polls", calls=record["calls"], duration=duration)
                if polls else t("progress.waited", duration=duration)
            )
            return " · ".join(parts)

        def live_line() -> str:
            return status_line(record["totalElapsed"] + elapsed_now())

        async def heartbeat(seconds: float) -> None:
            """轮询间隔照旧（别去多打上游），但计时每 _HEARTBEAT_SECONDS 秒走一格"""
            deadline = elapsed_now() + seconds
            while True:
                left = deadline - elapsed_now()
                if left <= 0 or elapsed_now() > timeout:
                    return
                await asyncio.sleep(min(_HEARTBEAT_SECONDS, left))
                await _emit_progress(on_progress, live_percent(), live_line())

        def pending(elapsed: float, changed: bool) -> dict:
            changed = changed or _wait_key(snapshot) != entry_key
            # 连着几轮没动就让下一次调用等得更久，见 _auto_timeout
            record["silent"] = 0 if changed else record.get("silent", 0) + 1
            record["totalElapsed"] += elapsed
            record["lastKey"] = _wait_key(snapshot)
            total = record["totalElapsed"]
            # 一轮进度只留一句 msg。以前 data 里把 statusText/progress/排队位置
            # 又抄了一遍，每次返回两份同样的话，等一个视频要轮询十几次，光这些
            # 重复就把对话刷满了。机器要用的只有 finished。
            return {
                "code": "202",
                "msg": status_line(total, polls=True) + "。",
                "agentNote": (
                    "把 msg 那行原样告诉用户，然后再次调用本工具继续等待。"
                    if changed else
                    # 排队时进度能十几分钟一动不动。之前每轮都要求复述，屏幕上
                    # 就是一屏一模一样的进度加一堆过场文字——轮询本身不该产生
                    # 输出。没有新东西可说时就什么都别说，直接接着等。
                    "进度和上次完全一样，没有新东西可告诉用户："
                    "**不要输出任何文字**（连「继续等待」「仍在处理」这类过场话也不要），"
                    "直接再次调用本工具继续等待。等到进度真的变了或任务结束，再开口。"
                ),
                "data": {"orderNo": order_no, "finished": False},
            }

        await _emit_progress(on_progress, 0.0, live_line())

        while True:
            elapsed = elapsed_now()
            if elapsed > timeout:
                return pending(elapsed, changed=False)

            detail = await self.get_video_translate_detail(order_no)
            code = str(detail.get("code") or "")
            if code != "200":
                if code in KEY_ERROR_RETRYABLE:
                    await heartbeat(max(interval, _RATE_LIMIT_BACKOFF))
                    interval = min(interval + 2, 15)
                    continue
                _WAITS.pop(order_no, None)
                return detail

            data = detail.get("data") or {}
            status = data.get("status")
            total = record["totalElapsed"] + elapsed

            if status == VIDEO_STATUS_DONE:
                _WAITS.pop(order_no, None)
                urls, rewrite = video_products(data)
                target = urls.get("targetFileUrl")
                note = data.get("outputNote") or ""
                # 产出说明必须挤进 msg。实测三条几百字符的签名链接会把 data 尾部
                # 顶出客户端的显示截断线，调用方压根读不到 outputNote，于是照着
                # 十几轮之前自己传过的参数瞎编——voiceRole=No 也能说成「英文配音」。
                # msg 是每一轮都被完整转述的字段，是唯一放得住这句话的地方。
                # 其余字段能省则省：几百字符的签名链接后面再挂一段说明，
                # 长到会把前面的产出说明顶出显示区。
                result = {
                    "code": "200",
                    "msg": (
                        f"视频翻译完成（累计等待 {_format_duration(total)}）。"
                        + (f"产出：{note}。" if note else "")
                    ),
                    "agentNote": (
                        ("msg 里那句产出说明请原样照抄，不要自己推断有没有配音。" if note else "")
                        + "请按 downloadNote 把签名链接原样完整交给用户。"
                    ),
                    "data": {
                        "orderNo": order_no,
                        "finished": True,
                        "outputNote": note,
                        **({"rewriteOrderNo": rewrite["rewriteOrderNo"]} if rewrite else {}),
                        "videoFileName": data.get("videoFileName"),
                        "targetLanguage": data.get("targetLanguage"),
                        "videoDurationMs": data.get("videoDurationMs"),
                        "videoDurationText": data.get("videoDurationText"),
                        "totalWaited": _format_duration(total),
                        "usedFreeQuota": data.get("freeTranslateQuota"),
                        "usedWalletQuota": data.get("walletTranslateQuota"),
                    },
                }
                if target:
                    # 只给要交付的那两条；原片和原文字幕真要用再去
                    # get_video_translation_status 取，没必要每单都甩四条签名地址。
                    result["data"].update({
                        "expiresAt": _expires_at(target),
                        "downloadNote": (
                            t("download.video")
                            + _URL_VERBATIM_NOTE
                            + _expiry_note(target)
                        ),
                        # 链接垫底
                        "translatedVideoUrl": target,
                        "targetSubtitlesUrl": urls.get("targetSubtitlesUrl"),
                    })
                return result

            if status in VIDEO_STATUS_TERMINAL:
                # 3 失败 / 4 已取消
                _WAITS.pop(order_no, None)
                # 上游经常不给 errorMessage。之前这里退化成状态文本「失败」，等于
                # 什么都没说，调用方就自己编了个原因（「大概是没有清晰的人声」）
                # 并据此改参数重提。原因不明就明说不明，别给它留想象空间；
                # step 是真有的信息，起码能说清楚死在哪一步。
                reason = data.get("errorMessage") or ""
                step_text = data.get("stepText") or (
                    video_step_text(data["step"]) if data.get("step") is not None else ""
                )
                return {
                    "code": "500",
                    "msg": (
                        f"视频任务已终止：{video_status_text(status)}"
                        + (f"，停在「{step_text}」这一步" if step_text else "")
                        + (f"，上游给的原因：{reason}" if reason
                           else f"，上游没有给出失败原因：{t('reason.unknown')}")
                        + f"。累计等待 {_format_duration(total)}。"
                    ),
                    "agentNote": (
                        ("" if reason else
                         "上游没给原因就照 msg 里那句如实说，不要自己推测是音频、"
                         "语言还是格式的问题。")
                        + "重新提交是一单新任务、会再扣一次费，所以必须先把 msg 里这些"
                        "告诉用户、问清楚要不要重做；同意后再带 retry_of_order_no 和 "
                        "retry_confirmed=true 调 translate_video，不带这两个参数会被直接拒绝。"
                    ),
                    "data": {
                        "orderNo": order_no,
                        "finished": True,
                        "failed": True,
                        "status": status,
                        "statusText": video_status_text(status),
                        "failedStep": data.get("step"),
                        "failedStepText": step_text,
                        "reasonKnown": bool(reason),
                        "reason": reason or "上游未提供失败原因，不要推测",
                        "retryOfOrderNo": order_no,
                        "totalElapsedSeconds": round(total),
                    },
                }

            # 0 未开始 / 1 进行中
            status_text = video_status_text(status)
            if data.get("stepText"):
                status_text = t("status.with_step", status=status_text, step=data["stepText"])
            snapshot = {
                "statusText": status_text,
                "videoFileName": data.get("videoFileName"),
            }
            progress_info = data.get("progress") or {}
            if progress_info:
                snapshot["progress"] = f"{float(progress_info.get('progress') or 0):.1f}%"
                snapshot["queueRank"] = progress_info.get("taskRanking")
                snapshot["queueTotal"] = progress_info.get("totalTask")
                snapshot["predictWaitSeconds"] = progress_info.get("predictWaitTime")

            await _emit_progress(on_progress, live_percent(), live_line())

            key = _wait_key(snapshot)
            if record["lastKey"] is None:
                record["lastKey"] = key
            elif key != record["lastKey"] and elapsed >= _WAIT_MIN_SECONDS:
                return pending(elapsed, changed=True)

            await heartbeat(interval)
            interval = min(interval + 2, 15)


    # ============ 结果下载 ============

