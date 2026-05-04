"""长轮询用的 per-key 异步队列管理。

设计原则:
- 每个等待方 (worker_id, task_id) 对应独立的 asyncio.Queue
- 投递方 put 到对应 queue, 等待方 get 阻塞到超时或拿到值
- 多个等待方等同一个 key, queue 是 FIFO 公平派发
- 用 RLock 保护 queue 字典以支持并发创建/销毁
- queue 取空后定期清理避免内存泄漏

为什么不用 asyncio.Event:
- Event 是广播,所有等待方都被唤醒,但只有一个能拿到值,其他白醒
- 一旦多任务并发投递,Event 可能合并通知导致丢消息
- Queue 天然解决这两个问题
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class WaiterRegistry(Generic[T]):
    """按 key 隔离的等待器集合。

    用法:
        registry = WaiterRegistry[MyValue]()

        # 投递方:
        await registry.put("worker-A", value)

        # 等待方:
        value = await registry.wait("worker-A", timeout_sec=60)
    """

    def __init__(self, name: str = "waiter") -> None:
        self._name = name
        # key -> queue
        self._queues: dict[str, asyncio.Queue[T]] = {}
        # 每个 key 当前等待者计数,用于清理判断
        self._waiter_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: T) -> bool:
        """向指定 key 的队列投递一个值。

        如果该 key 没有现存 queue,创建一个 (这样投递方可以早于等待方到达)。
        返回是否成功 (这里总是成功,但保留布尔以便未来支持限流)。
        """
        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[key] = queue
                self._waiter_counts.setdefault(key, 0)
            await queue.put(value)
        logger.debug(f"[{self._name}] put -> {key}")
        return True

    async def wait(self, key: str, timeout_sec: float) -> T | None:
        """等待 key 上的下一个投递,超时返回 None。"""
        async with self._lock:
            queue = self._queues.get(key)
            if queue is None:
                queue = asyncio.Queue()
                self._queues[key] = queue
                self._waiter_counts[key] = 0
            self._waiter_counts[key] = self._waiter_counts.get(key, 0) + 1

        try:
            value = await asyncio.wait_for(queue.get(), timeout=timeout_sec)
            logger.debug(f"[{self._name}] {key} 取到值")
            return value
        except asyncio.TimeoutError:
            logger.debug(f"[{self._name}] {key} 等待超时")
            return None
        finally:
            async with self._lock:
                self._waiter_counts[key] = max(0, self._waiter_counts.get(key, 1) - 1)
                # 如果没有等待者也没有积压消息,清理 queue 释放内存
                if (
                    self._waiter_counts[key] == 0
                    and key in self._queues
                    and self._queues[key].empty()
                ):
                    del self._queues[key]
                    del self._waiter_counts[key]

    async def has_pending(self, key: str) -> bool:
        """key 上是否有未消费的值。"""
        async with self._lock:
            queue = self._queues.get(key)
            return queue is not None and not queue.empty()

    async def waiter_count(self, key: str) -> int:
        """key 上当前的等待者数。"""
        async with self._lock:
            return self._waiter_counts.get(key, 0)

    async def stats(self) -> dict[str, dict[str, int]]:
        """返回所有 key 的状态,用于调试。"""
        async with self._lock:
            return {
                key: {
                    "waiters": self._waiter_counts.get(key, 0),
                    "pending": q.qsize(),
                }
                for key, q in self._queues.items()
            }


class GlobalQueue(Generic[T]):
    """单 key 等价物,用于事件流这种全局有序队列。"""

    def __init__(self, name: str = "global") -> None:
        self._name = name
        self._queue: asyncio.Queue[T] = asyncio.Queue()

    async def put(self, value: T) -> None:
        await self._queue.put(value)
        logger.debug(f"[{self._name}] enqueue (size={self._queue.qsize()})")

    async def wait(self, timeout_sec: float) -> T | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None

    def size(self) -> int:
        return self._queue.qsize()


# ===========================================================
# 全局单例 - 在 server 启动时创建,工具函数中引用
# ===========================================================

class Waiters:
    """所有 waiter 的集合,作为依赖注入容器。

    实例化一次,作为 lifespan context 传给所有工具。
    """

    def __init__(self) -> None:
        # worker_id -> 等待派给该 worker 的任务通知
        self.task_notifications: WaiterRegistry[dict[str, Any]] = WaiterRegistry(
            "task"
        )
        # task_id -> 等待 clarification 答复
        self.clarification_answers: WaiterRegistry[str] = WaiterRegistry(
            "clarification"
        )
        # task_id -> 等待 cleanup 信号 (无内容,只是触发)
        self.cleanup_signals: WaiterRegistry[bool] = WaiterRegistry("cleanup")
        # 协调者全局事件队列
        self.coord_events: GlobalQueue[dict[str, Any]] = GlobalQueue(
            "coord_events"
        )
