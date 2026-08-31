"""API 客户端"""

import json
import httpx
from typing import Optional

# 测试环境
API_BASE_URL = "http://internal-test-host:6101"
# 生产环境（取消注释切换）
# API_BASE_URL = "https://belindoc.com/api"
DOC_PREFIX = "/external/translate"
# 上游瞬时错误码：600 = System is busy
TRANSIENT_CODES = {"600"}
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
VIDEO_PREFIX = "/external/videoTranslate"


def _variant_hint(file_type: str | None) -> str:
    """按文件类型说明可用的版式，避免调用方去试注定失败的取值"""
    ft = (file_type or "").upper()
    if ft == "PDF":
        return "url_type=1 原文、3 横向对照（左右并排）、4 纵向对照（原文与译文上下排列）。"
    if ft == "EPUB":
        return "url_type=1 原文、4 纵向对照（原文与译文上下排列）；本类型不支持横向对照。"
    return f"url_type=1 原文；{ft or '该类型'} 不支持对照版式（横向仅 PDF，纵向仅 PDF 与 EPUB）。"


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
        item["uploadCommand"] = (
            f"curl -X PUT --upload-file '<本机文件的完整路径>' "
            f"-H \"Content-Disposition: attachment; filename*=UTF-8''{encoded}\" "
            f"'{url}'"
        )
        item["uploadNote"] = (
            "请原样执行 uploadCommand，只替换文件路径；"
            "Content-Disposition 一个字符都不能改，否则 S3 会报 SignatureDoesNotMatch。"
            "上传成功后用本条的 objectKey 作为 fileObjectKey 调 translate_document。"
        )


class TranslationClient:
    """翻译 API 客户端"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0)  # 30秒超时
        )
    
    async def close(self):
        await self.client.aclose()
    
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
            json={"fileNameList": file_name_list}
        )
        response.raise_for_status()
        result = response.json()
        _attach_upload_command(result)
        return result
    
    async def upload_file(self, file_path: str) -> dict:
        """上传本地文件到翻译平台"""
        import os
        
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
        presigned_url = upload_data["persignedUploadUrl"]
        object_key = upload_data["objectKey"]
        encode_file_name = upload_data["encodeFileName"]
        
        # 读取文件并上传
        with open(file_path, "rb") as f:
            file_content = f.read()
        
        # 使用 httpx 上传（同步）
        import httpx as sync_httpx
        upload_response = sync_httpx.put(
            presigned_url,
            content=file_content,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encode_file_name}"
            }
        )
        
        if upload_response.status_code == 200:
            return {
                "code": "200",
                "msg": "文件上传成功",
                "data": {
                    "fileName": file_name,
                    "objectKey": object_key,
                    "fileSize": len(file_content)
                }
            }
        else:
            return {
                "code": "500",
                "msg": f"文件上传失败: {upload_response.status_code}",
                "error": upload_response.text[:200]
            }
    
    async def wait_for_translation(self, order_no: str, timeout: int = 120) -> dict:
        """等待翻译任务完成，自动轮询状态

        超时不算失败：返回当前进度与排队信息，由调用方决定是否继续等待。
        """
        import asyncio
        
        start_time = asyncio.get_event_loop().time()
        last_progress = -1
        interval = 2  # 轮询间隔，逐步放宽到 15 秒，避免高频打上游
        snapshot = {}
        
        while True:
            # 检查超时
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return {
                    "code": "202",
                    "msg": (
                        f"仍在处理中（已等待 {int(elapsed)} 秒）。"
                        f"任务未失败，可再次调用 wait_for_translation 继续等待。"
                    ),
                    "data": {
                        "orderNo": order_no,
                        "elapsed": int(elapsed),
                        "finished": False,
                        **snapshot,
                    }
                }
            
            # 查询翻译状态
            try:
                status = await self.get_translate_file_detail(order_no)
                if status.get("code") != "200":
                    return status
                
                data = status["data"]
                task_status = data["status"]
                progress_info = data.get("taskProgress")
                progress = float(progress_info["progress"]) if progress_info else 0
                
                # 状态: 0=排队, 2=翻译中, 3=完成
                if task_status == 3:
                    # 翻译完成，取译文链接（url_type=2）；1 是原文，不要用
                    download_result = await self.get_translate_s3_download_url(order_no, 2)
                    
                    return {
                        "code": "200",
                        "msg": "翻译完成",
                        "data": {
                            "orderNo": order_no,
                            "finished": True,
                            "fileName": data["sourceFileName"],
                            "sourceLanguage": data["sourceLanguage"],
                            "targetLanguage": data["targetLanguage"],
                            "model": data["model"],
                            "textNumber": data.get("textNumber"),
                            "elapsed": int(elapsed),
                            "downloadUrl": download_result.get("url"),
                            "downloadUrlCN": download_result.get("url2"),
                            "downloadNote": (
                                "以上为纯译文。downloadUrl 走 CloudFront，"
                                "downloadUrlCN 为国内兜底线路，前者慢或不通时改用后者。"
                                "其他版式用 get_document_translation_result 取："
                                + _variant_hint(data.get("fileType"))
                            ),
                        }
                    }
                elif task_status in [0, 2]:
                    # 排队或翻译中，记录快照供超时返回时带出
                    status_text = "排队中" if task_status == 0 else "翻译中"
                    snapshot = {
                        "statusText": status_text,
                        "progress": f"{progress:.1f}%",
                        "fileName": data.get("sourceFileName"),
                    }
                    if progress_info:
                        snapshot["queueRank"] = progress_info.get("taskRanking")
                        snapshot["queueTotal"] = progress_info.get("totalTask")
                        snapshot["predictWaitSeconds"] = progress_info.get("predictWaitTime")
                    if int(progress) != int(last_progress):
                        last_progress = progress
                        print(f"[{status_text}] 进度: {progress:.1f}% (已等待 {int(elapsed)}秒)", flush=True)
                else:
                    # 未知状态
                    return {
                        "code": "500",
                        "msg": f"未知翻译状态: {task_status}",
                        "data": data
                    }
                
                # 递增退避：2s 起，逐步放宽到 15s 上限
                await asyncio.sleep(interval)
                interval = min(interval + 1, 15)
                
            except Exception as e:
                # 网络错误，等待后重试
                print(f"查询出错: {e}，{interval}秒后重试...", flush=True)
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
        
        payload = {
            "fileList": file_list,
            "sourceLanguage": source_language,
            "targetLanguage": target_language,
            "model": model,
            "isOcr": is_ocr,
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
    ) -> dict:
        """获取翻译文件下载地址

        url_type: 1=原文, 2=纯译文, 3=横向对照, 4=纵向对照。
        默认 2——调用方要的通常是译文，取 1 会拿到原文。

        返回的 url 走 CloudFront，url2 走 download.belindoc.com（国内兜底线路）。
        """
        response = await self.client.post(
            f"{DOC_PREFIX}/getTranslateS3DownloadUrl",
            json={
                "translateOrderNo": order_no,
                "urlType": url_type,
            }
        )
        response.raise_for_status()
        
        # 解析 SSE 格式的响应
        content = response.text
        result = {}
        for line in content.split('\n'):
            if line.startswith('data:') and len(line) > 5:
                json_str = line[5:].strip()
                if json_str:
                    try:
                        result = json.loads(json_str)
                    except:
                        pass
        
        if not result:
            return {"error": "未获取到下载链接", "raw": content}
        
        result["urlType"] = url_type
        result["variant"] = URL_TYPE_LABELS.get(url_type, f"未知版式({url_type})")
        
        if not result.get("url"):
            # 该文件类型不支持这个版式时，上游只回 "Failed to generate file."
            support = URL_TYPE_SUPPORT.get(url_type)
            result["code"] = "400"
            result["msg"] = (
                f"该文件不支持「{result['variant']}」版式"
                + (f"（{support} 支持）。" if support else "。")
                + "请改用 url_type=2 取纯译文，或 url_type=1 取原文。"
            )
            return result
        
        result["lineNote"] = "url 走 CloudFront；url2 为国内兜底线路，海外线路不通时改用它。"
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
        response = await self.client.post(
            f"{VIDEO_PREFIX}/batchPresignedUploadUrl",
            json={"fileNameList": file_name_list}
        )
        response.raise_for_status()
        result = response.json()
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
        response = await self.client.post(
            f"{VIDEO_PREFIX}/submitVideoTranslate",
            json={
                "sourceLanguage": source_language,
                "targetLanguage": target_language,
                "sourceFileObjectKey": source_file_object_key,
                "videoFileName": video_file_name,
                "videoTaskParam": video_task_param,
            }
        )
        response.raise_for_status()
        return response.json()
    
    async def video_translate_quota_calculate(
        self,
        video_duration: float,
        voice_role: str,
        subtitle_type: int,
    ) -> dict:
        """计算视频翻译配额"""
        response = await self.client.post(
            f"{VIDEO_PREFIX}/videoTranslateQuotaCalculate",
            json={
                "videoDuration": video_duration,
                "voiceRole": voice_role,
                "subtitleType": subtitle_type,
            }
        )
        response.raise_for_status()
        return response.json()
    
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
        response = await self.client.get(
            f"{VIDEO_PREFIX}/searchVideoTranslatePage",
            params=params
        )
        response.raise_for_status()
        return response.json()
    
    async def get_video_translate_detail(self, order_no: str) -> dict:
        """查询视频翻译详情"""
        response = await self.client.get(
            f"{VIDEO_PREFIX}/getVideoTranslateDetail",
            params={"videoTranslateOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
    async def cancel_video_translate(self, order_no: str) -> dict:
        """取消视频翻译任务"""
        response = await self.client.post(
            f"{VIDEO_PREFIX}/cancelVideoTranslateHistory",
            json={"videoTranslateOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
    async def get_video_subtitles(self, order_no: str) -> dict:
        """获取视频字幕"""
        response = await self.client.get(
            f"{VIDEO_PREFIX}/getVideoTranslateSubtitles",
            params={"videoTranslateOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
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
        response = await self.client.post(
            f"{VIDEO_PREFIX}/submitVideoRewrite",
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
    async def get_video_rewrite_detail(self, order_no: str) -> dict:
        """查询视频字幕改写详情"""
        response = await self.client.get(
            f"{VIDEO_PREFIX}/getVideoTranslateRewriteDetail",
            params={"videoTranslateRewriteOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
    
    async def video_rewrite_quota_calculate(self, order_no: str) -> dict:
        """计算视频字幕改写配额"""
        response = await self.client.get(
            f"{VIDEO_PREFIX}/videoTranslateRewriteQuotaCalculate",
            params={"videoTranslateRewriteOrderNo": order_no}
        )
        response.raise_for_status()
        return response.json()
