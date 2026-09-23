import asyncio

import aiosqlite

from src.config import ROOT_DIR

DB_PATH = ROOT_DIR / "live.db"

# 两把锁分工不同，绝不可复用同一把，否则会死锁：
# - _lock：保护「业务事务」（建表 / 增删查改）。所有公开函数都先 `async with _lock`
#   持锁，再在锁内调用 get_db()。
# - _connect_lock：仅保护「共享连接的创建与置空」。
# 因为 asyncio.Lock 不可重入，如果 get_db() 内部也去抢 _lock，就会在 init_db /
# insert_message 等「已经持锁」的调用路径上自锁而死锁。所以连接创建单独用
# _connect_lock，两条加锁链方向一致（_lock -> _connect_lock，close_db 也只取
# _connect_lock，不会反向），无环即无死锁。
_lock = asyncio.Lock()
_connect_lock = asyncio.Lock()
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
    """返回复用的共享连接（懒创建，双检锁保证全局只建一条）。"""
    global _db
    # 第一重检查：热路径不加锁，连接已建立就直接返回，避免每次访问都抢锁。
    if _db is not None:
        return _db
    # 第二重检查：持 _connect_lock 后再判断一次。因为可能在等锁期间，_db 已被别的
    # 协程建好（或已被 close_db 置空后重建）。只有确认仍为 None 才真正创建，从而
    # 保证任意时刻最多只创建一条连接，多余的创建不会发生、也就不会泄漏。
    async with _connect_lock:
        if _db is None:
            _db = await _connect()
    return _db


async def close_db() -> None:
    global _db
    # 同样走 _connect_lock，保证与 get_db 的「创建/置空」对 _db 的读写互斥：
    # 避免 close_db 置空的同时 get_db 正在创建，造成重复连接或返回已关闭的连接。
    async with _connect_lock:
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
