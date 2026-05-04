"""waiters 单元测试。

重点验证:
- 基础 put/wait
- 超时
- 多个等待者公平派发 (FIFO)
- 不同 key 之间隔离 (不会误唤醒)
- put 早于 wait 也能拿到 (不丢消息)
- 并发 put 和 wait 不丢值
- queue 清理 (无 leak)
"""
from __future__ import annotations

import asyncio

import pytest

from coop_server.waiters import GlobalQueue, WaiterRegistry, Waiters


class TestWaiterRegistryBasic:
    async def test_put_then_wait(self):
        reg = WaiterRegistry[str]()
        await reg.put("k1", "hello")
        v = await reg.wait("k1", timeout_sec=1)
        assert v == "hello"

    async def test_wait_timeout(self):
        reg = WaiterRegistry[str]()
        v = await reg.wait("k1", timeout_sec=0.1)
        assert v is None

    async def test_wait_then_put(self):
        """wait 早于 put 也能正确拿到。"""
        reg = WaiterRegistry[str]()

        async def producer():
            await asyncio.sleep(0.1)
            await reg.put("k1", "delayed")

        async def consumer():
            return await reg.wait("k1", timeout_sec=2)

        # 同时启动
        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())

        v = await consumer_task
        await producer_task
        assert v == "delayed"

    async def test_keys_isolated(self):
        """不同 key 互不影响:k1 put 不会唤醒 k2 的等待。"""
        reg = WaiterRegistry[str]()

        # k2 等待短超时
        async def k2_wait():
            return await reg.wait("k2", timeout_sec=0.3)

        async def k1_put():
            await asyncio.sleep(0.1)
            await reg.put("k1", "for k1")

        k2_task = asyncio.create_task(k2_wait())
        k1_task = asyncio.create_task(k1_put())

        v = await k2_task  # 应该超时返回 None
        await k1_task
        assert v is None

        # k1 上的值还在
        v1 = await reg.wait("k1", timeout_sec=0.5)
        assert v1 == "for k1"


class TestWaiterRegistryFIFO:
    async def test_fifo_order(self):
        """多个值依次 put,依次 wait 拿到的顺序与 put 相同。"""
        reg = WaiterRegistry[int]()
        for i in range(5):
            await reg.put("k", i)

        results = []
        for _ in range(5):
            v = await reg.wait("k", timeout_sec=1)
            results.append(v)
        assert results == [0, 1, 2, 3, 4]

    async def test_two_waiters_fair_dispatch(self):
        """两个等待者同时 wait 同一个 key,先到先得。

        put 两个值,两个等待者各拿到一个,不会一个拿两个。
        """
        reg = WaiterRegistry[int]()
        results = []

        async def waiter():
            v = await reg.wait("k", timeout_sec=2)
            results.append(v)

        # 启动两个等待者
        w1 = asyncio.create_task(waiter())
        w2 = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # 确保它们都进入 wait

        # 投递两个值
        await reg.put("k", 1)
        await reg.put("k", 2)

        await asyncio.gather(w1, w2)
        assert sorted(results) == [1, 2]


class TestWaiterRegistryConcurrency:
    async def test_concurrent_put_and_wait(self):
        """并发场景:N 个 producer 各 put M 个值, N 个 consumer 各 wait,
        总数应该匹配且不丢。"""
        reg = WaiterRegistry[int]()
        N_PRODUCERS = 5
        M_PER_PRODUCER = 10
        N_CONSUMERS = 5
        TOTAL = N_PRODUCERS * M_PER_PRODUCER

        async def producer(pid: int):
            for i in range(M_PER_PRODUCER):
                await reg.put("k", pid * 100 + i)

        async def consumer():
            received = []
            while True:
                v = await reg.wait("k", timeout_sec=0.5)
                if v is None:
                    return received
                received.append(v)

        # 启动 consumers (在 producers 之前,验证 wait-first 也工作)
        consumer_tasks = [
            asyncio.create_task(consumer()) for _ in range(N_CONSUMERS)
        ]

        # 启动 producers
        producer_tasks = [
            asyncio.create_task(producer(i)) for i in range(N_PRODUCERS)
        ]

        await asyncio.gather(*producer_tasks)
        results = await asyncio.gather(*consumer_tasks)

        all_received = [v for sub in results for v in sub]
        assert len(all_received) == TOTAL
        assert sorted(all_received) == sorted(
            pid * 100 + i for pid in range(N_PRODUCERS) for i in range(M_PER_PRODUCER)
        )

    async def test_no_message_loss_with_late_waiter(self):
        """put 在 wait 之前发生,值不丢。"""
        reg = WaiterRegistry[str]()
        # 先 put 三个
        for v in ["a", "b", "c"]:
            await reg.put("k", v)

        # 等待方稍后到来
        results = []
        for _ in range(3):
            v = await reg.wait("k", timeout_sec=0.5)
            results.append(v)
        assert results == ["a", "b", "c"]


class TestWaiterRegistryCleanup:
    async def test_cleanup_after_idle(self):
        """没有等待者也没积压消息时,key 应该被清掉 (避免内存泄漏)。"""
        reg = WaiterRegistry[str]()
        # put 一个,wait 拿走
        await reg.put("k", "x")
        v = await reg.wait("k", timeout_sec=1)
        assert v == "x"

        stats = await reg.stats()
        assert "k" not in stats  # 应该被清理

    async def test_no_cleanup_with_pending_messages(self):
        """有积压消息时不能清理。"""
        reg = WaiterRegistry[str]()
        await reg.put("k", "x")
        await reg.put("k", "y")
        v = await reg.wait("k", timeout_sec=1)
        assert v == "x"

        # 还有 "y" 在队列里
        stats = await reg.stats()
        assert "k" in stats
        assert stats["k"]["pending"] == 1

        v2 = await reg.wait("k", timeout_sec=1)
        assert v2 == "y"

    async def test_no_cleanup_with_active_waiters(self):
        """有等待者时不能清理。"""
        reg = WaiterRegistry[str]()

        async def waiter():
            return await reg.wait("k", timeout_sec=2)

        wt = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)

        # 检查
        stats = await reg.stats()
        assert "k" in stats
        assert stats["k"]["waiters"] == 1

        await reg.put("k", "x")
        v = await wt
        assert v == "x"

    async def test_timeout_cleanup(self):
        """超时退出后也应清理。"""
        reg = WaiterRegistry[str]()
        v = await reg.wait("k", timeout_sec=0.1)
        assert v is None
        stats = await reg.stats()
        assert "k" not in stats


class TestGlobalQueue:
    async def test_basic(self):
        q = GlobalQueue[int]()
        await q.put(1)
        await q.put(2)
        assert q.size() == 2

        v1 = await q.wait(timeout_sec=1)
        v2 = await q.wait(timeout_sec=1)
        assert v1 == 1
        assert v2 == 2

    async def test_timeout(self):
        q = GlobalQueue[str]()
        v = await q.wait(timeout_sec=0.1)
        assert v is None

    async def test_concurrent_consumers(self):
        """多消费者也是先到先得的 FIFO。"""
        q = GlobalQueue[int]()
        results = []

        async def consumer():
            v = await q.wait(timeout_sec=2)
            results.append(v)

        c1 = asyncio.create_task(consumer())
        c2 = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)

        await q.put(10)
        await q.put(20)

        await asyncio.gather(c1, c2)
        assert sorted(results) == [10, 20]


class TestWaitersContainer:
    """验证 Waiters 容器对象正常工作。"""

    async def test_components_isolated(self):
        w = Waiters()

        # 这几个都是独立的,互不影响
        await w.task_notifications.put("worker-A", {"task_id": "T-1"})
        await w.clarification_answers.put("T-1", "ans")
        await w.cleanup_signals.put("T-1", True)
        await w.coord_events.put({"type": "x"})

        # task notif 应该只在 worker-A 上
        v1 = await w.task_notifications.wait("worker-A", 1)
        v2 = await w.task_notifications.wait("worker-B", 0.1)
        assert v1 == {"task_id": "T-1"}
        assert v2 is None

        # 其他独立工作
        ans = await w.clarification_answers.wait("T-1", 1)
        assert ans == "ans"

        cu = await w.cleanup_signals.wait("T-1", 1)
        assert cu is True

        ev = await w.coord_events.wait(1)
        assert ev == {"type": "x"}
