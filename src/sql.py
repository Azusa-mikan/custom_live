import asyncio

import aiosqlite

from src.config import ROOT_DIR

DB_PATH = ROOT_DIR / "live.db"

_lock = asyncio.Lock()
_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ip TEXT,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS likes (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    count INTEGER NOT NULL DEFAULT 0
);

INSERT INTO likes (id, count) VALUES (1, 0)
ON CONFLICT(id) DO NOTHING;
"""


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    return db


async def get_db() -> aiosqlite.Connection:
    """返回复用的共享连接（懒创建）。"""
    global _db
    if _db is None:
        _db = await _connect()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db() -> None:
    async with _lock:
        db = await get_db()
        await db.executescript(_SCHEMA)
        await db.commit()


async def insert_message(
    text: str,
    created_at: int,
    *,
    name: str,
    ip: str | None = None,
) -> None:
    async with _lock:
        db = await get_db()
        await db.execute(
            "INSERT INTO messages (name, ip, text, created_at) VALUES (?, ?, ?, ?)",
            (name, ip, text, created_at),
        )
        await db.commit()


async def list_messages(limit: int = 200) -> list[dict]:
    async with _lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT name, ip, text, created_at FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = list(await cursor.fetchall())
    return [dict(row) for row in reversed(rows)]


async def get_likes() -> int:
    async with _lock:
        db = await get_db()
        cursor = await db.execute("SELECT count FROM likes WHERE id = 1")
        row = await cursor.fetchone()
        return int(row["count"]) if row is not None else 0


async def increment_likes() -> int:
    async with _lock:
        db = await get_db()
        await db.execute("UPDATE likes SET count = count + 1 WHERE id = 1")
        await db.commit()
        cursor = await db.execute("SELECT count FROM likes WHERE id = 1")
        row = await cursor.fetchone()
        return int(row["count"]) if row is not None else 0
