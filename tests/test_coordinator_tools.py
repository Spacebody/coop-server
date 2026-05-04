"""coordinator_tools 单元测试。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from coop_server import store
from coop_server.coordinator_tools import CoordinatorTools
from coop_server.db import init_db, open_connection
from coop_server.models import EventType, TaskStatus
from coop_server.waiters import Waiters
from coop_server.worker_tools import WorkerTools


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[tuple[str, Waiters, CoordinatorTools, WorkerTools]]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        waiters = Waiters()
        ct = CoordinatorTools(path, waiters)
        wt = WorkerTools(path, waiters)
        yield path, waiters, ct, wt
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


class TestListWorkers:
    async def test_empty(self, setup):
        _, _, ct, _ = setup
        r = await ct.list_workers()
        assert r["workers"] == []

    async def test_after_register(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await wt.register_worker("worker-B", "host-B")
        r = await ct.list_workers()
        ids = {w["worker_id"] for w in r["workers"]}
        assert ids == {"worker-A", "worker-B"}


class TestPublishTask:
    async def test_publish_to_unknown_worker(self, setup):
        _, _, ct, _ = setup
        r = await ct.publish_task("T-001", "ghost", "do x")
        assert r["ok"] is False

    async def test_publish_to_offline_worker(self, setup):
        """OFFLINE worker 不能被派任务,即使存在。"""
        path, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")

        # 把 worker 状态改成 OFFLINE
        from coop_server.db import open_connection
        from coop_server.models import WorkerStatus
        from coop_server import store
        conn = await open_connection(path)
        try:
            await store.update_worker_status(
                conn, "worker-A", WorkerStatus.OFFLINE
            )
            await conn.commit()
        finally:
            await conn.close()

        r = await ct.publish_task("T-001", "worker-A", "do x")
        assert r["ok"] is False
        assert "OFFLINE" in r["error"] or "失联" in r["error"]

    async def test_publish_normal(self, setup):
        path, waiters, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")

        r = await ct.publish_task("T-001", "worker-A", "do x")
        assert r["ok"] is True
        assert r["task"]["task_id"] == "T-001"
        assert r["task"]["assignee"] == "worker-A"

        # waiter 被推动
        notif = await waiters.task_notifications.wait("worker-A", 0.5)
        assert notif is not None

    async def test_publish_duplicate(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        r1 = await ct.publish_task("T-001", "worker-A", "x")
        r2 = await ct.publish_task("T-001", "worker-A", "y")
        assert r1["ok"] is True
        assert r2["ok"] is False

    async def test_invalid_priority(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        r = await ct.publish_task(
            "T-001", "worker-A", "x", priority="urgent"
        )
        assert r["ok"] is False

    async def test_publish_with_options(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        r = await ct.publish_task(
            "T-001", "worker-A", "x",
            priority="high",
            parent_task_id="P-001",
            depends_on=["T-000"],
        )
        assert r["ok"] is True
        assert r["task"]["priority"] == "high"
        assert r["task"]["parent_task_id"] == "P-001"
        assert r["task"]["depends_on"] == ["T-000"]


class TestListTasks:
    async def test_filter_by_status(self, setup):
        path, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")

        # 把第二个任务搬到 in_progress
        await ct.publish_task("T-002", "worker-A", "y")
        conn = await open_connection(path)
        try:
            await store.claim_pending_task(conn, "worker-A")
            await conn.commit()
        finally:
            await conn.close()

        r1 = await ct.list_tasks(status="pending")
        r2 = await ct.list_tasks(status="in_progress")
        assert len(r1["tasks"]) == 1
        assert len(r2["tasks"]) == 1

    async def test_invalid_status(self, setup):
        _, _, ct, _ = setup
        r = await ct.list_tasks(status="zzz")
        assert r["ok"] is False


class TestCancelTask:
    async def test_cancel_pending(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")

        r = await ct.cancel_task("T-001", "no longer needed")
        assert r["ok"] is True
        assert r["task"]["status"] == "cancelled"
        assert "no longer" in r["task"]["cancel_reason"]

    async def test_cancel_unknown(self, setup):
        _, _, ct, _ = setup
        r = await ct.cancel_task("T-NONE")
        assert r["ok"] is False


class TestRespondClarification:
    async def test_respond_unknown(self, setup):
        _, _, ct, _ = setup
        r = await ct.respond_clarification("T-NONE", "x")
        assert r["ok"] is False

    async def test_respond_wakes_waiter(self, setup):
        path, waiters, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")

        # 模拟 worker 已发出 clarification
        conn = await open_connection(path)
        try:
            await store.create_clarification(
                conn, "T-001", "worker-A", "JWT?"
            )
            await conn.commit()
        finally:
            await conn.close()

        async def consumer():
            return await waiters.clarification_answers.wait("T-001", 1)

        consumer_task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)

        r = await ct.respond_clarification("T-001", "yes")
        assert r["ok"] is True

        ans = await consumer_task
        assert ans == "yes"

    async def test_empty_answer(self, setup):
        _, _, ct, _ = setup
        r = await ct.respond_clarification("T-001", "")
        assert r["ok"] is False


class TestRequestCleanup:
    async def test_cleanup_unknown(self, setup):
        _, _, ct, _ = setup
        r = await ct.request_cleanup("T-NONE")
        assert r["ok"] is False

    async def test_cleanup_pending_rejected(self, setup):
        _, _, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")
        r = await ct.request_cleanup("T-001")
        assert r["ok"] is False

    async def test_cleanup_after_submit(self, setup):
        path, waiters, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")

        # 推到 submitted
        conn = await open_connection(path)
        try:
            await store.claim_pending_task(conn, "worker-A")
            await store.submit_task(
                conn, "T-001", "worker-A", "p", "b", "s", ""
            )
            await conn.commit()
        finally:
            await conn.close()

        r = await ct.request_cleanup("T-001")
        assert r["ok"] is True
        assert r["task"]["status"] == "cleaning"

        # waiter 推动
        sig = await waiters.cleanup_signals.wait("T-001", 0.5)
        assert sig is True


class TestWaitForEvent:
    async def test_no_event_timeout(self, setup):
        _, _, ct, _ = setup
        r = await ct.wait_for_event(timeout_sec=1)
        assert r["status"] == "no_event"

    async def test_existing_event_returned(self, setup):
        path, _, ct, _ = setup
        # 直接往 DB 塞一个事件
        conn = await open_connection(path)
        try:
            await store.append_event(
                conn,
                event_type=EventType.PROGRESS,
                payload={"note": "hi"},
                task_id="T-001",
                worker_id="worker-A",
            )
            await conn.commit()
        finally:
            await conn.close()

        r = await ct.wait_for_event(timeout_sec=1)
        assert r["status"] == "event"
        assert r["event"]["type"] == "progress"

    async def test_event_arrives_during_wait(self, setup):
        path, waiters, ct, wt = setup
        await wt.register_worker("worker-A", "host-A")

        # 把 register 产生的事件先消费掉
        first = await ct.wait_for_event(timeout_sec=1)
        assert first["status"] == "event"
        assert first["event"]["type"] == "worker_registered"

        async def deliver():
            await asyncio.sleep(0.1)
            conn = await open_connection(path)
            try:
                await store.append_event(
                    conn,
                    event_type=EventType.PROGRESS,
                    payload={"note": "delayed"},
                    task_id="T-001",
                    worker_id="worker-A",
                )
                await conn.commit()
            finally:
                await conn.close()
            await waiters.coord_events.put({"_trigger": "test"})

        deliver_task = asyncio.create_task(deliver())
        r = await ct.wait_for_event(timeout_sec=3)
        await deliver_task

        assert r["status"] == "event"
        assert r["event"]["type"] == "progress"
        assert r["event"]["note"] == "delayed"
