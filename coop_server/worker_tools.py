"""worker 端 MCP 工具实现。

每个工具:
1. 拿请求 → 校验参数
2. 操作 store(原子事务)
3. 推动相关 waiter 队列
4. 必要时写事件
5. 返回结构化结果

设计原则:
- 工具函数本身不持有状态(无副作用),所有状态在 store/waiters
- 异常都被 store 层抛出(NotFoundError/ConflictError/InvalidStateError),
  这里捕获转换为用户友好的错误响应
- 长轮询的 timeout 是 worker 传来的,server 端做上限保护
"""
from __future__ import annotations

import logging
from typing import Any

from .db import open_connection
from .models import EventType, Worker
from . import store
from .waiters import Waiters

logger = logging.getLogger(__name__)

# 安全上限,防止恶意客户端传超长 timeout
MAX_TIMEOUT_SEC = 600  # 10 分钟


class WorkerTools:
    """worker 端工具的承载类。绑定到具体的 db_path 和 waiters。"""

    def __init__(self, db_path: str, waiters: Waiters) -> None:
        self._db_path = db_path
        self._waiters = waiters

    def _capped_timeout(self, t: int | float) -> float:
        return float(min(max(1, t), MAX_TIMEOUT_SEC))

    # =======================================================
    # 注册 / 心跳
    # =======================================================

    async def register_worker(
        self, worker_id: str, hostname: str
    ) -> dict[str, Any]:
        """worker 启动时声明上线。幂等:已存在的 worker 重新上线。"""
        if not worker_id or not isinstance(worker_id, str):
            return {"ok": False, "error": "worker_id 必填"}
        if not hostname or not isinstance(hostname, str):
            return {"ok": False, "error": "hostname 必填"}

        conn = await open_connection(self._db_path)
        try:
            worker = await store.upsert_worker(conn, worker_id, hostname)
            await store.append_event(
                conn,
                event_type=EventType.WORKER_REGISTERED,
                payload={"worker_id": worker_id, "hostname": hostname},
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        # 通知协调者
        await self._waiters.coord_events.put(
            {"_trigger": "worker_registered"}
        )
        logger.info(f"worker {worker_id}@{hostname} 已注册")
        return {"ok": True, "worker": worker.to_dict()}

    async def heartbeat(self, worker_id: str) -> dict[str, Any]:
        """worker 定期心跳。"""
        conn = await open_connection(self._db_path)
        try:
            try:
                await store.update_heartbeat(conn, worker_id)
                await conn.commit()
            except store.NotFoundError:
                return {"ok": False, "error": "worker 未注册,请先 register_worker"}
        finally:
            await conn.close()
        return {"ok": True}

    async def deregister_worker(self, worker_id: str) -> dict[str, Any]:
        """worker 主动下线。直接从 DB 删除该 worker 记录。

        若 worker 持有进行中的任务,会标记任务为 abandoned。
        """
        conn = await open_connection(self._db_path)
        try:
            deleted = await store.delete_worker(conn, worker_id)
            if not deleted:
                return {"ok": False, "error": "worker 不存在"}
            await store.append_event(
                conn,
                event_type=EventType.WORKER_OFFLINE,
                payload={"worker_id": worker_id, "reason": "deregistered"},
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put(
            {"_trigger": "worker_deregistered"}
        )
        logger.info(f"worker {worker_id} 主动下线")
        return {"ok": True}

    # =======================================================
    # 接任务 / 提交
    # =======================================================

    async def wait_for_task(
        self, worker_id: str, timeout_sec: int = 60
    ) -> dict[str, Any]:
        """长轮询等任务。

        逻辑:
        1. 先去 DB 看有没有现成的 pending 任务
        2. 没有就阻塞在 waiters.task_notifications[worker_id] 上
        3. 被唤醒后再扫一次 DB
        """
        timeout = self._capped_timeout(timeout_sec)

        conn = await open_connection(self._db_path)
        try:
            # 必须 worker 已注册
            worker = await store.get_worker(conn, worker_id)
            if worker is None:
                return {"ok": False, "error": "worker 未注册"}

            # 顺便更新心跳
            await store.update_heartbeat(conn, worker_id)
            await conn.commit()

            # 先看有没有现成的任务
            task = await store.claim_pending_task(conn, worker_id)
            if task is not None:
                await conn.commit()
                logger.info(f"{worker_id} 拿到任务 {task.task_id} (即时)")
                return {"ok": True, "status": "assigned", "task": task.to_dict()}
        finally:
            await conn.close()

        # 没有 → 长轮询
        notification = await self._waiters.task_notifications.wait(
            worker_id, timeout
        )

        # 醒来后(无论是收到通知还是超时)再扫一次,因为通知里只是触发信号,
        # 真正的任务在 DB 里
        conn = await open_connection(self._db_path)
        try:
            task = await store.claim_pending_task(conn, worker_id)
            if task is not None:
                await conn.commit()
                logger.info(f"{worker_id} 拿到任务 {task.task_id} (唤醒后)")
                return {"ok": True, "status": "assigned", "task": task.to_dict()}
        finally:
            await conn.close()

        # 超时或被误唤醒
        return {
            "ok": True,
            "status": "no_task",
            "hint": "再次调用 wait_for_task 继续等",
        }

    async def submit_work(
        self,
        worker_id: str,
        task_id: str,
        project: str,
        branch: str,
        commit_sha: str,
        summary: str,
    ) -> dict[str, Any]:
        """worker 提交任务结果。"""
        for name, val in [
            ("worker_id", worker_id),
            ("task_id", task_id),
            ("project", project),
            ("branch", branch),
            ("commit_sha", commit_sha),
        ]:
            if not val or not isinstance(val, str):
                return {"ok": False, "error": f"{name} 必填"}

        conn = await open_connection(self._db_path)
        try:
            try:
                task = await store.submit_task(
                    conn, task_id, worker_id,
                    project, branch, commit_sha, summary or "",
                )
            except store.NotFoundError:
                return {"ok": False, "error": f"任务 {task_id} 不存在"}
            except store.InvalidStateError as e:
                return {"ok": False, "error": str(e)}

            await store.append_event(
                conn,
                event_type=EventType.WORK_SUBMITTED,
                payload={
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "project": project,
                    "branch": branch,
                    "commit_sha": commit_sha,
                    "summary": summary,
                },
                task_id=task_id,
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put({"_trigger": "work_submitted"})
        logger.info(f"{worker_id} 提交了 {task_id}")
        return {"ok": True, "task": task.to_dict()}

    # =======================================================
    # 进度 / 阻塞 / clarification
    # =======================================================

    async def report_progress(
        self, worker_id: str, task_id: str, note: str
    ) -> dict[str, Any]:
        """阶段性汇报。"""
        conn = await open_connection(self._db_path)
        try:
            task = await store.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "任务不存在"}
            await store.append_event(
                conn,
                event_type=EventType.PROGRESS,
                payload={
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "note": note,
                },
                task_id=task_id,
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put({"_trigger": "progress"})
        return {"ok": True}

    async def report_blocked(
        self, worker_id: str, task_id: str, reason: str
    ) -> dict[str, Any]:
        """worker 卡住升级。"""
        conn = await open_connection(self._db_path)
        try:
            task = await store.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "任务不存在"}

            # 把 worker 状态更新为 blocked
            from .models import WorkerStatus
            try:
                await store.update_worker_status(
                    conn, worker_id, WorkerStatus.BLOCKED
                )
            except store.NotFoundError:
                return {"ok": False, "error": "worker 未注册"}

            await store.append_event(
                conn,
                event_type=EventType.WORKER_BLOCKED,
                payload={
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "reason": reason,
                },
                task_id=task_id,
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put({"_trigger": "worker_blocked"})
        logger.info(f"{worker_id} blocked on {task_id}: {reason}")
        return {"ok": True}

    async def request_clarification(
        self, worker_id: str, task_id: str, question: str
    ) -> dict[str, Any]:
        """worker 提问,等协调者答复。

        建立 clarification 记录,worker 之后调 wait_for_clarification 等答复。
        """
        conn = await open_connection(self._db_path)
        try:
            task = await store.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "任务不存在"}

            await store.create_clarification(conn, task_id, worker_id, question)
            await store.append_event(
                conn,
                event_type=EventType.CLARIFICATION_REQUESTED,
                payload={
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "question": question,
                },
                task_id=task_id,
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put({"_trigger": "clarification"})
        logger.info(f"{worker_id} 就 {task_id} 提问: {question}")
        return {
            "ok": True,
            "hint": "调用 wait_for_clarification 等协调者答复",
        }

    async def wait_for_clarification(
        self, task_id: str, timeout_sec: int = 300
    ) -> dict[str, Any]:
        """长轮询等 clarification 答复。"""
        timeout = self._capped_timeout(timeout_sec)

        # 先看 DB 里有没有已答复但还没消费的
        conn = await open_connection(self._db_path)
        try:
            answer = await store.consume_clarification_answer(conn, task_id)
            if answer is not None:
                await conn.commit()
                return {"ok": True, "status": "answered", "answer": answer}
        finally:
            await conn.close()

        # 没有 → 长轮询
        signal = await self._waiters.clarification_answers.wait(task_id, timeout)

        # 唤醒后再去 DB 取
        conn = await open_connection(self._db_path)
        try:
            answer = await store.consume_clarification_answer(conn, task_id)
            if answer is not None:
                await conn.commit()
                return {"ok": True, "status": "answered", "answer": answer}
        finally:
            await conn.close()

        return {"ok": True, "status": "timeout", "hint": "再次调用继续等"}

    # =======================================================
    # 清理
    # =======================================================

    async def wait_for_cleanup_request(
        self, worker_id: str, task_id: str, timeout_sec: int = 600
    ) -> dict[str, Any]:
        """worker 在提交后等待协调者下发清理指令。"""
        timeout = self._capped_timeout(timeout_sec)

        # 先看任务当前状态,可能已经在 CLEANING
        conn = await open_connection(self._db_path)
        try:
            task = await store.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "任务不存在"}
            from .models import TaskStatus
            if task.status == TaskStatus.CLEANING:
                return {
                    "ok": True, "status": "cleanup_requested",
                    "task_id": task_id,
                }
            if task.status == TaskStatus.CLOSED:
                return {
                    "ok": True, "status": "already_closed",
                    "hint": "已清理过,无需操作",
                }
            if task.status not in (TaskStatus.SUBMITTED, TaskStatus.IN_PROGRESS):
                return {
                    "ok": False,
                    "error": f"任务当前状态 {task.status.value} 不应等待清理",
                }
        finally:
            await conn.close()

        # 长轮询等
        signal = await self._waiters.cleanup_signals.wait(task_id, timeout)

        # 醒来后再查
        conn = await open_connection(self._db_path)
        try:
            task = await store.get_task(conn, task_id)
            if task and task.status == TaskStatus.CLEANING:
                return {
                    "ok": True, "status": "cleanup_requested",
                    "task_id": task_id,
                }
        finally:
            await conn.close()

        return {"ok": True, "status": "timeout", "hint": "再次调用继续等"}

    async def acknowledge_cleanup(
        self, worker_id: str, task_id: str
    ) -> dict[str, Any]:
        """worker 完成 worktree 清理后回执。"""
        conn = await open_connection(self._db_path)
        try:
            try:
                task = await store.acknowledge_task_cleanup(
                    conn, task_id, worker_id
                )
            except store.NotFoundError:
                return {"ok": False, "error": "任务不存在"}
            except store.InvalidStateError as e:
                return {"ok": False, "error": str(e)}

            await store.append_event(
                conn,
                event_type=EventType.CLEANUP_DONE,
                payload={"task_id": task_id, "worker_id": worker_id},
                task_id=task_id,
                worker_id=worker_id,
            )
            await conn.commit()
        finally:
            await conn.close()

        await self._waiters.coord_events.put({"_trigger": "cleanup_done"})
        logger.info(f"{worker_id} 已清理 {task_id}")
        return {"ok": True, "task": task.to_dict()}
