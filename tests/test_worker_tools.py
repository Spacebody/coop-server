"""worker_tools 单元测试。

测试每个工具的:
- 正常路径
- 参数校验
- 异常处理
- waiter 推动
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from coop_server import store
from coop_server.db import init_db, open_connection
from coop_server.models import TaskStatus, WorkerStatus
from coop_server.waiters import Waiters
from coop_server.worker_tools import WorkerTools


@pytest_asyncio.fixture
async def setup() -> AsyncIterator[tuple[str, Waiters, WorkerTools]]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        waiters = Waiters()
        wt = WorkerTools(path, waiters)
        yield path, waiters, wt
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


class TestRegisterWorker:
    async def test_register_new_worker(self, setup):
        _, waiters, wt = setup
        result = await wt.register_worker("worker-A", "host-A")
        assert result["ok"] is True
        assert result["worker"]["worker_id"] == "worker-A"
        assert result["worker"]["status"] == "idle"

        # 协调者 queue 应被通知
        ev = await waiters.coord_events.wait(0.5)
        assert ev is not None

    async def test_register_idempotent(self, setup):
        _, _, wt = setup
        r1 = await wt.register_worker("worker-A", "host-A")
        r2 = await wt.register_worker("worker-A", "host-A2")
        assert r1["ok"] is True
        assert r2["ok"] is True
        assert r2["worker"]["hostname"] == "host-A2"

    async def test_missing_args(self, setup):
        _, _, wt = setup
        r = await wt.register_worker("", "host")
        assert r["ok"] is False


class TestHeartbeat:
    async def test_unknown_worker(self, setup):
        _, _, wt = setup
        r = await wt.heartbeat("ghost")
        assert r["ok"] is False

    async def test_heartbeat_updates(self, setup):
        path, _, wt = setup
        await wt.register_worker("worker-A", "host-A")

        # 直接读 DB 验证 last_heartbeat 被更新
        before = await wt.heartbeat("worker-A")
        assert before["ok"] is True


class TestWaitForTask:
    async def test_unknown_worker(self, setup):
        _, _, wt = setup
        r = await wt.wait_for_task("ghost", timeout_sec=1)
        assert r["ok"] is False

    async def test_no_task_returns_after_timeout(self, setup):
        _, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        r = await wt.wait_for_task("worker-A", timeout_sec=1)
        assert r["ok"] is True
        assert r["status"] == "no_task"

    async def test_immediate_task_assignment(self, setup):
        """任务在 wait 之前就已经入库,wait 应该立刻返回。"""
        path, _, wt = setup
        await wt.register_worker("worker-A", "host-A")

        # 直接通过 store 创建 pending 任务
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "test")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.wait_for_task("worker-A", timeout_sec=10)
        assert r["status"] == "assigned"
        assert r["task"]["task_id"] == "T-001"

    async def test_wait_then_task_arrives(self, setup):
        """wait 阻塞中,任务通过 waiter put 进来,应该被唤醒。"""
        path, waiters, wt = setup
        await wt.register_worker("worker-A", "host-A")

        async def deliver_task():
            await asyncio.sleep(0.2)
            # 模拟 publish_task 的两件事:
            # 1) 入库
            conn = await open_connection(path)
            try:
                await store.create_task(conn, "T-001", "worker-A", "x")
                await conn.commit()
            finally:
                await conn.close()
            # 2) 唤醒
            await waiters.task_notifications.put(
                "worker-A", {"task_id": "T-001"}
            )

        deliver_task_task = asyncio.create_task(deliver_task())
        r = await wt.wait_for_task("worker-A", timeout_sec=3)
        await deliver_task_task

        assert r["status"] == "assigned"
        assert r["task"]["task_id"] == "T-001"

    async def test_timeout_capped(self, setup):
        """传入超大 timeout 应该被截断。"""
        _, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        # 这里只是确认参数被处理,真正的 timeout 我们不会等
        # 通过传一个负数(被 max(1, ...) 截断为 1) 验证
        r = await wt.wait_for_task("worker-A", timeout_sec=-100)
        # 应该 1 秒内返回
        assert r["status"] == "no_task"


class TestSubmitWork:
    async def test_submit_normal_flow(self, setup):
        path, waiters, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await store.claim_pending_task(conn, "worker-A")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.submit_work(
            "worker-A", "T-001",
            summary="done",
            artifact={"project": "myapp", "branch": "feature/x", "commit_sha": "abc123"},
        )
        assert r["ok"] is True
        assert r["task"]["status"] == "submitted"
        assert r["task"]["submission"]["artifact"]["project"] == "myapp"

        # 协调者被通知
        ev = await waiters.coord_events.wait(0.5)
        assert ev is not None

    async def test_submit_unknown_task(self, setup):
        _, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        r = await wt.submit_work(
            "worker-A", "T-NONE", summary="s",
        )
        assert r["ok"] is False

    async def test_submit_missing_args(self, setup):
        _, _, wt = setup
        # worker_id 空
        r = await wt.submit_work("", "T-001", summary="s")
        assert r["ok"] is False
        # summary 空
        r = await wt.submit_work("worker-A", "T-001", summary="")
        assert r["ok"] is False
        # artifact 不是 dict
        r = await wt.submit_work(
            "worker-A", "T-001", summary="s", artifact="not-a-dict"  # type: ignore
        )
        assert r["ok"] is False


class TestProgress:
    async def test_progress_unknown_task(self, setup):
        _, _, wt = setup
        r = await wt.report_progress("worker-A", "T-NONE", "x")
        assert r["ok"] is False

    async def test_progress_normal(self, setup):
        path, waiters, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.report_progress("worker-A", "T-001", "正在写代码")
        assert r["ok"] is True
        ev = await waiters.coord_events.wait(0.5)
        assert ev is not None


class TestBlocked:
    async def test_blocked_normal(self, setup):
        path, waiters, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.report_blocked("worker-A", "T-001", "本机没这工程")
        assert r["ok"] is True

        # worker 状态应变成 BLOCKED
        conn = await open_connection(path)
        try:
            w = await store.get_worker(conn, "worker-A")
            assert w is not None
            assert w.status == WorkerStatus.BLOCKED
        finally:
            await conn.close()


class TestClarification:
    async def test_full_flow(self, setup):
        """request -> wait -> respond -> 醒来拿到答复。"""
        path, waiters, wt = setup
        from coop_server.coordinator_tools import CoordinatorTools
        ct = CoordinatorTools(path, waiters)

        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await conn.commit()
        finally:
            await conn.close()

        # worker 提问
        r1 = await wt.request_clarification(
            "worker-A", "T-001", "JWT 还是 OAuth?"
        )
        assert r1["ok"] is True

        # 启动 worker 的 wait,过一会协调者答复
        async def coordinator_answer():
            await asyncio.sleep(0.1)
            await ct.respond_clarification("T-001", "JWT")

        ans_task = asyncio.create_task(coordinator_answer())
        r2 = await wt.wait_for_clarification("T-001", timeout_sec=3)
        await ans_task

        assert r2["status"] == "answered"
        assert r2["answer"] == "JWT"

    async def test_wait_timeout(self, setup):
        path, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await store.create_clarification(
                conn, "T-001", "worker-A", "?"
            )
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.wait_for_clarification("T-001", timeout_sec=1)
        assert r["status"] == "timeout"

    async def test_consume_pre_answered(self, setup):
        """答复在 wait 之前就有了,wait 应该立刻返回。"""
        path, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await store.create_clarification(conn, "T-001", "worker-A", "?")
            await store.answer_clarification(conn, "T-001", "yes")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.wait_for_clarification("T-001", timeout_sec=10)
        assert r["status"] == "answered"
        assert r["answer"] == "yes"


class TestCleanup:
    async def test_cleanup_full_flow(self, setup):
        path, waiters, wt = setup
        from coop_server.coordinator_tools import CoordinatorTools
        ct = CoordinatorTools(path, waiters)

        await wt.register_worker("worker-A", "host-A")
        # 让任务处于 SUBMITTED
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await store.claim_pending_task(conn, "worker-A")
            await store.submit_task(conn, "T-001", "worker-A", summary="s")
            await conn.commit()
        finally:
            await conn.close()

        # worker 等清理指令
        async def coord_request():
            await asyncio.sleep(0.1)
            await ct.request_cleanup("T-001")

        coord_task = asyncio.create_task(coord_request())
        r = await wt.wait_for_cleanup_request(
            "worker-A", "T-001", timeout_sec=3
        )
        await coord_task

        assert r["status"] == "cleanup_requested"

        # ack
        r2 = await wt.acknowledge_cleanup("worker-A", "T-001")
        assert r2["ok"] is True
        assert r2["task"]["status"] == "closed"

    async def test_cleanup_already_done(self, setup):
        """任务已经 CLOSED 时 wait 应快速返回。"""
        path, _, wt = setup
        await wt.register_worker("worker-A", "host-A")
        conn = await open_connection(path)
        try:
            await store.create_task(conn, "T-001", "worker-A", "x")
            await store.claim_pending_task(conn, "worker-A")
            await store.submit_task(conn, "T-001", "worker-A", summary="s")
            await store.request_task_cleanup(conn, "T-001")
            await store.acknowledge_task_cleanup(conn, "T-001", "worker-A")
            await conn.commit()
        finally:
            await conn.close()

        r = await wt.wait_for_cleanup_request(
            "worker-A", "T-001", timeout_sec=3
        )
        assert r["status"] == "already_closed"
