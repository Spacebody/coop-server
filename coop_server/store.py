"""数据访问层 - 所有 CRUD 集中在这里。

设计原则:
- 函数式风格,接受 connection 作为参数,易于测试
- 严格的状态机校验:不允许非法状态跳转
- 写操作必须显式 commit (调用方控制事务边界)
- 所有操作都返回明确的成功/失败结果或抛特定异常
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from .models import (
    Clarification,
    Event,
    EventType,
    Task,
    TaskStatus,
    Worker,
    WorkerStatus,
    utcnow_iso,
)

logger = logging.getLogger(__name__)


class StoreError(Exception):
    """store 层基础异常。"""


class NotFoundError(StoreError):
    """请求的对象不存在。"""


class ConflictError(StoreError):
    """状态冲突,比如重复创建、状态机不允许的跳转。"""


class InvalidStateError(StoreError):
    """当前状态不允许此操作。"""


# ===========================================================
# Worker
# ===========================================================

async def upsert_worker(
    conn: aiosqlite.Connection,
    worker_id: str,
    hostname: str,
) -> Worker:
    """worker 注册或重新上线。

    如果 worker_id 已存在,刷新 hostname 和 last_heartbeat。
    返回 Worker 对象。
    """
    now = utcnow_iso()

    # 先查是否存在
    cur = await conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    )
    row = await cur.fetchone()

    if row is None:
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, status, current_task_id,
                                 registered_at, last_heartbeat)
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (worker_id, hostname, WorkerStatus.IDLE.value, now, now),
        )
        logger.info(f"worker 上线: {worker_id}@{hostname}")
        return Worker(
            worker_id=worker_id,
            hostname=hostname,
            status=WorkerStatus.IDLE,
            current_task_id=None,
            registered_at=now,
            last_heartbeat=now,
        )

    # 已存在:重新上线 (例如重启后),清空 current_task_id (旧任务由清理流程处理)
    await conn.execute(
        """
        UPDATE workers SET hostname = ?, status = ?, last_heartbeat = ?
        WHERE worker_id = ?
        """,
        (hostname, WorkerStatus.IDLE.value, now, worker_id),
    )
    logger.info(f"worker 重新上线: {worker_id}@{hostname}")
    return Worker(
        worker_id=worker_id,
        hostname=hostname,
        status=WorkerStatus.IDLE,
        current_task_id=row["current_task_id"],
        registered_at=row["registered_at"],
        last_heartbeat=now,
    )


async def update_heartbeat(
    conn: aiosqlite.Connection,
    worker_id: str,
) -> None:
    """更新 worker 心跳时间。worker 不存在时抛 NotFoundError。"""
    now = utcnow_iso()
    cur = await conn.execute(
        "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
        (now, worker_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError(f"worker 不存在: {worker_id}")


async def update_worker_status(
    conn: aiosqlite.Connection,
    worker_id: str,
    status: WorkerStatus,
    current_task_id: str | None = None,
) -> None:
    """更新 worker 状态。"""
    cur = await conn.execute(
        "UPDATE workers SET status = ?, current_task_id = ? WHERE worker_id = ?",
        (status.value, current_task_id, worker_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError(f"worker 不存在: {worker_id}")


async def get_worker(
    conn: aiosqlite.Connection,
    worker_id: str,
) -> Worker | None:
    cur = await conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    )
    row = await cur.fetchone()
    return _row_to_worker(row) if row else None


async def delete_worker(
    conn: aiosqlite.Connection, worker_id: str
) -> bool:
    """删除 worker 记录。返回是否真的删除了 (False 表示不存在)。

    若该 worker 持有进行中的任务,这些任务会被标记 abandoned。
    """
    # 先把它进行中的任务 abandon
    await conn.execute(
        """
        UPDATE tasks SET status = ?
        WHERE assignee = ? AND status = ?
        """,
        (
            TaskStatus.ABANDONED.value,
            worker_id,
            TaskStatus.IN_PROGRESS.value,
        ),
    )
    cur = await conn.execute(
        "DELETE FROM workers WHERE worker_id = ?", (worker_id,)
    )
    return cur.rowcount > 0


async def list_all_workers(
    conn: aiosqlite.Connection,
    online_only: bool = False,
) -> list[Worker]:
    """列出 worker。online_only=True 时排除 OFFLINE 状态。"""
    if online_only:
        cur = await conn.execute(
            "SELECT * FROM workers WHERE status != ? ORDER BY registered_at",
            (WorkerStatus.OFFLINE.value,),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM workers ORDER BY registered_at"
        )
    rows = await cur.fetchall()
    return [_row_to_worker(r) for r in rows]


async def delete_offline_workers(
    conn: aiosqlite.Connection,
) -> int:
    """删除所有 OFFLINE 状态的 worker。返回删除数量。"""
    cur = await conn.execute(
        "DELETE FROM workers WHERE status = ?",
        (WorkerStatus.OFFLINE.value,),
    )
    return cur.rowcount


async def find_stale_workers(
    conn: aiosqlite.Connection,
    timeout_threshold_iso: str,
) -> list[Worker]:
    """返回 last_heartbeat 早于阈值且状态不是 OFFLINE 的 worker。"""
    cur = await conn.execute(
        """
        SELECT * FROM workers
        WHERE last_heartbeat < ? AND status != ?
        """,
        (timeout_threshold_iso, WorkerStatus.OFFLINE.value),
    )
    rows = await cur.fetchall()
    return [_row_to_worker(r) for r in rows]


def _row_to_worker(row: aiosqlite.Row) -> Worker:
    return Worker(
        worker_id=row["worker_id"],
        hostname=row["hostname"],
        status=WorkerStatus(row["status"]),
        current_task_id=row["current_task_id"],
        registered_at=row["registered_at"],
        last_heartbeat=row["last_heartbeat"],
    )


# ===========================================================
# Task
# ===========================================================

async def create_task(
    conn: aiosqlite.Connection,
    task_id: str,
    assignee: str,
    description: str,
    priority: str = "normal",
    parent_task_id: str | None = None,
    depends_on: list[str] | None = None,
    dispatched_from: str = "coordinator",
) -> Task:
    """创建任务。task_id 已存在抛 ConflictError。"""
    if depends_on is None:
        depends_on = []

    now = utcnow_iso()
    try:
        await conn.execute(
            """
            INSERT INTO tasks (task_id, assignee, description, priority,
                               parent_task_id, depends_on, status,
                               dispatched_from, dispatched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, assignee, description, priority,
                parent_task_id, json.dumps(depends_on),
                TaskStatus.PENDING.value,
                dispatched_from, now,
            ),
        )
    except aiosqlite.IntegrityError:
        raise ConflictError(f"task_id 已存在: {task_id}")

    return Task(
        task_id=task_id,
        assignee=assignee,
        description=description,
        priority=priority,
        parent_task_id=parent_task_id,
        depends_on=depends_on,
        status=TaskStatus.PENDING,
        dispatched_from=dispatched_from,
        dispatched_at=now,
    )


async def get_task(
    conn: aiosqlite.Connection,
    task_id: str,
) -> Task | None:
    cur = await conn.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    )
    row = await cur.fetchone()
    return _row_to_task(row) if row else None


async def list_tasks(
    conn: aiosqlite.Connection,
    status: TaskStatus | None = None,
    assignee: str | None = None,
) -> list[Task]:
    """按条件过滤任务。"""
    sql = "SELECT * FROM tasks"
    conditions = []
    params: list[Any] = []
    if status:
        conditions.append("status = ?")
        params.append(status.value)
    if assignee:
        conditions.append("assignee = ?")
        params.append(assignee)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY dispatched_at"

    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [_row_to_task(r) for r in rows]


async def claim_pending_task(
    conn: aiosqlite.Connection,
    worker_id: str,
) -> Task | None:
    """worker 拉取属于自己的最早 pending 任务,原子地标记为 in_progress。

    用 UPDATE ... WHERE 一条 SQL 完成,避免竞态。
    返回拉到的任务,或 None。
    """
    # 先找任务 ID
    cur = await conn.execute(
        """
        SELECT task_id FROM tasks
        WHERE assignee = ? AND status = ?
        ORDER BY dispatched_at LIMIT 1
        """,
        (worker_id, TaskStatus.PENDING.value),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    task_id = row["task_id"]

    # 原子 claim:仅当状态仍是 pending 时才更新
    cur = await conn.execute(
        """
        UPDATE tasks SET status = ? WHERE task_id = ? AND status = ?
        """,
        (TaskStatus.IN_PROGRESS.value, task_id, TaskStatus.PENDING.value),
    )
    if cur.rowcount == 0:
        # 被并发抢走了,极罕见 (单 worker 不会出现)
        return None

    # 更新 worker 状态
    await update_worker_status(
        conn, worker_id, WorkerStatus.WORKING, current_task_id=task_id
    )

    return await get_task(conn, task_id)


async def submit_task(
    conn: aiosqlite.Connection,
    task_id: str,
    worker_id: str,
    project: str,
    branch: str,
    commit_sha: str,
    summary: str,
) -> Task:
    """worker 提交任务结果。

    校验:
    - 任务存在且属于该 worker
    - 当前状态是 IN_PROGRESS (重复提交返回当前状态而不是再次推动状态机)
    """
    task = await get_task(conn, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    if task.assignee != worker_id:
        raise InvalidStateError(
            f"任务 {task_id} 属于 {task.assignee}, 不是 {worker_id}"
        )
    if task.status == TaskStatus.SUBMITTED:
        # 幂等:已经提交过,直接返回当前状态
        logger.warning(f"task {task_id} 重复提交,忽略")
        return task
    if task.status != TaskStatus.IN_PROGRESS:
        raise InvalidStateError(
            f"任务 {task_id} 当前状态 {task.status.value} 不允许 submit"
        )

    now = utcnow_iso()
    await conn.execute(
        """
        UPDATE tasks SET status = ?,
            submitted_project = ?, submitted_branch = ?,
            submitted_commit_sha = ?, submitted_summary = ?,
            submitted_at = ?
        WHERE task_id = ?
        """,
        (
            TaskStatus.SUBMITTED.value,
            project, branch, commit_sha, summary, now,
            task_id,
        ),
    )

    await update_worker_status(
        conn, worker_id, WorkerStatus.IDLE, current_task_id=None
    )

    return await get_task(conn, task_id)  # type: ignore[return-value]


async def request_task_cleanup(
    conn: aiosqlite.Connection,
    task_id: str,
) -> Task:
    """协调者下发清理指令:状态从 SUBMITTED 推到 CLEANING。"""
    task = await get_task(conn, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    if task.status != TaskStatus.SUBMITTED:
        raise InvalidStateError(
            f"任务 {task_id} 当前状态 {task.status.value} 不允许请求清理"
        )

    await conn.execute(
        "UPDATE tasks SET status = ? WHERE task_id = ?",
        (TaskStatus.CLEANING.value, task_id),
    )
    return await get_task(conn, task_id)  # type: ignore[return-value]


async def acknowledge_task_cleanup(
    conn: aiosqlite.Connection,
    task_id: str,
    worker_id: str,
) -> Task:
    """worker 回执已清理 worktree:CLEANING -> CLOSED。"""
    task = await get_task(conn, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    if task.assignee != worker_id:
        raise InvalidStateError(
            f"任务 {task_id} 属于 {task.assignee}, 不是 {worker_id}"
        )
    if task.status == TaskStatus.CLOSED:
        return task  # 幂等
    if task.status != TaskStatus.CLEANING:
        raise InvalidStateError(
            f"任务 {task_id} 当前状态 {task.status.value} 不允许 ack cleanup"
        )

    await conn.execute(
        "UPDATE tasks SET status = ? WHERE task_id = ?",
        (TaskStatus.CLOSED.value, task_id),
    )
    return await get_task(conn, task_id)  # type: ignore[return-value]


async def cancel_task(
    conn: aiosqlite.Connection,
    task_id: str,
    reason: str = "",
) -> Task:
    """协调者取消任务。任何未结束状态都可取消。"""
    task = await get_task(conn, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    if task.status in (TaskStatus.CLOSED, TaskStatus.CANCELLED):
        raise InvalidStateError(f"任务 {task_id} 已经结束,无法取消")

    await conn.execute(
        "UPDATE tasks SET status = ?, cancel_reason = ? WHERE task_id = ?",
        (TaskStatus.CANCELLED.value, reason, task_id),
    )

    # 如果有 worker 在干,把它解绑
    if task.assignee:
        worker = await get_worker(conn, task.assignee)
        if worker and worker.current_task_id == task_id:
            await update_worker_status(
                conn, task.assignee, WorkerStatus.IDLE, current_task_id=None
            )

    return await get_task(conn, task_id)  # type: ignore[return-value]


async def abandon_task(
    conn: aiosqlite.Connection,
    task_id: str,
    reason: str,
) -> Task:
    """worker 失联或不可恢复错误时,把进行中的任务标记 abandoned。"""
    task = await get_task(conn, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    if task.status != TaskStatus.IN_PROGRESS:
        raise InvalidStateError(
            f"任务 {task_id} 当前状态 {task.status.value} 不允许 abandon"
        )

    await conn.execute(
        "UPDATE tasks SET status = ?, abandon_reason = ? WHERE task_id = ?",
        (TaskStatus.ABANDONED.value, reason, task_id),
    )
    return await get_task(conn, task_id)  # type: ignore[return-value]


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        assignee=row["assignee"],
        description=row["description"],
        priority=row["priority"],
        parent_task_id=row["parent_task_id"],
        depends_on=json.loads(row["depends_on"]),
        status=TaskStatus(row["status"]),
        dispatched_from=row["dispatched_from"],
        dispatched_at=row["dispatched_at"],
        submitted_project=row["submitted_project"],
        submitted_branch=row["submitted_branch"],
        submitted_commit_sha=row["submitted_commit_sha"],
        submitted_summary=row["submitted_summary"],
        submitted_at=row["submitted_at"],
        cancel_reason=row["cancel_reason"],
        abandon_reason=row["abandon_reason"],
    )


# ===========================================================
# Event
# ===========================================================

async def append_event(
    conn: aiosqlite.Connection,
    event_type: EventType,
    payload: dict[str, Any],
    task_id: str | None = None,
    worker_id: str | None = None,
) -> int:
    """追加事件到队列,返回 event_id。"""
    now = utcnow_iso()
    cur = await conn.execute(
        """
        INSERT INTO events (event_type, task_id, worker_id, payload,
                            created_at, consumed)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (event_type.value, task_id, worker_id, json.dumps(payload), now),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def fetch_next_event(
    conn: aiosqlite.Connection,
) -> Event | None:
    """协调者拉取下一个未消费的事件,原子地标记 consumed。"""
    # 先查最早未消费
    cur = await conn.execute(
        """
        SELECT * FROM events
        WHERE consumed = 0
        ORDER BY event_id ASC
        LIMIT 1
        """
    )
    row = await cur.fetchone()
    if row is None:
        return None
    event_id = row["event_id"]

    # 原子标记:仅当仍未消费时
    cur = await conn.execute(
        "UPDATE events SET consumed = 1 WHERE event_id = ? AND consumed = 0",
        (event_id,),
    )
    if cur.rowcount == 0:
        # 极小概率被并发协调者抢走 (我们只支持单协调者,这里防御性)
        return None

    return _row_to_event(row)


async def count_unconsumed_events(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE consumed = 0"
    )
    row = await cur.fetchone()
    return row[0] if row else 0


def _row_to_event(row: aiosqlite.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        event_type=EventType(row["event_type"]),
        task_id=row["task_id"],
        worker_id=row["worker_id"],
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
        consumed=bool(row["consumed"]),
    )


# ===========================================================
# Clarification
# ===========================================================

async def create_clarification(
    conn: aiosqlite.Connection,
    task_id: str,
    worker_id: str,
    question: str,
) -> Clarification:
    """worker 发起 clarification 请求。

    同一 task_id 同时只允许一个未答复的提问。如果上一个已答复但未取走,
    会被覆盖 (worker 重启后重新问)。
    """
    now = utcnow_iso()
    await conn.execute(
        """
        INSERT OR REPLACE INTO clarifications
            (task_id, worker_id, question, answer, created_at, answered_at)
        VALUES (?, ?, ?, NULL, ?, NULL)
        """,
        (task_id, worker_id, question, now),
    )
    return Clarification(
        task_id=task_id,
        worker_id=worker_id,
        question=question,
        answer=None,
        created_at=now,
        answered_at=None,
    )


async def answer_clarification(
    conn: aiosqlite.Connection,
    task_id: str,
    answer: str,
) -> Clarification:
    """协调者答复 clarification。"""
    now = utcnow_iso()
    cur = await conn.execute(
        """
        UPDATE clarifications SET answer = ?, answered_at = ?
        WHERE task_id = ? AND answer IS NULL
        """,
        (answer, now, task_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError(
            f"任务 {task_id} 没有待答复的 clarification"
        )
    return await get_clarification(conn, task_id)  # type: ignore[return-value]


async def get_clarification(
    conn: aiosqlite.Connection,
    task_id: str,
) -> Clarification | None:
    cur = await conn.execute(
        "SELECT * FROM clarifications WHERE task_id = ?", (task_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return Clarification(
        task_id=row["task_id"],
        worker_id=row["worker_id"],
        question=row["question"],
        answer=row["answer"],
        created_at=row["created_at"],
        answered_at=row["answered_at"],
    )


async def consume_clarification_answer(
    conn: aiosqlite.Connection,
    task_id: str,
) -> str | None:
    """worker 取走 clarification 答复,取走后删除记录。

    返回答案字符串,或 None (没有答复或没有 clarification)。
    """
    clar = await get_clarification(conn, task_id)
    if clar is None or clar.answer is None:
        return None
    await conn.execute(
        "DELETE FROM clarifications WHERE task_id = ?", (task_id,)
    )
    return clar.answer
