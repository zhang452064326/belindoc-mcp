"""上游地址走环境变量

原先 API_BASE_URL 是写死的测试环境常量，切生产得改源码——而部署包是 tar 解出来的，
等于每次升级都要重改一遍。现在默认值不变，BELINDOC_API_BASE_URL 一设即覆盖。
"""

import importlib

import pytest

import trans_mcp.client as client_mod


@pytest.fixture
def reloaded(monkeypatch):
    """按环境变量重载 client 模块，用完还原成默认值"""

    def load(value):
        if value is None:
            monkeypatch.delenv("BELINDOC_API_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("BELINDOC_API_BASE_URL", value)
        return importlib.reload(client_mod)

    yield load
    monkeypatch.delenv("BELINDOC_API_BASE_URL", raising=False)
    importlib.reload(client_mod)


def test_defaults_to_test_environment(reloaded):
    mod = reloaded(None)
    assert mod.API_BASE_URL == mod.DEFAULT_API_BASE_URL == "http://internal-test-host:6101"


@pytest.mark.parametrize("value,expected", [
    ("https://belindoc.com/api", "https://belindoc.com/api"),
    # 末尾斜杠和空格都别带进 httpx 的 base_url，否则拼出 //external/...
    ("https://belindoc.com/api/", "https://belindoc.com/api"),
    ("  https://belindoc.com/api  ", "https://belindoc.com/api"),
    # 空字符串当没设，别把 base_url 拼成空
    ("", "http://internal-test-host:6101"),
])
def test_env_overrides_base_url(reloaded, value, expected):
    mod = reloaded(value)
    assert mod.API_BASE_URL == expected


def test_client_and_404_hint_both_follow_the_env(reloaded):
    mod = reloaded("https://belindoc.com/api")
    client = mod.TranslationClient("ft_x")
    # httpx 自己会补末尾斜杠，比到路径为止即可
    assert str(client.client.base_url) == "https://belindoc.com/api/"
    # 404 那句可读提示报的是「当前服务地址」，也得跟着变，否则排查会指向错的环境
    assert "https://belindoc.com/api" in mod._not_found("/external/videoTranslate/x")["msg"]
