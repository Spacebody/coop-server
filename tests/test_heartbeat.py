"""心跳监控测试。

重点验证:
- 心跳超时的 worker 被标记 offline
- 超时 worker 正在干的任务被标记 abandoned
- 事件被正确写入
- 协调者的 wait queue 被唤醒
- 后台任务能优雅启停
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from coop_server import store
from coop_server.config import HeartbeatConfig
from coop_server.db import init_db, open_connection
from coop_server.heartbeat import HeartbeatMonitor
from coop_server.models import EventType, TaskStatus, WorkerStatus
from coop_server.waiters import Waiters


@pytest.fixture
def hb_config():
    """快速心跳配置,用于测试。"""
    return HeartbeatConfig(
        worker_interval_sec=1,
        timeout_sec=2,
        check_interval_sec=1,
    )


@pytest.fixture
def waiters():
    return Waiters()


@pytest.fixture
async def db_path(tmp_path):
    """每个测试一个临时 DB 文件。"""
    p = str(tmp_path / "test.db")
    await init_db(p)
    return p


async def _make_worker(db_path: str, worker_id: str, last_heartbeat: str | None = None):
    """辅助:创建 worker,可选地手动设置 last_heartbeat (模拟过期)。"""
    conn = await open_connection(db_path)
    try:
        await store.upsert_worker(conn, worker_id, "test-host")
        if last_heartbeat:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
                (last_heartbeat, worker_id),
            )
        await conn.commit()
    finally:
        await conn.close()


async def _make_in_progress_task(db_path: str, worker_id: str, task_id: str):
    """辅助:给 worker 派一个任务并标记 in_progress。"""
    conn = await open_connection(db_path)
    try:
        await store.create_task(conn, task_id, worker_id, "test description")
        await store.claim_pending_task(conn, worker_id)
        await conn.commit()
    finally:
        await conn.close()


class TestHeartbeatCheckOnce:
    async def test_no_stale_workers(self, db_path, hb_config, waiters):
        """所有 worker 心跳都新鲜,不应该有任何处理。"""
        await _make_worker(db_path, "worker-A")  # 现在的时间

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        count = await monitor.check_once()
        assert count == 0

    async def test_stale_worker_marked_offline(self, db_path, hb_config, waiters):
        """超时 worker 被标记 offline。"""
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        await _make_worker(db_path, "worker-A", last_heartbeat=old_time)

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        count = await monitor.check_once()
        assert count == 1

        conn = await open_connection(db_path)
        try:
            w = await store.get_worker(conn, "worker-A")
            assert w is not None
            assert w.status == WorkerStatus.OFFLINE
        finally:
            await conn.close()

    async def test_offline_event_emitted(self, db_path, hb_config, waiters):
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        await _make_worker(db_path, "worker-A", last_heartbeat=old_time)

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.check_once()

        conn = await open_connection(db_path)
        try:
            ev = await store.fetch_next_event(conn)
            assert ev is not None
            assert ev.event_type == EventType.WORKER_OFFLINE
            assert ev.worker_id == "worker-A"
        finally:
            await conn.close()

    async def test_in_progress_task_abandoned(self, db_path, hb_config, waiters):
        """worker 失联,它的任务被标记 abandoned。"""
        await _make_worker(db_path, "worker-A")
        await _make_in_progress_task(db_path, "worker-A", "T-001")

        # 把心跳调老
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        conn = await open_connection(db_path)
        try:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
                (old_time, "worker-A"),
            )
            await conn.commit()
        finally:
            await conn.close()

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.check_once()

        conn = await open_connection(db_path)
        try:
            t = await store.get_task(conn, "T-001")
            assert t is not None
            assert t.status == TaskStatus.ABANDONED
            assert t.abandon_reason is not None and "失联" in t.abandon_reason
        finally:
            await conn.close()

    async def test_abandoned_task_event_emitted(self, db_path, hb_config, waiters):
        await _make_worker(db_path, "worker-A")
        await _make_in_progress_task(db_path, "worker-A", "T-001")

        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        conn = await open_connection(db_path)
        try:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
                (old_time, "worker-A"),
            )
            await conn.commit()
        finally:
            await conn.close()

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.check_once()

        # 应该有两个事件:worker_offline 和 task_abandoned
        conn = await open_connection(db_path)
        try:
            ev1 = await store.fetch_next_event(conn)
            ev2 = await store.fetch_next_event(conn)
            assert ev1 is not None and ev2 is not None
            types = {ev1.event_type, ev2.event_type}
            assert EventType.WORKER_OFFLINE in types
            assert EventType.TASK_ABANDONED in types
        finally:
            await conn.close()

    async def test_already_offline_worker_skipped(self, db_path, hb_config, waiters):
        """已经 OFFLINE 状态的 worker 不会被重复处理。"""
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        await _make_worker(db_path, "worker-A", last_heartbeat=old_time)

        # 先标 offline
        conn = await open_connection(db_path)
        try:
            await store.update_worker_status(conn, "worker-A", WorkerStatus.OFFLINE)
            await conn.commit()
        finally:
            await conn.close()

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        count = await monitor.check_once()
        assert count == 0  # 不应再处理

    async def test_coord_queue_notified(self, db_path, hb_config, waiters):
        """有 worker 被标记 offline 时,协调者 queue 应被推动。"""
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        await _make_worker(db_path, "worker-A", last_heartbeat=old_time)

        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.check_once()

        # 协调者 queue 应该有触发信号
        v = await waiters.coord_events.wait(timeout_sec=1)
        assert v is not None


class TestHeartbeatLifecycle:
    async def test_start_and_stop(self, db_path, hb_config, waiters):
        """正常启动和停止。"""
        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.start()
        assert monitor._task is not None
        assert not monitor._task.done()

        await monitor.stop()
        assert monitor._task is None

    async def test_periodic_check(self, db_path, waiters):
        """运行一段时间能多次扫描。"""
        # 用更快的间隔
        cfg = HeartbeatConfig(
            worker_interval_sec=1,
            timeout_sec=2,
            check_interval_sec=1,  # 每秒扫
        )

        # 准备一个超时 worker
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        await _make_worker(db_path, "worker-A", last_heartbeat=old_time)

        monitor = HeartbeatMonitor(db_path, cfg, waiters)
        await monitor.start()

        # 等够一次扫描周期
        await asyncio.sleep(1.5)

        # worker 应该已被标 offline
        conn = await open_connection(db_path)
        try:
            w = await store.get_worker(conn, "worker-A")
            assert w is not None
            assert w.status == WorkerStatus.OFFLINE
        finally:
            await conn.close()

        await monitor.stop()

    async def test_double_start_safe(self, db_path, hb_config, waiters):
        """重复 start 不出错。"""
        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.start()
        await monitor.start()  # 不应抛异常
        await monitor.stop()

    async def test_stop_without_start(self, db_path, hb_config, waiters):
        """没启动直接 stop 不出错。"""
        monitor = HeartbeatMonitor(db_path, hb_config, waiters)
        await monitor.stop()  # 不应抛异常
