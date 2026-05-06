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

CURRENT_SCHEMA_VERSION = 2

SCHEMA_V2_MIGRATION = """
-- v2: 把 submitted_project / submitted_branch / submitted_commit_sha 三个固定字段
-- 合并成一个自由结构的 submitted_artifact JSON 字段, 让 server 完全协议中立
-- (Coop 0.2.0 起, server 不再假设业务工作流是 git)
ALTER TABLE tasks ADD COLUMN submitted_artifact TEXT;

-- 把已有数据从三个字段迁移到 artifact JSON
UPDATE tasks
SET submitted_artifact = json_object(
    'project', submitted_project,
    'branch', submitted_branch,
    'commit_sha', submitted_commit_sha
)
WHERE submitted_at IS NOT NULL;

-- 注意: 三个旧字段保留在表中(SQLite ALTER TABLE DROP COLUMN 在 3.35+ 才有,
-- 我们不做硬依赖)。代码层只读 submitted_artifact, 旧字段永远不再写入。

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
"""

SCHEMA_FRESH_V2 = """
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
    submitted_summary    TEXT,
    submitted_artifact   TEXT,                        -- JSON, server 不解析
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
    task_id      TEXT PRIMARY KEY,
    worker_id    TEXT NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT,
    created_at   TEXT NOT NULL,
    answered_at  TEXT
);

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
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
            logger.info("DB schema 不存在, 执行 v2 全新初始化")
            await conn.executescript(SCHEMA_FRESH_V2)
            await conn.commit()
            current_version = 2

        if current_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema 版本 {current_version} 比代码支持的 "
                f"{CURRENT_SCHEMA_VERSION} 更新, 请升级代码"
            )

        if current_version < 2:
            logger.info("DB schema v1 → v2 迁移中")
            await conn.executescript(SCHEMA_V2_MIGRATION)
            await conn.commit()
            current_version = 2

        logger.info(f"DB schema 已就绪 (version={current_version})")


async def open_connection(db_path: str) -> aiosqlite.Connection:
    """打开数据库连接,启用 row_factory 让查询返回 dict-like Row 对象。"""
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn
