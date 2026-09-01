"""API 客户端"""

import json
import re
import sys
import time
import httpx
from typing import Optional

# 测试环境
API_BASE_URL = "http://internal-test-host:6101"
# 生产环境（取消注释切换）
# API_BASE_URL = "https://belindoc.com/api"
DOC_PREFIX = "/external/translate"
# 上游瞬时错误码，重试即可（见 docs/document-translation-api-guide.md 常见错误）：
#   600   System is busy
#   30010 并发任务超过限制，等在跑的任务完成后重试
#   30012 任务重复提交，稍后重试
TRANSIENT_CODES = {"600", "30010", "30012"}
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
URL_TYPE_FIELDS = {
    2: ("targetFileUrl", "targetFileUrl2"),
    3: ("xComparisonS3Url", None),
    4: ("yComparisonS3Url", None),
}
VIDEO_PREFIX = "/external/videoTranslate"
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
VIDEO_SUBTITLE_TYPE = {
    0: "不嵌入字幕",
    1: "翻译字幕",
    2: "原始字幕",
    3: "翻译+原始字幕",
}
# 视频时长按 30 秒一个计费单位向上取整，每单位 4 额度
VIDEO_QUOTA_UNIT_MS = 30_000


def _not_found(path: str) -> dict:
    """404 的可读说明。上游对未部署的接口直接回 404，httpx 抛的是一句英文
    HTTPStatusError，调用方会当成网络故障反复重试、或告诉用户「服务器维护中」。"""
    return {
        "code": "404",
        "msg": (
            f"接口 {path} 在当前服务地址（{API_BASE_URL}）上不存在。"
            "这不是网络故障，也不是服务器维护，重试没有意义："
            "该功能在这个环境未部署，或路径已变更。请把这句话告诉用户，"
            "不要反复重试，也不要改用其他工具凑合。"
        ),
    }


def _annotate_video_status(record: dict) -> dict:
    """给视频记录补上状态中文说明，别让调用方拿文档翻译那套状态码去套"""
    if not isinstance(record, dict):
        return record
    if "status" in record:
        record["statusText"] = VIDEO_STATUS_TEXT.get(record["status"], f"未知({record['status']})")
    if record.get("step") is not None:
        record["stepText"] = VIDEO_STEP_TEXT.get(record["step"], f"未知({record['step']})")
    if record.get("stepStatus") is not None:
        record["stepStatusText"] = VIDEO_STEP_STATUS_TEXT.get(
            record["stepStatus"], f"未知({record['stepStatus']})"
        )
    return record


def _available_variants(data: dict) -> dict:
    """详情里实际存在的版式 -> 地址。比按文件类型猜准，因为这是上游真给了的。"""
    found = {}
    for url_type, (field, _) in URL_TYPE_FIELDS.items():
        if data.get(field):
            found[url_type] = data[field]
    return found

# 下载链接是带签名的：CloudFront 靠 Signature/Key-Pair-Id，国内线路靠 sign，
# 全挂在问号后面。实测把 ? 之后截掉会直接 403 MissingKey。转述时顺手把长链接
# 截短是很自然的动作，所以每条返回都得把这句话摆在明面上。
_URL_VERBATIM_NOTE = (
    "链接请原样完整交给用户：问号后面的签名参数（Signature、Key-Pair-Id、"
    "expires、sign 等）一个字符都不能删、不能截断、不能改写，也不要为了好看"
    "缩短它，否则会 403 MissingKey。"
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


def _expiry_note(url: str) -> str:
    """把真实有效期写出来。只说一句「有有效期」的话，调用方会自己编一个
    「约 1 小时」——实测只有 11 分钟，用户照着那个数去下载就已经过期了。"""
    remaining = _url_expiry(url)
    if remaining is None:
        return "链接有有效期，过期后重新调用本工具取新的。"
    if remaining <= 0:
        return "链接已过期，请重新调用本工具取新的。"
    return (
        f"链接还有 {_format_duration(remaining)}过期，请提醒用户尽快下载"
        "（有效期就是这个数，不要按经验说成一小时）；过期后重新调用本工具取新的。"
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


def _attach_upload_command(result: dict) -> None:
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
        item["uploadCommandBackground"] = f"( {curl} 2>&1 | tr '\\r' '\\n' > {log_path} ) &"
        item["progressLogPath"] = log_path
        item["uploadNote"] = (
            "上传前先 ls -lh 看一眼文件大小，把「多大、大概要传多久」先告诉用户——"
            "多数文件几秒就传完了，用户真正难受的是不知道要等多久。"
            "然后原样执行 uploadCommand，只替换文件路径；"
            "Content-Disposition 一个字符都不能改，否则 S3 会报 SignatureDoesNotMatch。"
            "命令末尾的 `2>&1 | tr` 是把 curl 的进度条摊成逐行输出，删掉就再也看不到进度了。"
            "文件很大（超过 100MB）时改用 uploadCommandBackground 放后台传，再反复执行 "
            f"`tail -n 3 {log_path}` 查看进度并转述给用户。"
            "输出的「上传结果 HTTP 200」才算成功，S3 成功时 body 为空属正常。"
            "上传成功后用本条的 objectKey 作为 fileObjectKey 调 translate_document。"
        )


# ============ 上传进度 ============
# MCP 的 tools/call 是一问一答，上传途中没有插话的通道；HTTP 模式下还会给每个请求
# 新建一个 TranslationClient。所以进度只能记在模块级，由 get_upload_status 轮询取回，
# 和 wait_for_translation 是同一套「发起 + 反复查」的路子。
_UPLOADS: dict[str, dict] = {}
_UPLOAD_KEEP = 50
# 还没有实测速度时用来估算耗时的保守带宽，只为给用户一个量级
_ASSUMED_UPLOAD_BPS = 2 * 1024 * 1024


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{max(1, seconds)} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


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


def _put_file(state: dict, presigned_url: str, encode_file_name: str) -> None:
    """同步上传，由 asyncio.to_thread 丢到线程里跑，别占着事件循环

    走文件流而不是一次 read() 到内存：几百 MB 的视频不该整个装进 RSS。
    """
    try:
        with open(state["filePath"], "rb") as fh:
            # 上传耗时完全由文件大小决定，读写不设上限，只卡建连
            with httpx.Client(timeout=httpx.Timeout(None, connect=30.0)) as c:
                resp = c.put(
                    presigned_url,
                    content=_ProgressReader(fh, state),
                    headers={
                        "Content-Disposition": f"attachment; filename*=UTF-8''{encode_file_name}"
                    },
                )
        if resp.status_code == 200:
            state["uploadedBytes"] = state["totalBytes"]
            state["status"] = "success"
        else:
            state["status"] = "failed"
            state["error"] = f"S3 返回 HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        state["status"] = "failed"
        state["error"] = f"{type(e).__name__}: {e}"
    finally:
        state["finishedAt"] = time.monotonic()


def _prune_uploads() -> None:
    """只留最近的记录，别让长跑的服务把内存攒满。dict 有序，先删最老的已完成项。"""
    if len(_UPLOADS) <= _UPLOAD_KEEP:
        return
    done = [k for k, v in _UPLOADS.items() if v["status"] != "uploading"]
    for k in done[: len(_UPLOADS) - _UPLOAD_KEEP]:
        _UPLOADS.pop(k, None)


def _upload_snapshot(upload_id: str) -> dict:
    """把某次上传的当前状态整理成可以直接念给用户听的样子"""
    state = _UPLOADS.get(upload_id)
    if not state:
        return {
            "code": "404",
            "msg": (
                f"没有 uploadId={upload_id} 的上传记录（服务重启或记录过期后会丢失），"
                "请重新调 upload_file。"
            ),
        }

    total = state["totalBytes"]
    sent = min(state["uploadedBytes"], total)
    elapsed = (state.get("finishedAt") or time.monotonic()) - state["startedAt"]
    speed = sent / elapsed if elapsed > 0 else 0
    percent = (sent / total * 100) if total else 100.0

    snapshot = {
        "uploadId": upload_id,
        "status": state["status"],
        "fileName": state["fileName"],
        "objectKey": state["objectKey"],
        "fileSize": total,
        "fileSizeHuman": _human_size(total),
        "progress": f"{percent:.1f}%",
        "uploadedHuman": _human_size(sent),
        "elapsedSeconds": round(elapsed, 1),
        "speedHuman": f"{_human_size(speed)}/s" if speed else "计算中",
    }

    if state["status"] == "uploading":
        remaining = (total - sent) / (speed or _ASSUMED_UPLOAD_BPS)
        snapshot["etaHuman"] = _human_duration(remaining)
        msg = (
            f"上传中 {percent:.1f}%（{_human_size(sent)}/{_human_size(total)}，"
            f"剩余{_human_duration(remaining)}）。请把这个进度转述给用户，再调一次 "
            "get_upload_status 继续看；上传在后台跑，不会因此中断。"
        )
        return {"code": "202", "data": snapshot, "msg": msg}

    if state["status"] == "success":
        msg = (
            f"上传成功（{_human_size(total)}，耗时 {elapsed:.1f} 秒）。"
            "用 objectKey 作为 fileObjectKey 调 translate_document。"
        )
        return {"code": "200", "data": snapshot, "msg": msg}

    snapshot["error"] = state.get("error", "")
    return {"code": "500", "data": snapshot, "msg": f"上传失败: {snapshot['error']}"}


class TranslationClient:
    """翻译 API 客户端"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
                # 不传这个头，上游的错误信息默认回英文
                "language": "zh-CN",
            },
            timeout=httpx.Timeout(30.0)  # 30秒超时
        )
    
    async def close(self):
        await self.client.aclose()
    
    async def _post(self, path: str, payload: dict) -> dict:
        """POST 并把 404 翻译成人话

        上游对未部署的接口直接回 404，httpx 抛出来的是一句英文 HTTPStatusError，
        调用方会当成网络故障，反复重试或者告诉用户「服务器维护中」——实测视频
        那批接口就是这样被误判的。实际是该接口在当前服务地址上不存在，重试无用。
        """
        response = await self.client.post(path, json=payload)
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
        """获取翻译模型列表，返回当前用户可用的模型名称列表"""
        response = await self.client.post(f"{DOC_PREFIX}/getModelList", json={})
        response.raise_for_status()
        result = response.json()
        
        # 只返回可用的模型名称列表
        if result.get("code") == "200":
            models = result.get("data", [])
            # vipType: -1 或 0 表示免费可用，其他需要对应VIP等级
            available_models = [m["version"] for m in models if m.get("vipType", 0) <= 0]
            return {"code": "200", "data": available_models, "msg": "请让用户从以下可用模型中选择一个"}
        return result
    
    # ============ 配额相关 ============
    
    async def get_quota(self) -> dict:
        """查询账户配额"""
        # 注意：此接口可能不存在，需要后端确认
        response = await self.client.get(f"{DOC_PREFIX}/getQuota")
        response.raise_for_status()
        return response.json()
    
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
    
    async def upload_file(self, file_path: str, wait: int = 8) -> dict:
        """上传本地文件到翻译平台

        小文件几秒就传完，那就在这一次调用里等掉；超过 wait 秒还没完成就带着当前
        进度返回，让调用方转述给用户后再用 get_upload_status 继续查——和
        wait_for_translation 一个路子，别让用户对着静默的界面干等。
        """
        import asyncio
        import os
        import uuid

        # 获取文件名
        file_name = os.path.basename(file_path)

        # 检查文件是否存在
        # 注意：读文件的是服务端进程。客户端与服务端不在同一台机器时，
        # 客户端的本地路径在这里必然不存在，且换路径重试没有意义。
        if not os.path.exists(file_path):
            import socket
            return {
                "code": "400",
                "msg": (
                    f"服务端（主机 {socket.gethostname()}）找不到路径 {file_path}。"
                    "如果 MCP 服务部署在另一台机器上，本工具读不到你本机的文件，"
                    "换路径或复制到 /tmp 都无效。请改用 upload_document 获取预签名链接，"
                    "然后按返回的 uploadCommand 原样执行上传（该命令的请求头必须与签名一致，"
                    "不要改写）。"
                ),
                "data": {"serverHost": socket.gethostname(), "triedPath": file_path},
            }

        # 获取预签名URL
        upload_info = await self.doc_batch_presigned_upload_url([file_name])
        if upload_info.get("code") != "200":
            return upload_info

        upload_data = upload_info["data"][0]
        upload_id = uuid.uuid4().hex[:12]
        state = {
            "filePath": file_path,
            "fileName": file_name,
            "objectKey": upload_data["objectKey"],
            "totalBytes": os.path.getsize(file_path),
            "uploadedBytes": 0,
            "status": "uploading",
            "startedAt": time.monotonic(),
            "finishedAt": None,
            "error": "",
        }
        _UPLOADS[upload_id] = state
        _prune_uploads()

        # 丢进线程：之前这里是同步的 httpx.put，上传期间整个事件循环冻结，
        # 其他工具的轮询全都停摆。task 存进 state，免得被 GC 提前回收。
        state["_task"] = asyncio.create_task(
            asyncio.to_thread(
                _put_file,
                state,
                upload_data["persignedUploadUrl"],
                upload_data["encodeFileName"],
            )
        )

        return await self._await_upload(upload_id, wait)

    async def get_upload_status(self, upload_id: str, wait: int = 8) -> dict:
        """查询 upload_file 发起的上传进度，未完成时最多等 wait 秒再返回"""
        return await self._await_upload(upload_id, wait)

    @staticmethod
    async def _await_upload(upload_id: str, wait: int) -> dict:
        """等一小会儿再给快照，免得调用方空转着反复查"""
        import asyncio

        state = _UPLOADS.get(upload_id)
        if state:
            # 先让出一次：wait=0 时下面的循环一次都不进，上传线程会连启动的
            # 机会都没有，快照永远停在 0%
            await asyncio.sleep(0)
            deadline = time.monotonic() + max(0, wait)
            while state["status"] == "uploading" and time.monotonic() < deadline:
                await asyncio.sleep(0.3)
        return _upload_snapshot(upload_id)

    async def wait_for_translation(self, order_no: str, timeout: int = 45) -> dict:
        """等待翻译任务完成，自动轮询状态

        上游只在轮询时给出进度，无法主动推送。两个返回时机：进度一变就立刻返回，
        否则最多等 timeout 秒。之前不管上游动没动都要熬满超时，连着调几次拿到的是
        逐字节相同的响应，调用方只好反复复述「还在等」。
        超时不算失败。
        """
        import asyncio

        record = _WAITS.setdefault(
            order_no, {"totalElapsed": 0.0, "calls": 0, "lastKey": None}
        )
        record["calls"] += 1
        _prune_waits()

        start_time = asyncio.get_event_loop().time()
        last_logged = -1
        interval = 2  # 轮询间隔，逐步放宽到 8 秒，避免高频打上游
        snapshot = {}

        def pending(elapsed: float, changed: bool) -> dict:
            """排队/翻译中时的返回体，带上跨调用的累计等待"""
            record["totalElapsed"] += elapsed
            record["lastKey"] = _wait_key(snapshot)
            total = record["totalElapsed"]
            head = (
                f"{snapshot.get('statusText', '处理中')}"
                f"{' ' + snapshot['progress'] if snapshot.get('progress') else ''}"
            )
            queue = (
                f"，排队第 {snapshot['queueRank']}/{snapshot['queueTotal']} 位"
                if snapshot.get("queueRank")
                else ""
            )
            # 从提交算起的时长——用户等的是这个，不是我们从第几次调用开始数的
            since = (
                f"，任务已提交 {snapshot['sinceSubmit']}"
                if snapshot.get("sinceSubmit")
                else ""
            )
            # 不管进度变没变，都要求把当前进度说出来。之前写的是「没变化就不必
            # 复述」，结果调用方在这些回合里什么都不说，界面上只剩一串省略号。
            tail = (
                "。请把上面这行进度原样告诉用户"
                + ("（进度有更新）" if changed else "（进度与上次相同，也照样说，不要沉默或跳过）")
                + "，然后再次调用 wait_for_translation 继续等待。"
            )
            return {
                "code": "202",
                "msg": (
                    f"{head}（本次等待 {_format_duration(elapsed)}，"
                    f"累计已等待 {_format_duration(total)}，第 {record['calls']} 次查询）"
                    f"{queue}{since}{tail}"
                ),
                "data": {
                    "orderNo": order_no,
                    "elapsed": round(elapsed),
                    "totalElapsedSeconds": round(total),
                    "totalWaited": _format_duration(total),
                    "pollCount": record["calls"],
                    "changedSinceLastCall": changed,
                    "finished": False,
                    **snapshot,
                },
            }

        while True:
            # 检查超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return pending(elapsed, changed=False)

            # 查询翻译状态
            try:
                status = await self.get_translate_file_detail(order_no)
                if status.get("code") != "200":
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
                        "msg": (
                            f"任务已被终止：{reason}（累计等待 {_format_duration(total)}）。"
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
                    others = [
                        f"url_type={t} {URL_TYPE_LABELS[t]}"
                        for t in sorted(variants)
                        if t != 2
                    ]
                    payload = {
                        "orderNo": order_no,
                        "finished": True,
                        "fileName": data["sourceFileName"],
                        "sourceLanguage": data["sourceLanguage"],
                        "targetLanguage": data["targetLanguage"],
                        "model": data["model"],
                        "textNumber": data.get("textNumber"),
                        "elapsed": round(elapsed),
                        "totalElapsedSeconds": round(total),
                        "totalWaited": _format_duration(total),
                        "watermark": watermark,
                        "downloadUrl": target_url,
                        "downloadUrlCN": target_url_cn,
                        "downloadNote": (
                            ("这是带水印的版本（账号没有无水印下载权限）。"
                             if watermark else "这是无水印的纯译文。")
                            + "downloadUrlCN 是同一份文件的国内兜底线路"
                            "（只有纯译文有，对照版式没有兜底线路），前者慢或不通时改用后者。"
                            + _URL_VERBATIM_NOTE
                            + _expiry_note(target_url)
                            + (
                                "本文件还可以取这些版式，用 get_document_translation_result："
                                + "、".join(others) + "。"
                                if others
                                else "本文件没有其他可用版式（原文用 url_type=1 取）。"
                            )
                        ),
                    }
                    if others:
                        payload["availableVariants"] = others
                    return {
                        "code": "200",
                        "msg": f"翻译完成（累计等待 {_format_duration(total)}）",
                        "data": payload,
                    }
                elif task_status in TASK_STATUS_PENDING:
                    # 未开始/解析中/翻译中，记录快照供返回时带出
                    status_text = TASK_STATUS_TEXT[task_status]
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

                # 递增退避：2s 起，逐步放宽到 8s 上限
                await asyncio.sleep(interval)
                interval = min(interval + 1, 8)

            except httpx.HTTPError as e:
                # 只有网络/超时这类瞬时故障才值得重试。原来这里 except Exception，
                # 连 TypeError、KeyError 都被吞掉后无限重试，最后报成「还在等待」
                # ——真正的错误彻底看不见。
                print(f"查询出错: {e}，{interval}秒后重试...", file=sys.stderr, flush=True)
                await asyncio.sleep(interval)

    async def batch_submit_translate_task(
        self,
        file_list: list[dict],
        source_language: str,
        target_language: str,
        model: str = "Gemini-2.5-Flash",
        is_ocr: int = 0,
    ) -> dict:
        """批量提交文档翻译任务

        上游偶发返回 600（System is busy），属于瞬时故障，内部自动重试，
        避免调用方误判成上传失败而重新上传文件。
        """
        import asyncio
        
        # 文档 4：启用 OCR 时每个文件也要带 isOcrFile=1；普通文档 isMath/isFlow 传 0
        files = []
        for item in file_list:
            item = dict(item)
            if is_ocr and "isOcrFile" not in item:
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
            result["msg"] = (
                f"{result.get('msg')}（{REUPLOAD_CODES[result['code']]}）。"
                "请重新调 upload_document 取预签名地址并重新上传，"
                "再用新的 objectKey 提交，重试本工具没有用。"
            )
            return result

        # 重试后仍失败：明确告知文件无需重传，避免调用方回头做上传
        if result.get("code") in TRANSIENT_CODES:
            result["msg"] = (
                f"{result.get('msg')}（已自动重试 4 次）。"
                "这是翻译服务的瞬时故障，与上传无关：文件已上传成功，"
                "fileObjectKey 仍然有效，请稍后用相同参数重试 translate_document，"
                "不要重新上传文件。"
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
        result["lineNote"] = (
            ("这是带水印的版本。" if is_watermark else "这是无水印版本。")
            + "url 走 CloudFront；url2 为国内中转线路（部分存储类型为 null），"
            "海外线路不通时改用它。" + _URL_VERBATIM_NOTE
            + _expiry_note(result.get("url"))
        )
        return result
    
    # ============ 图片翻译 ============
    
    async def submit_image_translate(
        self,
        source_language: str,
        target_language: str,
        image_url: str,
    ) -> dict:
        """提交图片翻译任务"""
        response = await self.client.post(
            f"{DOC_PREFIX}/submitImageTranslate",
            json={
                "sourceLanguage": source_language,
                "targetLanguage": target_language,
                "imageUrl": image_url,
            }
        )
        response.raise_for_status()
        return response.json()
    
    async def get_image_translate_detail(self, order_no: str) -> dict:
        """查询图片翻译详情"""
        response = await self.client.get(
            f"{DOC_PREFIX}/getImageTranslateDetail",
            params={"orderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
    # ============ 视频翻译 ============
    
    async def video_batch_presigned_upload_url(self, file_name_list: list[str]) -> dict:
        """批量获取视频预签名上传 URL"""
        result = await self._post(f"{VIDEO_PREFIX}/batchPresignedUploadUrl", {"fileNameList": file_name_list})

        _attach_upload_command(result)
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
        return await self._post(
            f"{VIDEO_PREFIX}/submitVideoTranslate",
            {
                "sourceLanguage": source_language,
                "targetLanguage": target_language,
                "sourceFileObjectKey": source_file_object_key,
                "videoFileName": video_file_name,
                "videoTaskParam": video_task_param,
            },
        )
    
    async def video_translate_quota_calculate(
        self,
        video_duration: float,
        voice_role: str,
        subtitle_type: int,
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
        return result
    
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
        for record in (result.get("data") or {}).get("records") or []:
            _annotate_video_status(record)
        if result.get("code") == "200":
            result["statusNote"] = (
                "视频任务状态：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消"
                "——注意 2 就是完成，和文档翻译的状态码不是一套。"
                "只保留最近 15 天的记录；各 *Url 为临时签名地址，60 分钟内有效。"
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
