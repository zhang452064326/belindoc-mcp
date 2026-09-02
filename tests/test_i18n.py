"""九种语言的用户可见文案

语言清单跟 free-pdf-translate 的 messages/ 对齐。这里盯三件事：语言怎么选、
九份表有没有漏条目或写坏占位符、以及真跑一轮 wait 出来的那行是不是整行都换了
语言——半截中文半截英文比全中文更糟。
"""

import re

import pytest

from trans_mcp import i18n, tools
from trans_mcp.client import _output_note, _format_duration


@pytest.fixture(autouse=True)
def reset_locale(monkeypatch):
    monkeypatch.delenv("MCP_LOCALE", raising=False)
    token = i18n.use_locale(None)
    yield
    i18n.reset_locale(token)


def test_locale_tags_are_normalized():
    assert i18n.normalize("zh-CN") == "zh"
    assert i18n.normalize("zh-TW") == "zh-Hant"
    assert i18n.normalize("zh-Hant") == "zh-Hant"
    assert i18n.normalize("EN") == "en"
    assert i18n.normalize("en-US") == "en"
    assert i18n.normalize("ja_JP") == "ja"
    # 认不出的返回空串，交给上层兜底，不要抛
    assert i18n.normalize("pt-BR") == ""
    assert i18n.normalize(None) == "" and i18n.normalize(123) == ""


def test_locale_precedence_is_arg_then_env_then_zh(monkeypatch):
    assert i18n.resolve(None) == "zh"
    monkeypatch.setenv("MCP_LOCALE", "ja")
    assert i18n.resolve(None) == "ja"
    assert i18n.resolve("de") == "de"
    # 传了不支持的值不报错，退到环境变量
    assert i18n.resolve("pt-BR") == "ja"


def test_no_locale_is_missing_a_key():
    base = set(i18n.MESSAGES["zh"])
    for locale in i18n.SUPPORTED_LOCALES:
        assert set(i18n.MESSAGES[locale]) == base, f"{locale} 的条目和中文对不上"


def test_placeholders_match_the_chinese_source():
    """占位符写错（{duration} 写成 {time}）不会报错，只会在用户面前露出花括号"""
    holder = re.compile(r"\{(\w+)\}")
    for key, zh_text in i18n.MESSAGES["zh"].items():
        expected = set(holder.findall(zh_text))
        for locale in i18n.SUPPORTED_LOCALES:
            got = set(holder.findall(i18n.MESSAGES[locale][key]))
            assert got == expected, f"{locale}/{key} 占位符不一致：{got} != {expected}"


def test_output_note_follows_the_locale():
    record = {"paramJson": '{"voiceRole": "No", "subtitleType": 1}'}
    token = i18n.use_locale("en")
    try:
        assert _output_note(record) == "no dubbing (original audio kept), translated subtitles burned in"
        assert _format_duration(125) == "2m 5s"
    finally:
        i18n.reset_locale(token)
    # 回到默认还是中文
    assert _output_note(record) == "未配音（保留原声），已嵌入译文字幕"


def test_unsupported_locale_falls_back_to_chinese():
    token = i18n.use_locale("pt-BR")
    try:
        assert _output_note({"paramJson": '{"voiceRole": "No", "subtitleType": 1}'}) \
            == "未配音（保留原声），已嵌入译文字幕"
    finally:
        i18n.reset_locale(token)


def test_locale_param_is_exposed_on_the_user_facing_tools():
    by_name = {t.name: t for t in tools.TOOLS}
    for name in tools._LOCALE_AWARE_TOOLS:
        schema = by_name[name].input_schema["properties"]
        assert "locale" in schema, f"{name} 少了 locale 参数"
        assert schema["locale"]["enum"] == list(i18n.SUPPORTED_LOCALES)
    # 不输出用户可见文案的工具不必加，别把 20 个 schema 都撑大
    assert "locale" not in by_name["cancel_video_translation"].input_schema.get("properties", {})


@pytest.mark.asyncio
async def test_confirmation_menu_follows_the_locale():
    """确认菜单是用户拿来做决定的那张表，整张都要换语言——
    七格标签、放弃那一行、额度警告，任何一处漏了都会在菜单中间露出中文。"""
    from trans_mcp.client import video_combo_label

    token = i18n.use_locale("en")
    try:
        assert video_combo_label("No", 1) == "Original audio + translated subtitles"
        assert video_combo_label("clone", 0) == "Cloned dubbing + no subtitles (audio only)"
        assert i18n.t("menu.give_up", index=8) == "8. Cancel — do not submit, no charge"
        assert "2 credits left" in i18n.t("warn.quota", available=2, needed=4)
        # 菜单里的额度单位也要跟着走，别在英文菜单里写「4 额度」
        assert i18n.t("menu.option", index=1, label="x", quota=4, note="", mark="") \
            == "1. x — 4 credits"
    finally:
        i18n.reset_locale(token)
    assert video_combo_label("No", 1) == "原声 + 译文字幕"
    # 菜单外的组合（上游给了没见过的字幕码）退回原始取值，不要编一个标签
    assert video_combo_label("No", 9) == "No/9"
