"""心跳超时检测后台任务。

每隔 check_interval 扫描一次 workers 表,把 last_heartbeat 早于阈值
且状态非 OFFLINE 的 worker 标记为 OFFLINE。如果该 worker 有正在跑的任务,
连带把任务标记为 ABANDONED 并向协调者发事件。

设计要点:
- 用单连接复用,避免短连接开销
- 整个扫描在一个事务里,保证 worker/task/event 状态一致
- task 失败不应影响其他 worker 的处理 (异常不上抛,日志即可)
- 通过 cancel() 优雅停止
- 检测系统休眠唤醒(笔记本合盖等),跳过该轮扫描以避免误标 OFFLINE
- 周期性 SQLite WAL checkpoint, 防止 wal 文件无限增长
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from .config import HeartbeatConfig
from .db import open_connection
from .models import EventType, TaskStatus, WorkerStatus, utcnow_iso
from . import store
from .waiters import Waiters

logger = logging.getLogger(__name__)

# 系统休眠检测阈值: 实际睡眠时长 - 预期睡眠时长 > 此值, 视为系统刚从休眠唤醒
WAKE_FROM_SLEEP_THRESHOLD_SEC = 30

# WAL checkpoint 间隔(轮数). 与 check_interval_sec 相乘得到实际间隔
# 默认 60 轮 * 30s = 30 分钟做一次 checkpoint
WAL_CHECKPOINT_EVERY_N_ROUNDS = 60


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
        self._round_count = 0  # 用于决定何时做 WAL checkpoint

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
        check_interval = self._config.check_interval_sec
        # 记录每轮开始的 wall clock, 用于检测系统休眠唤醒
        last_loop_start = time.time()

        while not self._stop_event.is_set():
            now = time.time()
            elapsed_since_last_loop = now - last_loop_start

            # 检测系统休眠唤醒:
            # 正常情况下相邻两次 loop 间隔约等于 check_interval。如果远超, 说明
            # 进程被 OS suspend 过(笔记本合盖、虚拟机暂停等)。此时所有 worker
            # 在 server 视角看起来心跳都"过时"了, 但实际它们没问题。
            # 处理方式: 续期所有 worker 的 last_heartbeat, 跳过本轮判定。
            if (
                last_loop_start > 0  # 不是第一轮
                and elapsed_since_last_loop > check_interval + WAKE_FROM_SLEEP_THRESHOLD_SEC
            ):
                logger.warning(
                    f"检测到系统休眠唤醒 (实际间隔 {elapsed_since_last_loop:.0f}s, "
                    f"预期 {check_interval}s), 续期所有 worker 的 last_heartbeat 后跳过本轮"
                )
                try:
                    await self._refresh_all_heartbeats()
                except Exception:
                    logger.exception("续期 worker 心跳失败")
            else:
                try:
                    await self.check_once()
                except Exception:
                    logger.exception("心跳扫描出错")

                # 周期性 WAL checkpoint
                self._round_count += 1
                if self._round_count >= WAL_CHECKPOINT_EVERY_N_ROUNDS:
                    self._round_count = 0
                    try:
                        await self._wal_checkpoint()
                    except Exception:
                        logger.exception("WAL checkpoint 失败")

            last_loop_start = time.time()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=check_interval,
                )
            except asyncio.TimeoutError:
                # 正常超时,继续下一轮
                pass

    async def _refresh_all_heartbeats(self) -> None:
        """系统休眠唤醒后调用: 把所有非 OFFLINE worker 的 last_heartbeat 续到现在。

        这样下一轮检查时, worker 有完整的 timeout_sec 时间窗口去重新心跳,
        不会因为 server 自己睡过头而被误判失联。
        """
        now = utcnow_iso()
        conn = await open_connection(self._db_path)
        try:
            cur = await conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE status != ?",
                (now, WorkerStatus.OFFLINE.value),
            )
            count = cur.rowcount
            await conn.commit()
            if count > 0:
                logger.info(f"已续期 {count} 个 worker 的心跳时间戳")
        finally:
            await conn.close()

    async def _wal_checkpoint(self) -> None:
        """主动执行 SQLite WAL checkpoint, 防止 wal 文件无限增长。

        TRUNCATE 模式: 把 wal 内容写入主库后, 把 wal 文件大小缩为 0。
        如果有读者持有旧 snapshot, checkpoint 会被部分阻塞但不会失败。
        """
        conn = await open_connection(self._db_path)
        try:
            cur = await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cur.fetchone()
            # row: (busy, log_pages, checkpointed_pages)
            if row:
                busy, log_pages, checkpointed = row
                if busy:
                    logger.debug(
                        f"WAL checkpoint busy=true (log={log_pages}, "
                        f"checkpointed={checkpointed}), 下次再试"
                    )
                else:
                    logger.debug(
                        f"WAL checkpoint OK (log={log_pages}, "
                        f"checkpointed={checkpointed})"
                    )
        finally:
            await conn.close()

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
