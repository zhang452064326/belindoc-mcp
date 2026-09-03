"""双语对照版式的提示

网页端把「左右对照」「上下对照」明摆在下载菜单里（free-pdf-translate 的
TaskActions / TransCard / TransHistoryTable / PreviewContent / TranslateClient
五处判定逐字一致：横向仅 PDF，纵向 PDF 与 EPUB）。接口这边不主动说，用户就永远
不知道有这功能——所以翻译完成时要报出来。

判据是**文件类型**，不是详情里有没有那条地址：对照版按需生成，详情里没有
xComparisonS3Url 不代表这个 PDF 取不到横向对照。
"""

import asyncio

import pytest

import trans_mcp.client as client_mod
from trans_mcp import i18n, tools
from trans_mcp.client import (
    TranslationClient, comparison_offer, comparison_variants, doc_file_type,
)


@pytest.fixture(autouse=True)
def zh(monkeypatch):
    monkeypatch.delenv("MCP_LOCALE", raising=False)
    token = i18n.use_locale(None)
    client_mod._WAITS.clear()
    yield
    i18n.reset_locale(token)
    client_mod._WAITS.clear()


@pytest.mark.parametrize("file_type,expected", [
    ("PDF", (3, 4)),
    ("EPUB", (4,)),
    ("DOCX", ()),
    ("PPTX", ()),
    ("XLSX", ()),
    ("TXT", ()),
    ("", ()),
])
def test_which_types_get_which_layouts(file_type, expected):
    assert comparison_variants({"fileType": file_type}) == expected


def test_file_type_falls_back_to_the_extension():
    """列表/详情不一定带 fileType，文件名后缀是兜底"""
    assert doc_file_type({"sourceFileName": "a.PDF"}) == "PDF"
    assert doc_file_type({"sourceFileName": "报告.epub"}) == "EPUB"
    assert doc_file_type({"sourceFileName": "noext"}) == ""
    # 有 fileType 就以它为准
    assert doc_file_type({"fileType": "epub", "sourceFileName": "a.pdf"}) == "EPUB"


def test_offer_wording_matches_the_type():
    assert "左右对照" in comparison_offer({"fileType": "PDF"})
    epub = comparison_offer({"fileType": "EPUB"})
    assert "上下对照" in epub and "左右" in epub and "没有左右并排" in epub
    assert comparison_offer({"fileType": "DOCX"}) == ""


@pytest.mark.asyncio
async def test_finished_pdf_offers_both_layouts_without_urls_in_the_detail():
    """详情里没有对照版地址（按需生成）时，也照样要报出这两种版式"""
    c = TranslationClient("test_api_key")
    detail = {
        "code": "200",
        "data": {
            "status": 3, "sourceFileName": "spec.pdf", "fileType": "PDF",
            "sourceLanguage": "en", "targetLanguage": "zh-CN", "model": "Gemini-2.5-Flash",
            "targetFileUrl": "https://cdn/x.pdf?Expires=99999999999&Signature=s",
        },
    }
    c.get_translate_file_detail = lambda o: asyncio.sleep(0, result=detail)
    c.get_translate_s3_download_url = lambda o, u, w: asyncio.sleep(
        0, result={"url": "https://cdn/x.pdf?Expires=99999999999&Signature=s"}
    )

    result = await c.wait_for_translation("DOC1", timeout=5)
    assert result["code"] == "200"
    # 版式说明是说给用户听的，留在 msg；「不问也要说」是操作指令，走 agentNote
    assert "左右对照" in result["msg"]
    assert "主动告诉用户" in result["agentNote"] and "主动告诉用户" not in result["msg"]
    variants = result["data"]["availableVariants"]
    assert any("url_type=3" in v for v in variants)
    assert any("url_type=4" in v for v in variants)
    assert result["data"]["fileType"] == "PDF"


@pytest.mark.asyncio
async def test_finished_docx_offers_nothing_extra():
    c = TranslationClient("test_api_key")
    detail = {
        "code": "200",
        "data": {
            "status": 3, "sourceFileName": "a.docx", "fileType": "DOCX",
            "sourceLanguage": "en", "targetLanguage": "zh-CN", "model": "Gemini-2.5-Flash",
            "targetFileUrl": "https://cdn/a.docx?Expires=99999999999&Signature=s",
        },
    }
    c.get_translate_file_detail = lambda o: asyncio.sleep(0, result=detail)
    c.get_translate_s3_download_url = lambda o, u, w: asyncio.sleep(
        0, result={"url": "https://cdn/a.docx?Expires=99999999999&Signature=s"}
    )
    result = await c.wait_for_translation("DOC2", timeout=5)
    assert "对照" not in result["msg"]
    assert "没有其他可用版式" in result["data"]["downloadNote"]


@pytest.mark.asyncio
async def test_status_query_also_offers_the_layouts():
    """交付现场不止 wait 一条路，查状态看到已完成时同样要报"""
    c = TranslationClient("test_api_key")
    c.get_translate_file_detail = lambda o: asyncio.sleep(0, result={
        "code": "200",
        "data": {"status": 3, "sourceFileName": "spec.pdf", "fileType": "PDF"},
    })
    out = await tools._doc_status(c, "DOC1")
    assert "左右对照" in out["msg"]
    assert out["data"]["comparisonVariants"] == [3, 4]


@pytest.mark.asyncio
async def test_offer_follows_the_locale():
    c = TranslationClient("test_api_key")
    c.get_translate_file_detail = lambda o: asyncio.sleep(0, result={
        "code": "200",
        "data": {"status": 3, "sourceFileName": "spec.pdf", "fileType": "PDF"},
    })
    token = i18n.use_locale("en")
    try:
        out = await tools._doc_status(c, "DOC1")
        assert "side-by-side" in out["msg"]
    finally:
        i18n.reset_locale(token)


def test_unrecognized_file_type_falls_back_to_the_extension():
    """实测一次：一份 PDF 交付时一个字都没提对照版式

    上游的 fileType 不保证是扩展名。原来只要它非空就照单全收，于是 "1" 这种
    值让 COMPARISON_BY_TYPE 查不到东西，comparison_offer 返回空串——交付现场
    就只剩纯译文，用户根本不知道还能要左右/上下对照。认不出的一律退回后缀。
    """
    assert doc_file_type({"fileType": "1", "sourceFileName": "声环境功能.pdf"}) == "PDF"
    assert doc_file_type({"fileType": "application/pdf", "sourceFileName": "a.pdf"}) == "PDF"
    assert "左右对照" in comparison_offer({"fileType": "1", "sourceFileName": "a.pdf"})
    # 认不出、又没有后缀可退，就保持原样，别凭空造一个类型出来
    assert doc_file_type({"fileType": "怪东西", "sourceFileName": "noext"}) == "怪东西"


def test_offer_also_comes_from_urls_the_detail_already_has():
    """类型认不出时，详情里真给了对照版地址也算数"""
    data = {"fileType": "1", "sourceFileName": "noext", "xComparisonS3Url": "https://x"}
    assert client_mod.offered_variants(data) == (3,)
    assert "左右对照" in comparison_offer(data, client_mod.offered_variants(data))
    # 纯译文自己不算「另有版式」
    assert client_mod.offered_variants(
        {"fileType": "DOCX", "sourceFileName": "a.docx", "targetFileUrl": "https://t"}
    ) == ()


def test_detail_field_spelling_from_the_real_upstream():
    """实测详情里是 xcomparisonS3Url / ycomparisonS3Url（小写 c）

    原来只认驼峰那版，于是「详情里已经有对照版地址」这条线索一次都没生效过。
    两种写法都认。
    """
    real = {"fileType": "PDF", "sourceFileName": "a.pdf",
            "xcomparisonS3Url": "https://x", "ycomparisonS3Url": "https://y"}
    assert client_mod._available_variants(real) == {3: "https://x", 4: "https://y"}
    camel = {"fileType": "1", "sourceFileName": "noext", "yComparisonS3Url": "https://y"}
    assert client_mod._available_variants(camel) == {4: "https://y"}
    # 纯译文的国内兜底线路不是一种版式，别混进来
    assert client_mod._available_variants(
        {"targetFileUrl2": "https://cn"}
    ) == {}


def test_the_offer_gets_its_own_content_block():
    """夹在 msg 里活不下来，就单独发一块，且排在 JSON 前面"""
    from trans_mcp.tools import _blocks

    result = {"code": "200", "msg": "翻译完成。", "sayToUser": "还能要对照版。",
              "data": {"finished": True}}
    blocks = _blocks(result)
    assert len(blocks) == 2
    assert blocks[0].text == "还能要对照版。"          # 纯用户文案，不掺操作指令
    assert "sayToUser" not in blocks[1].text          # 别在 JSON 里再抄一遍
    assert '"finished": true' in blocks[1].text
    # 没有这句话的工具照旧只有一块
    assert len(_blocks({"code": "200", "data": {}})) == 1


@pytest.mark.asyncio
async def test_finished_pdf_puts_the_offer_in_say_to_user():
    c = TranslationClient("test_api_key")
    detail = {
        "code": "200",
        "data": {
            "status": 3, "sourceFileName": "spec.pdf", "fileType": "PDF",
            "sourceLanguage": "en", "targetLanguage": "zh-CN", "model": "Gemini-2.5-Flash",
            "targetFileUrl": "https://cdn/x.pdf?Expires=99999999999&Signature=s",
        },
    }
    c.get_translate_file_detail = lambda o: asyncio.sleep(0, result=detail)
    c.get_translate_s3_download_url = lambda o, u, w: asyncio.sleep(
        0, result={"url": "https://cdn/x.pdf?Expires=99999999999&Signature=s"}
    )
    result = await c.wait_for_translation("DOC3", timeout=5)
    say = result["sayToUser"]
    assert "左右对照" in say
    # 合成要等这件事也要说，用户才知道为什么默认没给
    assert "多等一会儿" in say
    # 操作指令一个字都不许混进这一块
    assert "调用" not in say and "url_type" not in say
