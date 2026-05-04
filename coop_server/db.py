"""SQLite 持久化层 - schema 定义、连接管理、迁移。

设计原则:
- 单连接单写者(SQLite 限制),协程间靠 aiosqlite 内部排队
- WAL 模式提升并发读
- schema 版本化便于未来升级
"""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id      TEXT PRIMARY KEY,
    hostname       TEXT NOT NULL,
    status         TEXT NOT NULL,
    current_task_id TEXT,
    registered_at  TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,
    assignee             TEXT NOT NULL,
    description          TEXT NOT NULL,
    priority             TEXT NOT NULL DEFAULT 'normal',
    parent_task_id       TEXT,
    depends_on           TEXT NOT NULL DEFAULT '[]',  -- JSON array
    status               TEXT NOT NULL,
    dispatched_from      TEXT NOT NULL,
    dispatched_at        TEXT NOT NULL,
    submitted_project    TEXT,
    submitted_branch     TEXT,
    submitted_commit_sha TEXT,
    submitted_summary    TEXT,
    submitted_at         TEXT,
    cancel_reason        TEXT,
    abandon_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    task_id      TEXT,
    worker_id    TEXT,
    payload      TEXT NOT NULL,    -- JSON
    created_at   TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0  -- 0=false, 1=true
);

CREATE INDEX IF NOT EXISTS idx_events_consumed_id ON events(consumed, event_id);

CREATE TABLE IF NOT EXISTS clarifications (
    task_id      TEXT PRIMARY KEY,  -- 一个任务同时只允许一个未答复的提问
    worker_id    TEXT NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT,
    created_at   TEXT NOT NULL,
    answered_at  TEXT
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""


async def init_db(db_path: str) -> None:
    """初始化 DB 文件,创建表,执行迁移。

    幂等:多次调用不会重复创建。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as conn:
        # 启用 WAL 提升并发读性能
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")

        # 检查 schema 版本
        try:
            cur = await conn.execute("SELECT MAX(version) FROM schema_version")
            row = await cur.fetchone()
            current_version = row[0] if row and row[0] is not None else 0
        except aiosqlite.OperationalError:
            # 表还不存在
            current_version = 0

        if current_version == 0:
            logger.info("DB schema 不存在,执行 v1 初始化")
            await conn.executescript(SCHEMA_V1)
            await conn.commit()
            current_version = 1

        if current_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema 版本 {current_version} 比代码支持的 "
                f"{CURRENT_SCHEMA_VERSION} 更新,请升级代码"
            )

        # 未来加新版本时,这里写迁移逻辑
        # if current_version < 2: await migrate_v1_to_v2(conn)

        logger.info(f"DB schema 已就绪 (version={current_version})")


async def open_connection(db_path: str) -> aiosqlite.Connection:
    """打开数据库连接,启用 row_factory 让查询返回 dict-like Row 对象。"""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn
