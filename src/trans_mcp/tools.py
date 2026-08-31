"""MCP 工具定义"""

from typing import Optional
from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ListToolsRequest,
    CallToolRequest,
    ListToolsResult,
    CallToolResult,
    CallToolRequestParams,
    PaginatedRequestParams,
)
from .client import TranslationClient



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
        description="上传本地文件到翻译平台（仅 stdio 模式可用，因为读文件的是服务端进程）。传入路径即可，服务端自动完成预签名与上传并返回 objectKey。HTTP 远程模式下本工具不会出现在工具列表中，请用 upload_document。",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "本地文件的完整路径，如 /Users/xxx/document.pdf"
                }
            },
            "required": ["file_path"]
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
        description="获取文档翻译结果下载链接。url_type 决定拿到哪个版本：1=原文、2=纯译文（默认）、3=双语对照、4=双语对照另一版式。要译文不要传 1。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                },
                "url_type": {
                    "type": "integer",
                    "description": "1=原文, 2=纯译文(默认), 3=双语对照, 4=双语对照另一版式"
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
        description="批量获取视频上传链接",
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
        description="提交视频翻译任务",
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
                    "description": "语音角色，可选 'clone' 或其他角色"
                },
                "subtitle_type": {
                    "type": "integer",
                    "description": "字幕类型，1=硬字幕"
                }
            },
            "required": ["source_language", "target_language", "source_file_object_key", "video_file_name"]
        }
    ),
    Tool(
        name="calculate_video_translation_quota",
        description="计算视频翻译配额",
        inputSchema={
            "type": "object",
            "properties": {
                "video_duration": {
                    "type": "number",
                    "description": "视频时长（秒）"
                },
                "voice_role": {
                    "type": "string",
                    "description": "语音角色"
                },
                "subtitle_type": {
                    "type": "integer",
                    "description": "字幕类型"
                }
            },
            "required": ["video_duration"]
        }
    ),
    Tool(
        name="get_video_translation_status",
        description="查询视频翻译任务状态",
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
        name="list_video_translations",
        description="查询视频翻译任务列表",
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
        description="取消视频翻译任务",
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
        description="等待翻译任务完成。返回 finished=true 时附带 downloadUrl（纯译文）与 bilingualUrl（双语对照）；finished=false 表示仍在处理，返回中含 progress、排队名次与预计等待秒数，可再次调用本工具继续等待（任务不会因此中断）。",
        inputSchema={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "翻译任务订单号"
                },
                "timeout": {
                    "type": "integer",
                    "description": "本次最多等待的秒数，默认 120。到点未完成会返回当前进度而非报错，可再次调用继续等待。"
                }
            },
            "required": ["order_no"]
        }
    ),
    Tool(
        name="get_video_subtitles",
        description="获取视频字幕",
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
        description="提交视频字幕改写任务",
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
                }
            },
            "required": ["order_no", "source_subtitles_txt", "target_subtitles_txt"]
        }
    ),
    Tool(
        name="get_video_rewrite_status",
        description="查询视频字幕改写状态",
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
        "upload_file": lambda args: client.upload_file(args["file_path"]),
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
            args.get("url_type", 2)
        ),
        "list_document_translations": lambda args: client.search_translate_file_page(
            args.get("page_num", 1),
            args.get("page_size", 10),
            args.get("status")
        ),
        "get_document_translation_by_batch": lambda args: client.search_translate_file_by_batch_no(args["batch_no"]),
        "upload_video": lambda args: client.video_batch_presigned_upload_url(args["file_name_list"]),
        "translate_video": lambda args: client.submit_video_translate(
            args["source_language"],
            args["target_language"],
            args["source_file_object_key"],
            args["video_file_name"],
            {
                "voiceRole": args.get("voice_role", "clone"),
                "subtitleType": args.get("subtitle_type", 1)
            }
        ),
        "calculate_video_translation_quota": lambda args: client.video_translate_quota_calculate(
            args["video_duration"],
            args.get("voice_role", "clone"),
            args.get("subtitle_type", 1)
        ),
        "get_video_translation_status": lambda args: client.get_video_translate_detail(args["order_no"]),
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
            args["target_subtitles_txt"]
        ),
        "get_video_rewrite_status": lambda args: client.get_video_rewrite_detail(args["order_no"]),
        "wait_for_translation": lambda args: client.wait_for_translation(args["order_no"], args.get("timeout", 300)),
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
    
    # 注册处理器
    server.add_request_handler("tools/list", ListToolsRequest, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequest, handle_call_tool)
    
    print(f"DEBUG: Registered {len(TOOLS)} tools", flush=True)
