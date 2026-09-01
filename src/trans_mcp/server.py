"""MCP Server 主入口（stdio 模式）"""

import asyncio
import os
import sys
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from .client import TranslationClient
from .tools import register_tools


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

    # 工具定义与处理器都在 tools.py，与 HTTP 模式共用同一份。
    # 这里原先内联了一整套副本，两边各自演化到 14 个 schema、6 段描述、
    # 4 个默认值互不相同——其中 get_document_translation_result 的
    # url_type 默认取 1，stdio 客户端拿到的一直是原文而不是译文。
    register_tools(server, client)

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
