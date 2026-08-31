"""MCP Server 主入口"""

import asyncio
import os
import sys
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    ListToolsRequest,
    CallToolRequest,
    ListToolsResult,
    CallToolResult,
    CallToolRequestParams,
    PaginatedRequestParams,
    Tool,
    TextContent,
)
from .client import TranslationClient


def main():
    """启动 MCP Server"""
    
    # 从环境变量读取 API Key
    api_key = os.environ.get("BELINDOC_API_KEY")
    if not api_key:
        print("错误: 请设置 BELINDOC_API_KEY 环境变量", file=sys.stderr)
        print("export BELINDOC_API_KEY='your_api_key'", file=sys.stderr)
        return
    
    # 创建 MCP Server
    server = Server("trans-mcp")
    
    # 创建 API 客户端
    client = TranslationClient(api_key)
    
    # 工具定义列表
    TOOLS = [
        Tool(
            name="get_supported_languages",
            description="获取支持的语言列表。请在调用 translate_document 之前调用此工具，让用户选择源语言和目标语言。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_model_list",
            description="获取当前用户可用的翻译模型列表。请在调用 translate_document 之前调用此工具，并让用户选择一个模型。",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="upload_document",
            description="批量获取文档上传链接",
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
            description="上传本地文件到翻译平台。这个工具会自动获取预签名URL并上传文件，返回上传结果和objectKey。",
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
            description="提交文档翻译任务。请先调用 get_model_list 获取可用模型，调用 get_supported_languages 获取支持的语言列表，然后让用户选择模型和目标语言。",
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
                        "description": "文件列表"
                    },
                    "source_language": {
                        "type": "string",
                        "description": "源语言代码。如果用户未指定，请让用户从 get_supported_languages 返回的列表中选择。"
                    },
                    "target_language": {
                        "type": "string",
                        "description": "目标语言代码。这是必填项，如果用户未指定，请让用户从 get_supported_languages 返回的列表中选择。"
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
            description="获取文档翻译结果下载链接",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_no": {
                        "type": "string",
                        "description": "翻译任务订单号"
                    },
                    "url_type": {
                        "type": "integer",
                        "description": "URL 类型，1=主链接，2=备用链接"
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
                        "description": "任务状态过滤"
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
                    "source_language": {"type": "string"},
                    "target_language": {"type": "string"},
                    "source_file_object_key": {"type": "string"},
                    "video_file_name": {"type": "string"},
                    "voice_role": {"type": "string"},
                    "subtitle_type": {"type": "integer"}
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
                    "video_duration": {"type": "number"},
                    "voice_role": {"type": "string"},
                    "subtitle_type": {"type": "integer"}
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
                    "order_no": {"type": "string"}
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
                    "page_num": {"type": "integer"},
                    "page_size": {"type": "integer"},
                    "status": {"type": "integer"}
                }
            }
        ),
        Tool(
            name="cancel_video_translation",
            description="取消视频翻译任务",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_no": {"type": "string"}
                },
                "required": ["order_no"]
            }
        ),
        Tool(
            name="wait_for_translation",
            description="等待翻译任务完成。自动轮询翻译状态，直到翻译完成或超时。返回翻译结果信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_no": {
                        "type": "string",
                        "description": "翻译任务订单号"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 300 秒（5分钟）"
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
                    "order_no": {"type": "string"}
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
                    "order_no": {"type": "string"},
                    "source_subtitles_txt": {"type": "string"},
                    "target_subtitles_txt": {"type": "string"}
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
                    "order_no": {"type": "string"}
                },
                "required": ["order_no"]
            }
        ),
    ]
    
    # 工具处理器映射
    TOOL_HANDLERS = {
        "get_supported_languages": lambda args: client.get_language_enum(),
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
            args.get("url_type", 1)
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
    
    # 注册工具列表处理器
    async def handle_list_tools(ctx, params):
        return ListToolsResult(tools=TOOLS)
    
    # 注册工具调用处理器
    async def handle_call_tool(ctx, params):
        tool_name = params.name
        args = params.arguments or {}
        
        if tool_name not in TOOL_HANDLERS:
            return CallToolResult(
                content=[TextContent(type="text", text=f"未知工具: {tool_name}")]
            )
        
        try:
            result = await TOOL_HANDLERS[tool_name](args)
            return CallToolResult(
                content=[TextContent(type="text", text=str(result))]
            )
        except Exception as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"错误: {str(e)}")]
            )
    
    # 注册处理器
    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)
    
    # 启动服务
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n服务已停止", file=sys.stderr)
    finally:
        asyncio.run(client.close())


if __name__ == "__main__":
    main()
