"""端到端集成测试。

这些测试验证一个完整的工作流从开始到结束都能正确运转,
包括:
- 派单 → worker 接活 → 提交 → 协调者收到事件 → 清理
- 多 worker 并发不互相干扰
- clarification 双向
- worker 崩溃 → 心跳超时 → 任务 abandoned
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from coop_server import store
from coop_server.config import HeartbeatConfig
from coop_server.coordinator_tools import CoordinatorTools
from coop_server.db import init_db, open_connection
from coop_server.heartbeat import HeartbeatMonitor
from coop_server.models import EventType, TaskStatus, WorkerStatus
from coop_server.waiters import Waiters
from coop_server.worker_tools import WorkerTools


@pytest_asyncio.fixture
async def system() -> AsyncIterator[dict]:
    """完整的子系统:DB + waiters + 工具集 + heartbeat。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        waiters = Waiters()
        wt = WorkerTools(path, waiters)
        ct = CoordinatorTools(path, waiters)
        hb_cfg = HeartbeatConfig(
            worker_interval_sec=1,
            timeout_sec=2,
            check_interval_sec=1,
        )
        hb = HeartbeatMonitor(path, hb_cfg, waiters)

        yield {
            "db_path": path,
            "waiters": waiters,
            "wt": wt,
            "ct": ct,
            "hb": hb,
        }
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


async def _drain_event_until(
    ct: CoordinatorTools, target_type: str, max_wait: float = 3
) -> dict:
    """轮询拉事件直到拿到指定类型。返回该事件 dict。"""
    deadline = asyncio.get_event_loop().time() + max_wait
    while asyncio.get_event_loop().time() < deadline:
        r = await ct.wait_for_event(timeout_sec=1)
        if r["status"] == "event" and r["event"]["type"] == target_type:
            return r["event"]
    raise AssertionError(f"等不到 {target_type} 事件")


# ===========================================================
# 主流程
# ===========================================================

class TestE2EHappyPath:
    async def test_full_lifecycle(self, system):
        """协调者派任务 → worker 接活 → 提交 → 协调者收事件 → 清理 → 关闭。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        # 1. worker 上线
        r = await wt.register_worker("worker-A", "host-A")
        assert r["ok"]

        # 2. 协调者看到 worker
        r = await ct.list_workers()
        assert len(r["workers"]) == 1
        assert r["workers"][0]["status"] == "idle"

        # 3. 协调者派任务
        r = await ct.publish_task(
            "T-001", "worker-A", "在 myapp 实现 X 函数,基于 main 分支 feature/x"
        )
        assert r["ok"]

        # 4. worker 接活 (无需等待,任务已就绪)
        r = await wt.wait_for_task("worker-A", timeout_sec=2)
        assert r["status"] == "assigned"
        task = r["task"]
        assert task["task_id"] == "T-001"

        # 5. worker 工作中,中途汇报进度
        r = await wt.report_progress("worker-A", "T-001", "已写完核心")
        assert r["ok"]

        # 6. worker 提交
        r = await wt.submit_work(
            "worker-A", "T-001", "myapp", "feature/x", "abc123", "完成"
        )
        assert r["ok"]
        assert r["task"]["status"] == "submitted"

        # 7. 协调者拉事件 (会有多个,我们只验证关键的)
        evt = await _drain_event_until(ct, "work_submitted")
        assert evt["task_id"] == "T-001"
        assert evt["project"] == "myapp"
        assert evt["branch"] == "feature/x"
        assert evt["commit_sha"] == "abc123"

        # 8. 协调者请求清理
        r = await ct.request_cleanup("T-001")
        assert r["ok"]

        # 9. worker 等待清理指令并 ack
        async def worker_cleanup():
            r1 = await wt.wait_for_cleanup_request(
                "worker-A", "T-001", timeout_sec=2
            )
            assert r1["status"] == "cleanup_requested"
            r2 = await wt.acknowledge_cleanup("worker-A", "T-001")
            return r2

        r = await worker_cleanup()
        assert r["task"]["status"] == "closed"

        # 10. 协调者拉到 cleanup_done 事件
        evt = await _drain_event_until(ct, "cleanup_done")
        assert evt["task_id"] == "T-001"

        # 11. 任务最终状态
        r = await ct.list_tasks(status="closed")
        assert len(r["tasks"]) == 1


class TestE2EClarification:
    async def test_clarification_round_trip(self, system):
        """worker 提问 → 协调者答复 → worker 拿到答复 → 继续干。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "做事")
        r = await wt.wait_for_task("worker-A", timeout_sec=1)
        assert r["status"] == "assigned"

        # worker 提问
        r = await wt.request_clarification(
            "worker-A", "T-001", "用 JWT 还是 OAuth?"
        )
        assert r["ok"]

        # 模拟双方异步:
        # worker 阻塞 wait_for_clarification, 同时协调者 respond
        async def coordinator():
            await asyncio.sleep(0.1)
            # 协调者收到 clarification 事件
            evt = await _drain_event_until(ct, "clarification_requested")
            assert evt["question"] == "用 JWT 还是 OAuth?"
            # 答复
            await ct.respond_clarification("T-001", "JWT")

        coord_task = asyncio.create_task(coordinator())
        r = await wt.wait_for_clarification("T-001", timeout_sec=3)
        await coord_task

        assert r["status"] == "answered"
        assert r["answer"] == "JWT"


class TestE2EMultiWorker:
    async def test_two_workers_independent(self, system):
        """两个 worker 互不干扰:派给 A 的任务不会唤醒 B 的等待。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        await wt.register_worker("worker-A", "host-A")
        await wt.register_worker("worker-B", "host-B")

        # B 进入 wait
        async def b_wait():
            return await wt.wait_for_task("worker-B", timeout_sec=2)

        b_task = asyncio.create_task(b_wait())
        await asyncio.sleep(0.1)

        # 派任务给 A
        await ct.publish_task("T-001", "worker-A", "for A")

        # B 应该超时,A 应该立刻拿到
        ra = await wt.wait_for_task("worker-A", timeout_sec=1)
        rb = await b_task

        assert ra["status"] == "assigned"
        assert ra["task"]["task_id"] == "T-001"
        assert rb["status"] == "no_task"  # B 等不到自己的

    async def test_two_workers_parallel_tasks(self, system):
        """两个 worker 并行处理各自任务。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        await wt.register_worker("worker-A", "host-A")
        await wt.register_worker("worker-B", "host-B")

        await ct.publish_task("T-001", "worker-A", "for A")
        await ct.publish_task("T-002", "worker-B", "for B")

        # 两个 worker 同时干
        async def do_work(worker_id, task_id):
            r = await wt.wait_for_task(worker_id, timeout_sec=2)
            assert r["task"]["task_id"] == task_id
            await wt.submit_work(
                worker_id, task_id, "p", "b", "sha-" + worker_id, "ok"
            )

        await asyncio.gather(
            do_work("worker-A", "T-001"),
            do_work("worker-B", "T-002"),
        )

        # 两个任务都到 submitted
        r = await ct.list_tasks(status="submitted")
        assert len(r["tasks"]) == 2


class TestE2EWorkerCrash:
    async def test_heartbeat_timeout_abandons_task(self, system):
        """worker 失联后,心跳监控自动把任务 abandon 并通知协调者。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]
        hb: HeartbeatMonitor = system["hb"]
        path = system["db_path"]

        await wt.register_worker("worker-A", "host-A")
        await ct.publish_task("T-001", "worker-A", "x")
        await wt.wait_for_task("worker-A", timeout_sec=1)
        # 现在 worker 在 working,任务在 in_progress

        # 模拟 worker 失联:把心跳时间改老
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).isoformat()
        conn = await open_connection(path)
        try:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
                (old, "worker-A"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # 触发一次心跳扫描
        count = await hb.check_once()
        assert count == 1

        # worker 应该是 OFFLINE,任务应该是 ABANDONED
        conn = await open_connection(path)
        try:
            w = await store.get_worker(conn, "worker-A")
            assert w is not None
            assert w.status == WorkerStatus.OFFLINE
            t = await store.get_task(conn, "T-001")
            assert t is not None
            assert t.status == TaskStatus.ABANDONED
        finally:
            await conn.close()

        # 协调者应该能拉到 worker_offline 和 task_abandoned 事件
        events_seen = set()
        for _ in range(5):
            r = await ct.wait_for_event(timeout_sec=1)
            if r["status"] == "event":
                events_seen.add(r["event"]["type"])
            if "worker_offline" in events_seen and "task_abandoned" in events_seen:
                break
        assert "worker_offline" in events_seen
        assert "task_abandoned" in events_seen


class TestE2EDispatchFeedback:
    async def test_blocked_then_redispatch_to_other_worker(self, system):
        """worker A 没工程 → blocked → 协调者重派给 B。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        await wt.register_worker("worker-A", "host-A")
        await wt.register_worker("worker-B", "host-B")

        # 派给 A
        await ct.publish_task("T-001", "worker-A", "在 mobile-app 做 X")

        # A 接到任务,模拟没工程,直接 blocked (在真实代码中由 Claude 决定)
        r = await wt.wait_for_task("worker-A", timeout_sec=1)
        assert r["status"] == "assigned"
        await wt.report_blocked("worker-A", "T-001", "本机未配置 mobile-app")

        # 协调者收到 blocked 事件
        evt = await _drain_event_until(ct, "worker_blocked")
        assert "mobile-app" in evt["reason"]

        # 协调者取消原任务,重派给 B
        await ct.cancel_task("T-001", "改派给 B")
        await ct.publish_task("T-002", "worker-B", "在 mobile-app 做 X (重派)")

        r = await wt.wait_for_task("worker-B", timeout_sec=1)
        assert r["status"] == "assigned"
        assert r["task"]["task_id"] == "T-002"


class TestE2EConcurrentDispatch:
    async def test_publish_during_long_wait(self, system):
        """publish 在 wait 阻塞中触发,worker 立即被唤醒。"""
        wt: WorkerTools = system["wt"]
        ct: CoordinatorTools = system["ct"]

        await wt.register_worker("worker-A", "host-A")

        # worker 进入 wait (长 timeout)
        async def worker_waits():
            return await wt.wait_for_task("worker-A", timeout_sec=10)

        wait_task = asyncio.create_task(worker_waits())
        await asyncio.sleep(0.2)  # 确保进入 wait

        # 派任务,应当立即唤醒
        start = asyncio.get_event_loop().time()
        await ct.publish_task("T-001", "worker-A", "x")
        r = await wait_task
        elapsed = asyncio.get_event_loop().time() - start

        assert r["status"] == "assigned"
        assert elapsed < 1  # 唤醒响应应当 < 1 秒
