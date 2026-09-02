"""上传失败时别替用户诊断他的网络

实测一次：uploadCommand 被客户端沙箱挡住（curl 挂住没有输出、nslookup 报
bind: Operation not permitted），模型据此断定「用户网络受限、DNS 解析失败」，
建议开 VPN 或换台机器——用户的网络好好的，是跑命令那一端没有联网权限。
另外预签名 PUT 只有 10 分钟，排查耗掉的时间也算在里面，而 SigV4 的过期写在
X-Amz-Date + X-Amz-Expires 里，原来那个只认 Expires=<epoch> 的解析读不到它。
"""

import time

from trans_mcp.client import _attach_upload_command, _sigv4_expiry, _url_expiry


def _url(issued_gmt=None, ttl=600):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(issued_gmt or time.time()))
    return (
        "https://test-fuxi.s3.ap-southeast-1.amazonaws.com/belin/file/x.mp4"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Date={stamp}&X-Amz-SignedHeaders=content-disposition%3Bhost"
        f"&X-Amz-Expires={ttl}&X-Amz-Signature=830f4c9f"
    )


def test_sigv4_expiry_is_read_from_x_amz_fields():
    # X-Amz-Date 精确到秒，签发时刻会被截到整秒，所以只卡一个区间
    assert 595 <= _sigv4_expiry(_url()) <= 600
    # 老的解析只认 CloudFront 那种 Expires=<epoch>，读不到 X-Amz-Expires
    assert _url_expiry(_url()) is None
    assert _sigv4_expiry("https://s3/x?no-signature=1") is None


def test_expired_link_is_called_expired():
    assert _sigv4_expiry(_url(issued_gmt=time.time() - 900)) == 0


def _note(url):
    result = {"code": "200", "data": [{
        "persignedUploadUrl": url, "objectKey": "belin/file/x.mp4", "encodeFileName": "x.mp4",
    }]}
    _attach_upload_command(result, "video")
    return result["data"][0]["uploadNote"]


def test_upload_note_states_the_deadline_and_triages_failures():
    note = _note(_url())
    # 期限说成绝对时刻，别让调用方按经验编一个
    assert "（服务端本地时间）过期，还剩 " in note and "分" in note
    # 挂住/权限报错要归给沙箱，不能归给用户的网络或 DNS
    assert "沙箱" in note and "不是用户的网络坏了" in note
    assert "不要建议用户开 VPN" in note
    # 403 是换新链接，不是重试旧的
    assert "SignatureDoesNotMatch" in note and "别拿旧链接反复重试" in note


def test_expired_link_note_says_so():
    assert "已经过期" in _note(_url(issued_gmt=time.time() - 900))
