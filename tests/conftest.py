"""共享测试 fixture。"""
from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator

import aiosqlite
import pytest_asyncio

from coop_server.db import init_db, open_connection


@pytest_asyncio.fixture
async def db_conn() -> AsyncIterator[aiosqlite.Connection]:
    """每个测试一个全新的临时 SQLite 文件。

    用临时文件而非 :memory: 是因为 aiosqlite 在不同协程间访问 :memory:
    会创建新的内存空间,共享不了状态。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_db(path)
        conn = await open_connection(path)
        try:
            yield conn
        finally:
            await conn.close()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        # 清理 WAL 副产物
        for ext in ("-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass
