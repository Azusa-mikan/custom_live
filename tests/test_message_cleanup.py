import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from src import api, sql, telegram_bridge


THREE_DAYS_MS = 3 * 24 * 60 * 60 * 1000  # 259_200_000 ms


def _reset_sql_locks() -> None:
    # 多个用例各自创建独立事件循环；asyncio.Lock 一旦发生竞争就会永久绑定到当前 loop，
    # 因此在用例切换 loop 且连接已关闭后重置模块锁，避免跨 loop 复用报错。
    sql._lock = asyncio.Lock()
    sql._connect_lock = asyncio.Lock()


class MessageCleanupTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = sql.DB_PATH
        await sql.close_db()
        _reset_sql_locks()
        sql.DB_PATH = Path(self._tmpdir.name) / "test_live.db"
        await sql.init_db()

    async def asyncTearDown(self) -> None:
        await sql.close_db()
        _reset_sql_locks()
        sql.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    async def test_delete_messages_before_removes_only_older_rows(self) -> None:
        cutoff_ms = 1_800_000_000_000
        await sql.insert_message("expired", cutoff_ms - 1, name="alice")
        await sql.insert_message("at-boundary", cutoff_ms, name="bob")
        await sql.insert_message("fresh", cutoff_ms + 60_000, name="carol")
        await sql.increment_likes()

        deleted = await sql.delete_messages_before(cutoff_ms)

        self.assertEqual(deleted, 1)
        remaining = await sql.list_messages(limit=50)
        self.assertEqual([row["text"] for row in remaining], ["at-boundary", "fresh"])
        self.assertEqual(await sql.get_likes(), 1)

    async def test_delete_messages_before_returns_zero_when_nothing_expired(self) -> None:
        cutoff_ms = 1_800_000_000_000
        await sql.insert_message("fresh", cutoff_ms + 1_000, name="alice")

        deleted = await sql.delete_messages_before(cutoff_ms)

        self.assertEqual(deleted, 0)
        remaining = await sql.list_messages(limit=50)
        self.assertEqual([row["text"] for row in remaining], ["fresh"])

    async def test_cleanup_expired_messages_uses_three_day_ms_window(self) -> None:
        now_ms = 1_800_000_000_000
        # 3 天 = 259_200_000 毫秒；1ms 边界验证单位不是秒且窗口正好为 3 天
        await sql.insert_message("too-old", now_ms - 259_200_001, name="alice")
        await sql.insert_message("just-kept", now_ms - 259_200_000, name="bob")
        await sql.insert_message("recent", now_ms - 1_000, name="carol")

        deleted = await api._cleanup_expired_messages(now_ms=now_ms)

        self.assertEqual(deleted, 1)
        remaining = await sql.list_messages(limit=50)
        self.assertEqual([row["text"] for row in remaining], ["just-kept", "recent"])

    async def test_cleanup_loop_runs_periodically_and_survives_error(self) -> None:
        now_ms = int(time.time() * 1000)
        await sql.insert_message("old-1", now_ms - THREE_DAYS_MS - 10_000, name="alice")
        await sql.insert_message("fresh-1", now_ms - 1_000, name="bob")

        attempts = 0
        real_delete = sql.delete_messages_before

        async def flaky_delete(cutoff_ms: int) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient db error")
            return await real_delete(cutoff_ms)

        with (
            patch.object(sql, "delete_messages_before", side_effect=flaky_delete),
            self.assertLogs(api.logger, level="WARNING") as logs,
        ):
            task = asyncio.create_task(api._message_cleanup_loop(interval=0.02))
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    rows = await sql.list_messages(limit=50)
                    if [r["text"] for r in rows] == ["fresh-1"]:
                        break
                    await asyncio.sleep(0.02)
                rows = await sql.list_messages(limit=50)
                self.assertEqual([r["text"] for r in rows], ["fresh-1"])
                self.assertGreaterEqual(attempts, 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(any("transient db error" in msg for msg in logs.output))

    async def test_lifespan_cleans_on_startup_and_periodically_and_stops_cleanly(self) -> None:
        now_ms = int(time.time() * 1000)
        await sql.insert_message("startup-old", now_ms - THREE_DAYS_MS - 60_000, name="alice")
        await sql.insert_message("startup-fresh", now_ms - 60_000, name="bob")
        await sql.close_db()

        with (
            patch.object(telegram_bridge, "TELEGRAM_BOT_TOKEN", None),
            patch.object(api, "MESSAGE_CLEANUP_INTERVAL", 0.02),
        ):
            async with api.lifespan(api.app):
                # 启动时立即清理掉 3 天前的历史消息
                rows = await sql.list_messages(limit=50)
                self.assertEqual([r["text"] for r in rows], ["startup-fresh"])

                # 运行期间新增的过期消息也会被后台定时任务清理
                await sql.insert_message(
                    "runtime-old",
                    int(time.time() * 1000) - THREE_DAYS_MS - 60_000,
                    name="carol",
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    rows = await sql.list_messages(limit=50)
                    if [r["text"] for r in rows] == ["startup-fresh"]:
                        break
                    await asyncio.sleep(0.02)
                rows = await sql.list_messages(limit=50)
                self.assertEqual([r["text"] for r in rows], ["startup-fresh"])

            # lifespan 退出后后台清理任务已停止，且数据库已关闭
            self.assertIsNone(sql._db)
            self.assertIsNone(api._cleanup_task)


class ChatHistoryCleanupIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = sql.DB_PATH
        asyncio.run(sql.close_db())
        _reset_sql_locks()
        sql.DB_PATH = Path(self._tmpdir.name) / "test_ws.db"

    def tearDown(self) -> None:
        asyncio.run(sql.close_db())
        _reset_sql_locks()
        sql.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_websocket_history_excludes_messages_older_than_three_days(self) -> None:
        async def seed() -> None:
            await sql.init_db()
            now_ms = int(time.time() * 1000)
            await sql.insert_message("4-days-ago", now_ms - 4 * 24 * 60 * 60 * 1000, name="old")
            await sql.insert_message("1-hour-ago", now_ms - 60 * 60 * 1000, name="new")
            await sql.close_db()

        asyncio.run(seed())
        _reset_sql_locks()

        with patch.object(telegram_bridge, "TELEGRAM_BOT_TOKEN", None):
            with TestClient(api.app) as client:
                with client.websocket_connect("/api/chat") as ws:
                    history = ws.receive_json()
                    self.assertEqual(history["type"], "history")
                    self.assertEqual(
                        [item["text"] for item in history["items"]],
                        ["1-hour-ago"],
                    )


if __name__ == "__main__":
    unittest.main()
