"""心跳超时检测后台任务。

每隔 check_interval 扫描一次 workers 表,把 last_heartbeat 早于阈值
且状态非 OFFLINE 的 worker 标记为 OFFLINE。如果该 worker 有正在跑的任务,
连带把任务标记为 ABANDONED 并向协调者发事件。

设计要点:
- 用单连接复用,避免短连接开销
- 整个扫描在一个事务里,保证 worker/task/event 状态一致
- task 失败不应影响其他 worker 的处理 (异常不上抛,日志即可)
- 通过 cancel() 优雅停止
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import HeartbeatConfig
from .db import open_connection
from .models import EventType, TaskStatus, WorkerStatus, utcnow_iso
from . import store
from .waiters import Waiters

logger = logging.getLogger(__name__)


class HeartbeatMonitor:
    """后台心跳监控器。"""

    def __init__(
        self,
        db_path: str,
        config: HeartbeatConfig,
        waiters: Waiters,
    ) -> None:
        self._db_path = db_path
        self._config = config
        self._waiters = waiters
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """启动后台扫描任务。"""
        if self._task is not None and not self._task.done():
            logger.warning("HeartbeatMonitor 已在运行")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="heartbeat-monitor")
        logger.info(
            f"心跳监控启动: 每 {self._config.check_interval_sec}s 检查, "
            f"超时阈值 {self._config.timeout_sec}s"
        )

    async def stop(self) -> None:
        """优雅停止。"""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("HeartbeatMonitor 停止超时,强制取消")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("心跳监控已停止")

    async def _run(self) -> None:
        """扫描循环。"""
        while not self._stop_event.is_set():
            try:
                await self.check_once()
            except Exception:
                logger.exception("心跳扫描出错")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.check_interval_sec,
                )
            except asyncio.TimeoutError:
                # 正常超时,继续下一轮
                pass

    async def check_once(self) -> int:
        """执行一次扫描。

        Returns:
            标记 offline 的 worker 数量。
        """
        threshold = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self._config.timeout_sec)
        ).isoformat()

        conn = await open_connection(self._db_path)
        try:
            stale_workers = await store.find_stale_workers(conn, threshold)

            if not stale_workers:
                return 0

            for worker in stale_workers:
                logger.warning(
                    f"worker {worker.worker_id} 心跳超时 "
                    f"(last={worker.last_heartbeat}, threshold={threshold})"
                )

                # 标记 worker offline
                await store.update_worker_status(
                    conn, worker.worker_id, WorkerStatus.OFFLINE,
                    current_task_id=None,
                )

                # 发事件
                await store.append_event(
                    conn,
                    event_type=EventType.WORKER_OFFLINE,
                    payload={
                        "worker_id": worker.worker_id,
                        "hostname": worker.hostname,
                        "last_heartbeat": worker.last_heartbeat,
                    },
                    worker_id=worker.worker_id,
                )

                # 如果该 worker 正在干一个任务,把任务标 abandoned
                if worker.current_task_id:
                    task = await store.get_task(conn, worker.current_task_id)
                    if task and task.status == TaskStatus.IN_PROGRESS:
                        try:
                            await store.abandon_task(
                                conn,
                                worker.current_task_id,
                                f"worker {worker.worker_id} 心跳超时失联",
                            )
                            await store.append_event(
                                conn,
                                event_type=EventType.TASK_ABANDONED,
                                payload={
                                    "task_id": worker.current_task_id,
                                    "worker_id": worker.worker_id,
                                    "reason": "worker 失联",
                                },
                                task_id=worker.current_task_id,
                                worker_id=worker.worker_id,
                            )
                        except store.InvalidStateError as e:
                            logger.warning(
                                f"无法 abandon 任务 {worker.current_task_id}: {e}"
                            )

            await conn.commit()

            # 唤醒协调者的 wait_for_event
            for _ in stale_workers:
                # 每个 worker 至少发了一个 worker_offline 事件,
                # 加上可能的 task_abandoned, 用 has_pending 触发
                # 这里简单起见,投递一个空触发让协调者的 wait_for_event 重新拉
                await self._waiters.coord_events.put({"_trigger": "heartbeat"})

            logger.info(f"心跳扫描完成,标记 {len(stale_workers)} 个 worker offline")
            return len(stale_workers)

        finally:
            await conn.close()
