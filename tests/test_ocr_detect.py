"""扫描件判定

is_ocr 以前是模型拍脑袋填的：它手里只有文件名。扫描件按普通 PDF 提交会翻出
一片空白，OCR 又走另一档额度，两边拍错都要付代价。上游开了 isOcr 之后，PDF
一律以服务端的判定为准。
"""

import pytest

from trans_mcp import tools
from trans_mcp.client import TranslationClient, note_storage_type, storage_type_of
import trans_mcp.client as client_mod


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def clean_cache():
    client_mod._KEY_OCR.clear()
    client_mod._KEY_STORAGE.clear()
    client_mod._OCR_TASKS.clear()
    yield
    client_mod._KEY_OCR.clear()
    client_mod._KEY_STORAGE.clear()


@pytest.fixture
def client():
    return TranslationClient("test_api_key")


def submitted_files(calls):
    for url, payload in calls:
        if url.endswith("batchSubmitTranslateTask"):
            return payload
    raise AssertionError("没有提交请求")


def make_client(client, ocr_payload):
    """ocr_payload=None 表示检测接口挂了"""
    calls = []

    async def fake_post(url, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("/isOcr"):
            if ocr_payload is None:
                raise RuntimeError("分类服务不可用")
            return FakeResponse({"code": "200", "data": ocr_payload})
        if url.endswith("searchTranslateFilePage"):
            return FakeResponse({"code": "200", "data": {"records": []}})
        return FakeResponse({"code": "200", "data": {}})

    client.client.post = fake_post
    return calls


@pytest.mark.asyncio
async def test_scanned_pdf_is_submitted_as_ocr(client):
    calls = make_client(client, {"isOcr": 1, "isDoubleDeck": 0})
    result = await client.batch_submit_translate_task(
        [{"fileName": "scan.pdf", "fileObjectKey": "k/scan.pdf"}], "zh-CN", "en"
    )
    payload = submitted_files(calls)
    assert payload["isOcr"] == 1
    assert payload["fileList"][0]["isOcrFile"] == 1
    assert "扫描件" in result["msg"] and "scan.pdf" in result["msg"]


@pytest.mark.asyncio
async def test_forced_ocr_is_kept_but_questioned(client):
    """is_ocr=1 是「强制整批 OCR」，服务端 ocrSwatch==1 时压根不看检测结果。
    我们不擅自把它改掉（用户可能真要强制），但要说清这会去扣 OCR 额度。"""
    calls = make_client(client, {"isOcr": 0, "isDoubleDeck": 0})
    result = await client.batch_submit_translate_task(
        [{"fileName": "text.pdf", "fileObjectKey": "k/text.pdf"}], "zh-CN", "en", is_ocr=1
    )
    assert submitted_files(calls)["isOcr"] == 1
    assert "都是文本版" in result["msg"] and "OCR 额度" in result["msg"]


@pytest.mark.asyncio
async def test_mixed_batch_does_not_force_ocr_on_the_text_ones(client):
    """一份扫描件不该把整批拖进 OCR：批级开关一开，文本版那几份也按 OCR 记账"""
    calls = []

    async def fake_post(url, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("/isOcr"):
            scanned = json["fileObjectKey"].endswith("scan.pdf")
            return FakeResponse({"code": "200",
                                 "data": {"isOcr": 1 if scanned else 0, "isDoubleDeck": 0}})
        if url.endswith("searchTranslateFilePage"):
            return FakeResponse({"code": "200", "data": {"records": []}})
        return FakeResponse({"code": "200", "data": {}})

    client.client.post = fake_post
    result = await client.batch_submit_translate_task(
        [{"fileName": "scan.pdf", "fileObjectKey": "k/scan.pdf"},
         {"fileName": "text.pdf", "fileObjectKey": "k/text.pdf"}],
        "zh-CN", "en",
    )
    payload = submitted_files(calls)
    assert payload["isOcr"] == 0                      # 不整批强制
    by_name = {f["fileName"]: f for f in payload["fileList"]}
    assert by_name["scan.pdf"]["isOcrFile"] == 1      # 逐文件标记
    assert by_name["text.pdf"]["isOcrFile"] == 0
    assert "自动 OCR" in result["msg"]                 # 并说清它取决于账号开关


@pytest.mark.asyncio
async def test_non_pdf_is_never_checked(client):
    """扫描件这个概念只对 PDF 成立，别为 docx 白打一趟上游"""
    calls = make_client(client, {"isOcr": 1, "isDoubleDeck": 0})
    await client.batch_submit_translate_task(
        [{"fileName": "a.docx", "fileObjectKey": "k/a.docx"}], "zh-CN", "en"
    )
    assert not [url for url, _ in calls if url.endswith("/isOcr")]


@pytest.mark.asyncio
async def test_detection_failure_does_not_block_the_task(client):
    """辅助接口抽风不能把整批任务卡住，但要说清楚没测成"""
    calls = make_client(client, None)
    result = await client.batch_submit_translate_task(
        [{"fileName": "x.pdf", "fileObjectKey": "k/x.pdf"}], "zh-CN", "en"
    )
    payload = submitted_files(calls)
    assert payload["isOcr"] == 0
    assert "没能判断" in result["msg"] and "空白" in result["msg"]


def test_storage_type_comes_from_the_presign_step():
    """isOcr 要 storageType，而那个值预签发时上游就给了，不必让调用方转述"""
    client_mod._KEY_STORAGE.clear()
    assert storage_type_of("k/unknown.pdf") == 2      # 不知道就按 aws 算
    note_storage_type("k/private.pdf", 1)
    assert storage_type_of("k/private.pdf") == 1


@pytest.mark.asyncio
async def test_double_deck_pdf_points_at_the_flatten_tool(client):
    """双层 PDF 直接翻会翻到那层错字上，得先拍平"""
    calls = make_client(client, {"isOcr": 1, "isDoubleDeck": 1})
    result = await client.batch_submit_translate_task(
        [{"fileName": "dual.pdf", "fileObjectKey": "k/dual.pdf"}], "zh-CN", "en"
    )
    assert "belindoc.com/zh/tools/flatten-pdf" in result["msg"]
    assert "dual.pdf" in result["msg"]


@pytest.mark.asyncio
async def test_detection_is_reused_from_the_upload_step(client):
    """上传后已经测过的，提交时不再打第二趟——那一趟要下载并分析整个 PDF"""
    from trans_mcp.client import remember_ocr

    calls = make_client(client, {"isOcr": 1, "isDoubleDeck": 0})
    remember_ocr("k/cached.pdf", {"fileName": "cached.pdf", "isOcr": 1, "isDoubleDeck": 0})
    payload_calls = await client.batch_submit_translate_task(
        [{"fileName": "cached.pdf", "fileObjectKey": "k/cached.pdf"}], "zh-CN", "en"
    )
    assert not [url for url, _ in calls if url.endswith("/isOcr")]
    assert submitted_files(calls)["isOcr"] == 1


@pytest.mark.asyncio
async def test_check_pdf_ocr_does_not_block_on_a_slow_file(client, monkeypatch):
    """分类要把整个 PDF 下下来，大文件能跑一分钟。工具不该在那儿干耗——
    先回一句「在测了」，结果留给提交那一步。"""
    import asyncio

    async def slow_post(url, json=None, **kwargs):
        if url.endswith("/isOcr"):
            await asyncio.sleep(30)
            return FakeResponse({"code": "200", "data": {"isOcr": 1, "isDoubleDeck": 0}})
        return FakeResponse({"code": "200", "data": {}})

    client.client.post = slow_post
    monkeypatch.setattr(tools, "_OCR_CHECK_WAIT", 0.2)   # 别让单测真等满 8 秒
    tools_result = await tools._check_pdf_ocr(client, "k/slow.pdf", "slow.pdf")
    assert tools_result["code"] == "202"
    assert tools_result["data"]["detecting"] is True
    assert "k/slow.pdf" in client_mod._OCR_TASKS      # 检测确实在后台跑着
    client_mod._OCR_TASKS["k/slow.pdf"].cancel()


@pytest.mark.asyncio
async def test_background_detection_actually_calls_upstream(client):
    """上传时发出去的那趟检测得真打到上游

    原先 start_ocr_detection 挂的是 detect_ocr_for_key 自己，而它一进
    _OCR_TASKS，开头那句「已经有人在测了，等它」等到的就是任务自己——死等到
    120 秒超时，上游一趟都没打出去。表现是 check_pdf_ocr 一直回「还在检测」，
    而同一个 objectKey 直接打上游 1 秒就有答案。扫描件因此被当普通 PDF 提交，
    翻出来一片空白，还扣错了那本账。
    """
    import asyncio

    calls = make_client(client, {"isOcr": 1, "isDoubleDeck": 0})
    client.start_ocr_detection("k/scan.pdf", "scan.pdf")

    # 没有任何人去 await 它，它也该自己跑完
    for _ in range(50):
        if client_mod.ocr_of("k/scan.pdf"):
            break
        await asyncio.sleep(0.01)

    assert [url for url, _ in calls if url.endswith("/isOcr")], "后台检测没打到上游"
    assert client_mod.ocr_of("k/scan.pdf")["isOcr"] == 1


@pytest.mark.asyncio
async def test_check_pdf_ocr_answers_from_the_background_task(client):
    """后台已经测完的，check_pdf_ocr 直接给答案，不再回「还在检测」"""
    import asyncio

    make_client(client, {"isOcr": 1, "isDoubleDeck": 0})
    client.start_ocr_detection("k/scan2.pdf", "scan2.pdf")
    await asyncio.sleep(0.05)

    result = await tools._check_pdf_ocr(client, "k/scan2.pdf", "scan2.pdf")
    assert result["code"] == "200"
    assert result["data"]["isOcr"] == 1
