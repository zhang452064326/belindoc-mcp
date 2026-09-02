"""MCP 工具定义"""

import sys
from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ListToolsResult,
    CallToolResult,
    CallToolRequestParams,
    PaginatedRequestParams,
)
from .client import TranslationClient, _build_video_task_param



VOICE_ROLE_CHOICES = ("No", "clone")


async def _reject(msg: str) -> dict:
    """参数校验不过就返回一条模型看得懂的错误，而不是抛异常——
    让它回去问用户，而不是把栈信息糊给用户看。"""
    return {"code": "400", "msg": msg, "data": None}


def _submit_video_translate(client, args):
    """translate_video 会真实扣费，所以确认与配音选择在这里硬拦，
    光靠工具描述里的祈使句拦不住（实测模型会试算完直接提交）。"""
    voice_role = args.get("voice_role")
    if voice_role not in VOICE_ROLE_CHOICES:
        return _reject(
            "voice_role 必须显式传 No 或 clone，不能省略。请先问用户是否开启同声翻译"
            "（配音）：No=不配音、保留原声只做字幕；clone=克隆原说话人音色配音，"
            "且 subtitle_type≠0 时额度翻倍。拿到用户的选择后再重新调用本工具。"
        )
    if args.get("user_confirmed") is not True:
        return _reject(
            "本次提交会真实扣减额度，必须先用 calculate_video_translation_quota 试算、"
            "把预计消耗和配音选项一并告诉用户并得到明确同意，再带 user_confirmed=true "
            "重新调用。请不要替用户做决定。"
        )
    return client.submit_video_translate(
        args["source_language"],
        args["target_language"],
        args["source_file_object_key"],
        args["video_file_name"],
        _build_video_task_param(
            voice_role,
            args.get("subtitle_type", 1),
            args.get("video_task_param"),
        ),
    )


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
        description="获取当前用户可用的翻译模型列表。请在调用 translate_document 之前调用此工具，并让用户选择一个模型。",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="upload_document",
        description="上传文件的标准方式：先用本工具拿到预签名链接，再原样执行返回的 uploadCommand 完成上传（只替换其中的文件路径，其余尤其是 Content-Disposition 一个字符都不能改，否则 S3 报 SignatureDoesNotMatch）。上传成功后用返回的 objectKey 作为 fileObjectKey 调 translate_document。",
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
        name="upload_file",
        description="上传本地文件到翻译平台（仅 stdio 模式可用，因为读文件的是服务端进程）。传入路径即可，服务端自动完成预签名与上传。小文件通常在本次调用内就传完并返回 objectKey；大文件等满 wait 秒会先返回一次进度（含文件大小、百分比、速度、预计剩余时间）和 uploadId——请把进度转述给用户，再用 uploadId 调 get_upload_status 继续跟进，上传在后台照常进行。HTTP 远程模式下本工具不会出现在工具列表中，请用 upload_document。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "本地文件的完整路径，如 /Users/xxx/document.pdf"
                },
                "wait": {
                    "type": "integer",
                    "description": "本次最多等待的秒数，默认 8。到点未传完会返回当前进度而非报错。"
                }
            },
            "required": ["file_path"]
        }
    ),
    Tool(
        name="get_upload_status",
        description="查询 upload_file 发起的上传进度（仅 stdio 模式可用）。返回 status：uploading=仍在传（附百分比、已传字节、速度、预计剩余秒数，请转述给用户后再调一次继续跟进）、success=已完成（用返回的 objectKey 作为 fileObjectKey 调 translate_document）、failed=失败（error 里是原因）。",
        inputSchema={
            "type": "object",
            "properties": {
                "upload_id": {
                    "type": "string",
                    "description": "upload_file 返回的 uploadId"
                },
                "wait": {
                    "type": "integer",
                    "description": "本次最多等待的秒数，默认 8。到点仍在上传就返回当前进度。"
                }
            },
            "required": ["upload_id"]
        }
    ),
    Tool(
        name="translate_document",
        description="提交文档翻译任务。请先调用 get_model_list 获取可用模型，调用 get_supported_languages 获取支持的语言列表，然后让用户选择模型和目标语言。返回中的 orders[].translateOrderNo 即订单号，直接用它调 wait_for_translation，无需再查列表。若返回非 200（如 600 系统繁忙），说明是翻译服务侧的问题而非上传问题：用相同参数重试本工具即可，不要重新上传文件。",
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
                    "description": "是否启用 OCR，0=否，1=是"
                }
            },
            "required": ["file_list", "source_language", "target_language", "model"]
        }
    ),
    Tool(
        name="get_document_translation_status",
        description="查询文档翻译任务状态",
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
        description="获取文档翻译结果下载链接。url_type 决定版式：1=原文、2=纯译文（默认）、3=横向对照（左右并排，仅 PDF）、4=纵向对照（原文与译文上下排列，仅 PDF 与 EPUB）。要译文不要传 1。返回的 url 走 CloudFront，url2 为国内兜底线路。两条链接都带签名参数，转述给用户时必须连问号后面的 Signature/Key-Pair-Id/expires/sign 一起原样给全，截断或缩短会导致 403 MissingKey。",
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
        description="查询文档翻译任务列表",
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
        description="获取视频文件的预签名上传地址。拿到后原样执行返回的 uploadCommand 完成上传（只替换文件路径，Content-Disposition 一个字符都不能改，否则 S3 报 SignatureDoesNotMatch）。预签名地址仅 10 分钟有效，取到就传。注意视频和文档走的是不同端点、不同存储路径，视频文件必须用本工具，不能用 upload_document。上传成功后用返回的 objectKey 作为 source_file_object_key 调 translate_video。",
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
        description="提交视频翻译任务。⚠️ 本工具会真实扣减账户额度并计入调用次数。提交前必须做完两件事：(1) 用 calculate_video_translation_quota 试算并把预计消耗告诉用户；(2) 问用户是否开启同声翻译（配音），由用户选 No 或 clone。两件事都做完、拿到用户明确同意后，才带 voice_role 和 user_confirmed=true 调用本工具——这两个参数都是必填，缺任何一个都会被直接拒绝，不会提交也不会扣费。voice_role 与 subtitle_type 决定这次翻译到底做什么：两者都关（voice_role 传 No 且 subtitle_type=0）等于既不配音也不嵌字幕，产出的视频和原片没有区别，但一样扣费——上游不拦这个组合，请在提交前自行拦下并问用户。返回 data.videoTranslateOrderNo 是后续所有查询用的订单号。限制：免费用户单个视频最长 10 分钟、每月累计 10 分钟、单文件 200MB、同时只能有 1 个进行中的任务（Pro 为 60 分钟/1024MB/2 个）。若 target_language 传 ar（阿拉伯语）且账号不是付费会员，上游要求人机验证 token，外部调用无法提供，会直接失败。",
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
                    "description": "用户已经看到试算额度、并明确选择了是否开启同声翻译（配音）后才传 true。没问过就传 true 属于替用户做决定，会导致误扣额度。为空或 false 时本工具直接拒绝提交。"
                },
                "video_task_param": {
                    "type": "object",
                    "description": "完整的生成参数，覆盖 voice_role / subtitle_type 这两个快捷参数。配音：voiceRate 语速、volume 音量、pitch 音调（均为 +0% / +0Hz 这类字符串）、voiceAutorate 语音自动变速、videoAutorate 视频自动变速（默认都 true）。字幕样式：fontsize 字号(默认14)、fontname 字体、fontcolor 颜色(#RRGGBB)、fontbold 加粗、subtitlePosX 水平位置 5-95(50居中)、subtitlePosY 底边距 0-90、fontbordercolor 描边色、outline 描边宽 0-10(0关闭)、shadow 阴影 0-10(0关闭)、backgroundcolor 背景框色、borderStyle 1普通描边/3逐行矩形背景框。不传的字段走服务端默认值。注意服务端对这些字段一个都不校验，填了非法值不会报错、会照常扣费然后在生成阶段失败，不确定就别传。"
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
        description="试算视频翻译要消耗多少额度，不扣费。提交 translate_video 前应当先调这个并把结果告诉用户。计费规则：按 30 秒为一个计费单位向上取整，每单位 4 额度；voice_role 为 clone 且 subtitle_type≠0 时额度翻倍（实测 10 分钟视频：不配音 80 额度，开克隆配音 160 额度）。",
        inputSchema={
            "type": "object",
            "properties": {
                "video_duration": {
                    "type": "number",
                    "description": "视频时长，单位是**毫秒**（不是秒）。例如 2 分 5 秒要传 125000。传成秒会让试算额度远低于实际扣费。"
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
        description="等待视频翻译任务完成。提交 translate_video 后就用它跟进，不要自己反复调 get_video_translation_status。上游只能轮询、无法推送，本工具有两个返回时机：进度一有变化就立刻返回，否则最多等 timeout 秒（默认 60）。视频任务通常要几分钟。finished=false 时请把返回 msg 里那行进度告诉用户——不管 changedSinceLastCall 是 true 还是 false 都要说，和上次一样也照样说一遍，不要沉默跳过，然后再次调用本工具继续等待，任务不会因此中断。完成时返回 translatedVideoUrl（译制视频）、targetSubtitlesUrl（译文字幕）等地址，均为临时签名地址、60 分钟有效，必须原样完整交给用户。描述产物时请原样照抄 outputNote（例如「未配音（保留原声），已嵌入译文字幕」），不要凭之前传过的参数自己推断有没有配音。任务失败或被取消时返回 code=500 且 data.failed=true，reason 是原因——请先告诉用户，问过之后再决定是否重新提交，重提会再次扣费。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "视频翻译订单号，即 translate_video 返回的 videoTranslateOrderNo"
                },
                "timeout": {
                    "type": "integer",
                    "description": "本次最多等待的秒数，默认 60。到点未完成会返回当前进度而非报错，可再次调用继续等待。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="list_video_translations",
        description="分页查询视频翻译任务列表，只返回最近 15 天的记录。status 过滤值：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消。完成的记录里 targetFileUrl 是译制视频、targetSubtitlesUrl 是译文字幕，都是临时签名地址、60 分钟有效，必须原样完整交给用户，不能截断签名参数。",
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
        description="等待翻译任务完成。上游只能轮询、无法推送，本工具有两个返回时机：进度一有变化就立刻返回，否则最多等 timeout 秒（默认 45）。返回 finished=true 时附带 downloadUrl（纯译文，CloudFront）与 downloadUrlCN（同一文件的国内兜底线路），其他版式用 get_document_translation_result 取。这两条链接带签名，转述时必须把问号后面的参数一起原样给全，截断会 403。finished=false 表示仍在处理，请把返回的 msg 里那行进度告诉用户——不管 changedSinceLastCall 是 true 还是 false 都要说，进度和上次一样也照样说一遍，不要沉默跳过，然后再次调用本工具继续等待，任务不会因此中断。若任务被服务端取消或失败，返回 code=500 且 data.failed=true，reason 是原因（如 BACKEND_CANCEL）——请先把原因告诉用户，问过用户之后再决定是否用相同参数重试 translate_document，文件不需要重新上传。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                },
                "timeout": {
                    "type": "integer",
                    "description": "本次最多等待的秒数，默认 45。到点未完成会返回当前进度而非报错，可再次调用继续等待。文件较大时可调大，但会让用户等更久才看到反馈。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="get_video_subtitles",
        description="获取视频的原文与译文字幕下载地址。任务 status 必须是 2（成功），否则返回 31008「文件翻译中」。若该任务已有改写记录，返回的是最近一次改写后的字幕。地址 60 分钟有效。",
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
        name="rewrite_video_subtitles",
        description="用编辑后的字幕重新生成视频。⚠️ 本工具会真实扣减额度并计入调用次数，提交前请把预计消耗告诉用户并得到确认。原任务的 status 必须是 2（成功）。返回 data.videoTranslateRewriteOrderNo 是改写订单号，查进度用 get_video_rewrite_status。",
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
                }
            },
            "required": ["order_no", "source_subtitles_txt", "target_subtitles_txt"]
        }
    ),
    Tool(
        name="get_video_rewrite_status",
        description="查询字幕改写任务的进度。上游是 SSE 流，本工具取第一帧数据就返回。状态含义同视频翻译：0 未开始 / 1 进行中 / 2 成功 / 3 失败 / 4 已取消。",
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


def build_tool_handlers(client: TranslationClient):
    """按传入的 client 构建工具处理器映射"""
    return {
        "get_supported_languages": lambda args: client.get_language_enum(args.get("display_locale", "zh")),
        "get_model_list": lambda args: client.get_model_list(),
        "upload_document": lambda args: client.doc_batch_presigned_upload_url(args["file_name_list"]),
        "upload_file": lambda args: client.upload_file(args["file_path"], args.get("wait", 8)),
        "get_upload_status": lambda args: client.get_upload_status(args["upload_id"], args.get("wait", 8)),
        "translate_document": lambda args: client.batch_submit_translate_task(
            args["file_list"],
            args["source_language"],
            args["target_language"],
            args.get("model", "Gemini-2.5-Flash"),
            args.get("is_ocr", 0)
        ),
        "get_document_translation_status": lambda args: client.get_translate_file_detail(args["order_no"]),
        "get_document_translation_result": lambda args: client.get_translate_s3_download_url(
            args["order_no"],
            args.get("url_type", 2),
            args.get("is_watermark", 0)
        ),
        "list_document_translations": lambda args: client.search_translate_file_page(
            args.get("page_num", 1),
            args.get("page_size", 10),
            args.get("status")
        ),
        "get_document_translation_by_batch": lambda args: client.search_translate_file_by_batch_no(args["batch_no"]),
        "upload_video": lambda args: client.video_batch_presigned_upload_url(args["file_name_list"]),
        "translate_video": lambda args: _submit_video_translate(client, args),
        "calculate_video_translation_quota": lambda args: client.video_translate_quota_calculate(
            args["video_duration"],
            args.get("voice_role", "No"),
            args.get("subtitle_type", 1)
        ),
        "get_video_translation_status": lambda args: client.get_video_translate_detail(args["order_no"]),
        "wait_for_video_translation": lambda args: client.wait_for_video_translation(
            args["order_no"], args.get("timeout", 60)
        ),
        "list_video_translations": lambda args: client.search_video_translate_page(
            args.get("page_num", 1),
            args.get("page_size", 10),
            args.get("status")
        ),
        "cancel_video_translation": lambda args: client.cancel_video_translate(args["order_no"]),
        "get_video_subtitles": lambda args: client.get_video_subtitles(args["order_no"]),
        "rewrite_video_subtitles": lambda args: client.submit_video_rewrite(
            args["order_no"],
            args["source_subtitles_txt"],
            args["target_subtitles_txt"],
            args.get("video_task_param"),
        ),
        "get_video_rewrite_status": lambda args: client.get_video_rewrite_detail(args["order_no"]),
        "wait_for_translation": lambda args: client.wait_for_translation(args["order_no"], args.get("timeout", 45)),
    }


def register_tools(server: Server, client: TranslationClient):
    """注册所有 MCP 工具（stdio 模式）"""
    TOOL_HANDLERS = build_tool_handlers(client)
    
    # 注册工具列表处理器
    async def handle_list_tools(ctx, params: PaginatedRequestParams) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)
    
    # 注册工具调用处理器
    async def handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        tool_name = params.name
        args = params.arguments or {}
        
        if tool_name not in TOOL_HANDLERS:
            return CallToolResult(
                content=[TextContent(type="text", text=f"未知工具: {tool_name}")]
            )
        
        try:
            result = await TOOL_HANDLERS[tool_name](args)
            return CallToolResult(
                content=[TextContent(type="text", text=_to_json(result))],
                isError=False
            )
        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {str(e)}")],
                isError=True
            )
    
    # add_request_handler 要的是 params 模型（RequestParams 的子类），
    # 不是 ListToolsRequest / CallToolRequest 这种 Request 模型。
    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)

    # stdio 模式下 stdout 是 JSON-RPC 通道，任何多余输出都会污染协议流
    print(f"已注册 {len(TOOLS)} 个工具", file=sys.stderr, flush=True)
