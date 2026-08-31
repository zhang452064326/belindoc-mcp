#!/usr/bin/env python3
"""测试 API 连接"""

import asyncio
import os
from trans_mcp.client import TranslationClient


async def test_api():
    """测试 API 连接"""
    api_key = os.environ.get("BELINDOC_API_KEY")
    if not api_key:
        print("错误: 请设置 BELINDOC_API_KEY 环境变量")
        print("export BELINDOC_API_KEY='your_api_key'")
        return
    
    print(f"测试 API Key: {api_key[:10]}...")
    client = TranslationClient(api_key)
    
    try:
        print("\n1. 测试获取语言列表...")
        result = await client.get_language_enum()
        if result.get("code") == "200":
            print("✓ 语言列表获取成功")
            print(f"  支持的语言: {len(result.get('data', {}))} 种")
        else:
            print(f"✗ 错误: {result.get('msg')}")
            return
        
        print("\n2. 测试查询配额...")
        result = await client.get_quota()
        if result.get("code") == "200":
            print("✓ 配额查询成功")
            print(f"  配额信息: {result.get('data')}")
        else:
            print(f"✗ 错误: {result.get('msg')}")
        
        print("\n✓ API 连接测试完成!")
        
    except Exception as e:
        print(f"\n✗ 连接错误: {e}")
        print("\n可能的原因:")
        print("1. API Key 不正确或未在数据库中注册")
        print("2. API Key 已过期或被禁用")
        print("3. 后端服务未启动")
        print("4. 网络连接问题")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(test_api())
