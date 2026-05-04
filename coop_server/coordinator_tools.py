"""协调者端 MCP 工具实现。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .db import open_connection
from .models import TaskStatus, WorkerStatus
from . import store
from .waiters import Waiters

logger = logging.getLogger(__name__)

MAX_TIMEOUT_SEC = 600


class CoordinatorTools:
    def __init__(self, db_path: str, waiters: Waiters) -> None:
        self._db_path = db_path
        self._waiters = waiters

    def _capped_timeout(self, t: int | float) -> float:
        return float(min(max(1, t), MAX_TIMEOUT_SEC))

    # =======================================================
    # 查询
    # =======================================================

    async def list_workers(
        self, online_only: bool = True
    ) -> dict[str, Any]:
        """查看 worker 列表。

        Args:
            online_only: 只显示在线 (idle/working/blocked) 的 worker, 默认 True。
                         False 时也包含 OFFLINE 状态的(用于诊断)。
        """
        conn = await open_connection(self._db_path)
        try:
            workers = await store.list_all_workers(
                conn, online_only=online_only
            )
        finally:
            await conn.close()
        return {
            "ok": True,
            "workers": [w.to_dict() for w in workers],
        }

    async def prune_offline_workers(self) -> dict[str, Any]:
        """删除所有 OFFLINE 状态的 worker 记录。"""
        conn = await open_connection(self._db_path)
        try:
            deleted = await store.delete_offline_workers(conn)
            await conn.commit()
        finally:
            await conn.close()
        logger.info(f"清理了 {deleted} 个 OFFLINE worker")
        return {"ok": True, "deleted_count": deleted}

    async def list_tasks(self, status: str | None = None) -> dict[str, Any]:
        """查看任务列表,可按状态过滤。"""
        status_enum = None
        if status:
            try:
                status_enum = TaskStatus(status)
            except ValueError:
                return {"ok": False, "error": f"无效的状态: {status}"}

        conn = await open_connection(self._db_path)
        try:
            tasks = await store.list_tasks(conn, status=status_enum)
        finally:
            await conn.close()
        return {
            "ok": True,
            "tasks": [t.to_dict() for t in tasks],
        }

    # =======================================================
    # 派单
    # =======================================================

    async def publish_task(
        self,
        task_id: str,
        assignee: str,
        description: str,
        priority: str = "normal",
        parent_task_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """协调者发布任务。

        派单不依赖 worker 能力——直接派给 assignee。
        如果 worker 本机没有任务涉及的工程,worker 会自己 report_blocked。
        """
        for name, val in [
            ("task_id", task_id),
            ("assignee", assignee),
            ("description", description),
        ]:
            if not val or not isinstance(val, str):
                return {"ok": False, "error": f"{name} 必填"}
        if priority not in ("high", "normal", "low"):
            return {"ok": False, "error": "priority 必须是 high/normal/low"}

        conn = await open_connection(self._db_path)
        try:
            # 校验 assignee 存在且在线(避免派给已失联的 worker 永远卡 pending)
            worker = await store.get_worker(conn, assignee)
            if worker is None:
                return {
                    "ok": False,
                    "error": f"worker {assignee} 不存在,请先 list_workers 查看",
                }
            if worker.status == WorkerStatus.OFFLINE:
                return {
                    "ok": False,
                    "error": (
                        f"worker {assignee} 已失联 (OFFLINE),不能派任务。"
                        f"该 worker 上次心跳: {worker.last_heartbeat}。"
                        f"请用 list_workers 看在线 worker。"
                    ),
                }

            try:
                task = await store.create_task(
                    conn,
                    task_id=task_id,
                    assignee=assignee,
                    description=description,
                    priority=priority,
                    parent_task_id=parent_task_id,
                    depends_on=depends_on,
                )
            except store.ConflictError as e:
                return {"ok": False, "error": str(e)}

            await conn.commit()
        finally:
            await conn.close()

        # 唤醒该 worker 的 wait_for_task
        await self._waiters.task_notifications.put(
            assignee,
            {"_trigger": "new_task", "task_id": task_id},
        )

        logger.info(f"派发 {task_id} -> {assignee} (priority={priority})")
        return {"ok": True, "task": task.to_dict()}

    async def cancel_task(
        self, task_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """协调者取消任务。"""
        conn = await open_connection(self._db_path)
        try:
            try:
                task = await store.cancel_task(conn, task_id, reason)
            except store.NotFoundError:
                return {"ok": False, "error": "任务不存在"}
            except store.InvalidStateError as e:
                return {"ok": False, "error": str(e)}
            await conn.commit()
        finally:
            await conn.close()

        logger.info(f"任务 {task_id} 已取消: {reason}")
        return {"ok": True, "task": task.to_dict()}

    # =======================================================
    # 答复 / 清理
    # =======================================================

    async def respond_clarification(
        self, task_id: str, answer: str
    ) -> dict[str, Any]:
        """协调者答复 worker 的提问。"""
        if not answer or not isinstance(answer, str):
            return {"ok": False, "error": "answer 必填"}

        conn = await open_connection(self._db_path)
        try:
            try:
                clar = await store.answer_clarification(conn, task_id, answer)
            except store.NotFoundError:
                return {
                    "ok": False,
                    "error": f"任务 {task_id} 没有待答复的 clarification",
                }
            await conn.commit()
        finally:
            await conn.close()

        # 唤醒 worker 的 wait_for_clarification
        await self._waiters.clarification_answers.put(task_id, answer)
        logger.info(f"已答复 {task_id} 的 clarification")
        return {"ok": True}

    async def request_cleanup(self, task_id: str) -> dict[str, Any]:
        """协调者通知 worker 清理 worktree。"""
        conn = await open_connection(self._db_path)
        try:
            try:
                task = await store.request_task_cleanup(conn, task_id)
            except store.NotFoundError:
                return {"ok": False, "error": "任务不存在"}
            except store.InvalidStateError as e:
                return {"ok": False, "error": str(e)}
            await conn.commit()
        finally:
            await conn.close()

        # 唤醒 worker 的 wait_for_cleanup_request
        await self._waiters.cleanup_signals.put(task_id, True)
        logger.info(f"请求清理 {task_id}")
        return {"ok": True, "task": task.to_dict()}

    # =======================================================
    # 事件流
    # =======================================================

    async def wait_for_event(
        self, timeout_sec: int = 60
    ) -> dict[str, Any]:
        """协调者长轮询接收 worker 事件。

        coord_events queue 里的 trigger 只是"去 DB 扫一次"的信号,
        不携带事件内容。每次拿到 DB 事件后,清空 trigger 队列避免
        下次 wait 因残留 trigger 立即返回但 DB 里没事件。
        """
        timeout = self._capped_timeout(timeout_sec)

        # 先扫 DB
        conn = await open_connection(self._db_path)
        try:
            ev = await store.fetch_next_event(conn)
            if ev is not None:
                await conn.commit()
                # 清空残留 trigger,因为它们是为这个事件准备的
                await self._drain_triggers()
                logger.debug(
                    f"协调者取走事件 {ev.event_id}: {ev.event_type.value}"
                )
                return {"ok": True, "status": "event", "event": ev.to_dict()}
        finally:
            await conn.close()

        # 没有 → 等触发信号
        signal = await self._waiters.coord_events.wait(timeout)

        # 醒来后扫一次 DB
        conn = await open_connection(self._db_path)
        try:
            ev = await store.fetch_next_event(conn)
            if ev is not None:
                await conn.commit()
                await self._drain_triggers()
                return {"ok": True, "status": "event", "event": ev.to_dict()}
        finally:
            await conn.close()

        return {"ok": True, "status": "no_event", "hint": "再次调用继续等"}

    async def _drain_triggers(self) -> None:
        """清空 coord_events queue 中的残留 trigger。"""
        # 内部访问私有 queue,直接读到空为止
        q = self._waiters.coord_events._queue
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
