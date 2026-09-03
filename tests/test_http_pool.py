"""HTTP 模式的连接池

远程模式原先每个 JSON-RPC 请求都 new 一条 TranslationClient（内含 httpx.AsyncClient）
且从不 close，连接和 fd 只涨不落。现在按 API Key 复用，空闲够久才关——但 wait_* 一次
能挂几分钟，回收线绝不能把正在用的那条关掉。
"""

import asyncio

import pytest

from trans_mcp.http_server import ClientPool


@pytest.mark.asyncio
async def test_same_key_reuses_one_client():
    pool = ClientPool()
    async with pool.acquire("ft_a") as first:
        pass
    async with pool.acquire("ft_a") as second:
        pass
    assert first is second
    await pool.close_all()


@pytest.mark.asyncio
async def test_different_keys_never_share_a_client():
    pool = ClientPool()
    async with pool.acquire("ft_a") as a:
        async with pool.acquire("ft_b") as b:
            assert a is not b
            assert a.api_key == "ft_a" and b.api_key == "ft_b"
    await pool.close_all()


@pytest.mark.asyncio
async def test_in_flight_client_survives_a_due_recycle():
    """一个长请求挂着的同时另一个请求结束，回收线不能把在用的那条关掉"""
    pool = ClientPool()
    pool.IDLE_TTL = -1  # 每次归还都立刻到期

    started = asyncio.Event()
    release = asyncio.Event()
    holder = {}

    async def long_call():
        async with pool.acquire("ft_a") as client:
            holder["client"] = client
            started.set()
            await release.wait()

    task = asyncio.create_task(long_call())
    await started.wait()

    # 同一个 key 的另一次调用结束，触发一轮回收
    async with pool.acquire("ft_a"):
        pass
    # 长请求还挂着，它那条连接必须完好
    assert not holder["client"].client.is_closed

    release.set()
    await task
    # 最后一个用户走了，这才轮到回收
    assert holder["client"].client.is_closed


@pytest.mark.asyncio
async def test_idle_client_is_closed_when_due():
    pool = ClientPool()
    pool.IDLE_TTL = -1

    async with pool.acquire("ft_a") as client:
        pass
    # 归还即到期，这一轮就该被关掉并摘出池子
    assert client.client.is_closed
    async with pool.acquire("ft_a") as fresh:
        assert fresh is not client


@pytest.mark.asyncio
async def test_idle_pool_stays_under_the_cap():
    pool = ClientPool()
    pool.MAX_IDLE = 2

    for i in range(5):
        async with pool.acquire(f"ft_{i}"):
            pass
    assert len(pool._entries) <= pool.MAX_IDLE
    await pool.close_all()


@pytest.mark.asyncio
async def test_close_all_empties_the_pool():
    pool = ClientPool()
    async with pool.acquire("ft_a") as a:
        pass
    async with pool.acquire("ft_b") as b:
        pass

    await pool.close_all()
    assert pool._entries == {}
    assert a.client.is_closed and b.client.is_closed
