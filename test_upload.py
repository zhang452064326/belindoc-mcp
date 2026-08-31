#!/usr/bin/env python3
"""测试文档上传到S3"""

import httpx
import asyncio
import os

# API 配置
API_BASE_URL = "http://internal-test-host:6101"
DOC_PREFIX = "/external/translate"
API_KEY = "ft_REDACTED_KEY_ROTATED"


async def test_upload():
    """测试文档上传流程"""
    
    # 创建 HTTP 客户端
    client = httpx.AsyncClient(
        base_url=API_BASE_URL,
        headers={
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
        }
    )
    
    try:
        # 步骤1: 获取预签名上传 URL
        print("步骤1: 获取预签名上传 URL...")
        response = await client.post(
            f"{DOC_PREFIX}/batchPresignedUploadUrl",
            json={"fileNameList": ["test.pdf"]}
        )
        response.raise_for_status()
        upload_info = response.json()
        
        if upload_info.get("code") != "200":
            print(f"错误: {upload_info}")
            return
        
        upload_data = upload_info["data"][0]
        presigned_url = upload_data["persignedUploadUrl"]
        object_key = upload_data["objectKey"]
        file_name = upload_data["fileName"]
        
        print(f"✓ 获取上传 URL 成功")
        print(f"  文件名: {file_name}")
        print(f"  Object Key: {object_key}")
        print(f"  预签名 URL: {presigned_url[:100]}...")
        
        # 步骤2: 上传文件到 S3
        print("\n步骤2: 上传文件到 S3...")
        
        # 创建测试文件
        test_file_path = "/tmp/test_upload.pdf"
        with open(test_file_path, "w") as f:
            f.write("This is a test PDF content for upload testing.")
        
        # 读取文件内容
        with open(test_file_path, "rb") as f:
            file_content = f.read()
        
        # 上传到 S3
        upload_response = httpx.put(
            presigned_url,
            content=file_content,
            headers={"Content-Type": "application/pdf"}
        )
        
        if upload_response.status_code == 200:
            print(f"✓ 文件上传成功")
            print(f"  状态码: {upload_response.status_code}")
        else:
            print(f"✗ 文件上传失败")
            print(f"  状态码: {upload_response.status_code}")
            print(f"  响应: {upload_response.text}")
            return
        
        # 步骤3: 提交翻译任务
        print("\n步骤3: 提交翻译任务...")
        translate_response = await client.post(
            f"{DOC_PREFIX}/batchSubmitTranslateTask",
            json={
                "fileList": [
                    {
                        "fileName": file_name,
                        "fileObjectKey": object_key
                    }
                ],
                "sourceLanguage": "en",
                "targetLanguage": "zh-CN",
                "model": "Gemini-2.5-Flash",
                "isOcr": 0
            }
        )
        translate_response.raise_for_status()
        translate_result = translate_response.json()
        
        if translate_result.get("code") == "200":
            print(f"✓ 翻译任务提交成功")
            print(f"  批次号: {translate_result['data']['batchNo']}")
            print(f"  订单号: {translate_result['data']['translateOrderNo']}")
            
            # 步骤4: 查询翻译状态
            print("\n步骤4: 查询翻译状态...")
            order_no = translate_result['data']['translateOrderNo']
            status_response = await client.post(
                f"{DOC_PREFIX}/getTranslateFileDetail",
                json={"translateOrderNo": order_no}
            )
            status_response.raise_for_status()
            status_result = status_response.json()
            
            if status_result.get("code") == "200":
                print(f"✓ 查询状态成功")
                print(f"  状态: {status_result['data']['status']}")
                print(f"  进度: {status_result['data'].get('progress', 'N/A')}%")
            else:
                print(f"✗ 查询状态失败: {status_result}")
        else:
            print(f"✗ 翻译任务提交失败: {translate_result}")
        
        # 清理测试文件
        os.remove(test_file_path)
        print(f"\n✓ 测试完成，已清理临时文件")
        
    except Exception as e:
        print(f"错误: {e}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(test_upload())