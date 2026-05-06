"""store 层单元测试。

覆盖:worker / task / event / clarification 的 CRUD 和状态机。
"""
from __future__ import annotations

import pytest

from coop_server import store
from coop_server.models import EventType, TaskStatus, WorkerStatus


# ===========================================================
# Worker
# ===========================================================

class TestWorker:
    async def test_upsert_worker_new(self, db_conn):
        w = await store.upsert_worker(db_conn, "worker-A", "host-A")
        assert w.worker_id == "worker-A"
        assert w.hostname == "host-A"
        assert w.status == WorkerStatus.IDLE
        assert w.current_task_id is None

    async def test_upsert_worker_existing(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        w = await store.upsert_worker(db_conn, "worker-A", "host-B")
        assert w.hostname == "host-B"
        assert w.status == WorkerStatus.IDLE

    async def test_heartbeat_updates_timestamp(self, db_conn):
        w1 = await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.update_heartbeat(db_conn, "worker-A")
        w2 = await store.get_worker(db_conn, "worker-A")
        assert w2 is not None
        # 心跳更新后,last_heartbeat 不会早于注册时间
        assert w2.last_heartbeat >= w1.last_heartbeat

    async def test_heartbeat_unknown_worker(self, db_conn):
        with pytest.raises(store.NotFoundError):
            await store.update_heartbeat(db_conn, "ghost")

    async def test_list_workers(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.upsert_worker(db_conn, "worker-B", "host-B")
        ws = await store.list_all_workers(db_conn)
        assert len(ws) == 2
        ids = {w.worker_id for w in ws}
        assert ids == {"worker-A", "worker-B"}

    async def test_find_stale_workers(self, db_conn):
        # 手动插入一个心跳时间很早的 worker
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        # 直接 SQL 改 last_heartbeat 模拟过期
        await db_conn.execute(
            "UPDATE workers SET last_heartbeat = '2000-01-01T00:00:00+00:00' "
            "WHERE worker_id = ?",
            ("worker-A",),
        )

        await store.upsert_worker(db_conn, "worker-B", "host-B")  # 新的

        stale = await store.find_stale_workers(
            db_conn, "2020-01-01T00:00:00+00:00"
        )
        assert len(stale) == 1
        assert stale[0].worker_id == "worker-A"

    async def test_find_stale_skips_offline(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.update_worker_status(db_conn, "worker-A", WorkerStatus.OFFLINE)
        await db_conn.execute(
            "UPDATE workers SET last_heartbeat = '2000-01-01T00:00:00+00:00'"
        )
        stale = await store.find_stale_workers(
            db_conn, "2020-01-01T00:00:00+00:00"
        )
        assert stale == []


# ===========================================================
# Task lifecycle
# ===========================================================

class TestTask:
    async def test_create_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        t = await store.create_task(
            db_conn,
            task_id="T-001",
            assignee="worker-A",
            description="测试任务",
            priority="high",
            depends_on=["T-000"],
        )
        assert t.task_id == "T-001"
        assert t.status == TaskStatus.PENDING
        assert t.priority == "high"
        assert t.depends_on == ["T-000"]

    async def test_create_duplicate_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        with pytest.raises(store.ConflictError):
            await store.create_task(db_conn, "T-001", "worker-A", "y")

    async def test_claim_pending_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")

        claimed = await store.claim_pending_task(db_conn, "worker-A")
        assert claimed is not None
        assert claimed.task_id == "T-001"
        assert claimed.status == TaskStatus.IN_PROGRESS

        worker = await store.get_worker(db_conn, "worker-A")
        assert worker is not None
        assert worker.status == WorkerStatus.WORKING
        assert worker.current_task_id == "T-001"

    async def test_claim_when_no_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        claimed = await store.claim_pending_task(db_conn, "worker-A")
        assert claimed is None

    async def test_claim_only_own_tasks(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.upsert_worker(db_conn, "worker-B", "host-B")
        await store.create_task(db_conn, "T-001", "worker-A", "x")

        # B 不应该拿到 A 的任务
        claimed = await store.claim_pending_task(db_conn, "worker-B")
        assert claimed is None

    async def test_claim_oldest_first(self, db_conn):
        """有多个 pending 时,先派最早的。"""
        import asyncio
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "first")
        await asyncio.sleep(0.01)  # 确保 dispatched_at 不同
        await store.create_task(db_conn, "T-002", "worker-A", "second")

        claimed = await store.claim_pending_task(db_conn, "worker-A")
        assert claimed is not None
        assert claimed.task_id == "T-001"

    async def test_submit_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")

        submitted = await store.submit_task(
            db_conn, "T-001", "worker-A",
            summary="done",
            artifact={"project": "myapp", "branch": "feature/x", "commit_sha": "abc123"},
        )
        assert submitted.status == TaskStatus.SUBMITTED
        assert submitted.submitted_artifact["project"] == "myapp"
        assert submitted.submitted_artifact["commit_sha"] == "abc123"
        assert submitted.submitted_summary == "done"

        # worker 回到 idle
        worker = await store.get_worker(db_conn, "worker-A")
        assert worker is not None
        assert worker.status == WorkerStatus.IDLE
        assert worker.current_task_id is None

    async def test_submit_unknown_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        with pytest.raises(store.NotFoundError):
            await store.submit_task(
                db_conn, "T-NONE", "worker-A", summary="s",
            )

    async def test_submit_other_workers_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.upsert_worker(db_conn, "worker-B", "host-B")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")

        with pytest.raises(store.InvalidStateError):
            await store.submit_task(
                db_conn, "T-001", "worker-B", summary="s",
            )

    async def test_submit_pending_task_rejected(self, db_conn):
        """未 claim 直接 submit 应该被拒绝。"""
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")

        with pytest.raises(store.InvalidStateError):
            await store.submit_task(
                db_conn, "T-001", "worker-A", summary="s",
            )

    async def test_submit_idempotent(self, db_conn):
        """重复 submit 同一任务不抛异常,返回当前状态。"""
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")
        first = await store.submit_task(
            db_conn, "T-001", "worker-A",
            summary="s", artifact={"commit_sha": "abc"},
        )
        # 再调一次应该不抛
        second = await store.submit_task(
            db_conn, "T-001", "worker-A",
            summary="s2", artifact={"commit_sha": "def"},
        )
        # 第二次的内容不应覆盖第一次 (幂等保护)
        assert second.submitted_artifact == first.submitted_artifact
        assert first.submitted_artifact["commit_sha"] == "abc"

    async def test_request_and_acknowledge_cleanup(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")
        await store.submit_task(
            db_conn, "T-001", "worker-A", summary="s",
        )

        t = await store.request_task_cleanup(db_conn, "T-001")
        assert t.status == TaskStatus.CLEANING

        t2 = await store.acknowledge_task_cleanup(db_conn, "T-001", "worker-A")
        assert t2.status == TaskStatus.CLOSED

    async def test_cleanup_wrong_state(self, db_conn):
        """状态不是 SUBMITTED 不能 request_cleanup。"""
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        # 还在 PENDING 状态
        with pytest.raises(store.InvalidStateError):
            await store.request_task_cleanup(db_conn, "T-001")

    async def test_cancel_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")

        t = await store.cancel_task(db_conn, "T-001", reason="不需要了")
        assert t.status == TaskStatus.CANCELLED
        assert t.cancel_reason == "不需要了"

        # worker 回到 idle
        worker = await store.get_worker(db_conn, "worker-A")
        assert worker is not None
        assert worker.current_task_id is None

    async def test_cancel_already_closed(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")
        await store.submit_task(db_conn, "T-001", "worker-A", summary="s")
        await store.request_task_cleanup(db_conn, "T-001")
        await store.acknowledge_task_cleanup(db_conn, "T-001", "worker-A")

        with pytest.raises(store.InvalidStateError):
            await store.cancel_task(db_conn, "T-001")

    async def test_abandon_task(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.claim_pending_task(db_conn, "worker-A")

        t = await store.abandon_task(db_conn, "T-001", "worker 失联")
        assert t.status == TaskStatus.ABANDONED
        assert t.abandon_reason == "worker 失联"

    async def test_list_tasks_by_status(self, db_conn):
        await store.upsert_worker(db_conn, "worker-A", "host-A")
        await store.create_task(db_conn, "T-001", "worker-A", "x")
        await store.create_task(db_conn, "T-002", "worker-A", "y")
        await store.claim_pending_task(db_conn, "worker-A")

        pending = await store.list_tasks(db_conn, status=TaskStatus.PENDING)
        in_prog = await store.list_tasks(db_conn, status=TaskStatus.IN_PROGRESS)
        assert len(pending) == 1
        assert len(in_prog) == 1


# ===========================================================
# Event
# ===========================================================

class TestEvent:
    async def test_append_and_fetch_event(self, db_conn):
        eid = await store.append_event(
            db_conn,
            event_type=EventType.WORKER_REGISTERED,
            payload={"hostname": "host-A"},
            worker_id="worker-A",
        )
        assert eid > 0

        ev = await store.fetch_next_event(db_conn)
        assert ev is not None
        assert ev.event_id == eid
        assert ev.event_type == EventType.WORKER_REGISTERED
        assert ev.worker_id == "worker-A"
        assert ev.payload == {"hostname": "host-A"}

    async def test_fetch_when_empty(self, db_conn):
        ev = await store.fetch_next_event(db_conn)
        assert ev is None

    async def test_fetch_consumes_event(self, db_conn):
        await store.append_event(
            db_conn, EventType.PROGRESS, {"note": "hi"}, "T-001", "worker-A"
        )
        ev1 = await store.fetch_next_event(db_conn)
        assert ev1 is not None
        # 第二次取应该是空
        ev2 = await store.fetch_next_event(db_conn)
        assert ev2 is None

    async def test_fetch_oldest_first(self, db_conn):
        await store.append_event(db_conn, EventType.PROGRESS, {"n": 1}, "T-1")
        await store.append_event(db_conn, EventType.PROGRESS, {"n": 2}, "T-2")

        ev1 = await store.fetch_next_event(db_conn)
        ev2 = await store.fetch_next_event(db_conn)
        assert ev1 is not None and ev2 is not None
        assert ev1.payload["n"] == 1
        assert ev2.payload["n"] == 2

    async def test_count_unconsumed(self, db_conn):
        assert await store.count_unconsumed_events(db_conn) == 0
        await store.append_event(db_conn, EventType.PROGRESS, {})
        await store.append_event(db_conn, EventType.PROGRESS, {})
        assert await store.count_unconsumed_events(db_conn) == 2
        await store.fetch_next_event(db_conn)
        assert await store.count_unconsumed_events(db_conn) == 1


# ===========================================================
# Clarification
# ===========================================================

class TestClarification:
    async def test_create_and_answer(self, db_conn):
        c = await store.create_clarification(
            db_conn, "T-001", "worker-A", "JWT 还是 OAuth?"
        )
        assert c.answer is None
        assert c.answered_at is None

        c2 = await store.answer_clarification(db_conn, "T-001", "JWT")
        assert c2.answer == "JWT"
        assert c2.answered_at is not None

    async def test_answer_unknown(self, db_conn):
        with pytest.raises(store.NotFoundError):
            await store.answer_clarification(db_conn, "T-NONE", "x")

    async def test_consume_answer_deletes(self, db_conn):
        await store.create_clarification(db_conn, "T-001", "worker-A", "?")
        await store.answer_clarification(db_conn, "T-001", "yes")

        ans = await store.consume_clarification_answer(db_conn, "T-001")
        assert ans == "yes"

        # 第二次应该是 None
        ans2 = await store.consume_clarification_answer(db_conn, "T-001")
        assert ans2 is None

    async def test_consume_unanswered(self, db_conn):
        await store.create_clarification(db_conn, "T-001", "worker-A", "?")
        ans = await store.consume_clarification_answer(db_conn, "T-001")
        assert ans is None

    async def test_replace_pending_clarification(self, db_conn):
        """worker 重复发同一任务的 clarification,后者覆盖前者。"""
        await store.create_clarification(db_conn, "T-001", "worker-A", "Q1")
        await store.create_clarification(db_conn, "T-001", "worker-A", "Q2")

        c = await store.get_clarification(db_conn, "T-001")
        assert c is not None
        assert c.question == "Q2"

    async def test_answer_already_answered(self, db_conn):
        """已答复但还没消费时,不允许再次 answer。"""
        await store.create_clarification(db_conn, "T-001", "worker-A", "?")
        await store.answer_clarification(db_conn, "T-001", "yes")

        # 现在记录里 answer 不为 NULL,再次 answer 走 WHERE answer IS NULL 失败
        with pytest.raises(store.NotFoundError):
            await store.answer_clarification(db_conn, "T-001", "no")
