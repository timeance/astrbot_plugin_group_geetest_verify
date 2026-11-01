import asyncio
import importlib
import logging
import platform as stdlib_platform
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import AsyncMock, patch

import aiosqlite


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION_ROOT = REPO_ROOT.parent
RUNTIME_ROOT = SESSION_ROOT / "runtime"
PACKAGE_NAME = "astrbot_plugin_group_geetest_verify"


# This test file is intentionally self-contained.  It must exercise the real
# aiosqlite package and must not inherit the fallback module stubs in older
# flow-test fixtures.
if not callable(getattr(aiosqlite, "connect", None)) or getattr(
    aiosqlite, "Connection", object
) is object:
    raise RuntimeError("test_state_persistence.py requires the real aiosqlite package")

if REPO_ROOT in Path(stdlib_platform.__file__).resolve().parents:
    raise RuntimeError(
        "Run pytest from the session ROOT so plugin-security/platform does not shadow stdlib platform"
    )


def _install_astrbot_logger():
    try:
        api = importlib.import_module("astrbot.api")
    except ModuleNotFoundError:
        astrbot = sys.modules.setdefault("astrbot", ModuleType("astrbot"))
        if not hasattr(astrbot, "__path__"):
            astrbot.__path__ = []
        api = ModuleType("astrbot.api")
        sys.modules["astrbot.api"] = api
        astrbot.api = api

    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("astrbot-test")


_install_astrbot_logger()
package = sys.modules.get(PACKAGE_NAME) or ModuleType(PACKAGE_NAME)
package.__path__ = [str(REPO_ROOT)]
sys.modules[PACKAGE_NAME] = package

from astrbot_plugin_group_geetest_verify.core.database.db import VerifyStateDB


def _state(
    *,
    status="pending",
    round_id="round-1",
    created_at=1_700_000_000.0,
    **overrides,
):
    state = {
        "status": status,
        "question": "1 + 1 = ?",
        "answer": 2,
        "wrong_count": 1,
        "verify_method": "geetest",
        "max_wrong_answers": 5,
        "verify_time": 1_700_000_001.0,
        "created_at": created_at,
        "verification_deadline": 1_700_000_300.0,
        "platform_type": "aiocqhttp",
        "platform_instance_id": "bot-a",
        "bot_identity": "10001",
        "round_id": round_id,
        "kick_attempts": 2,
        "next_retry_at": 1_700_000_010.0,
    }
    state.update(overrides)
    return state


class VerifyStatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    def test_uses_real_aiosqlite(self):
        self.assertTrue(Path(aiosqlite.__file__).is_file())
        self.assertIsNot(aiosqlite.Connection, object)

    async def test_legacy_table_is_migrated_in_place_without_losing_data(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    """
                    CREATE TABLE verify_states (
                        state_key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        question TEXT,
                        answer INTEGER,
                        wrong_count INTEGER DEFAULT 0,
                        verify_method TEXT,
                        max_wrong_answers INTEGER DEFAULT 5,
                        verify_time REAL,
                        created_at REAL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO verify_states(
                        state_key, status, question, answer, wrong_count,
                        verify_method, max_wrong_answers, verify_time, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-key",
                        "pending",
                        "legacy question",
                        9,
                        3,
                        "math",
                        4,
                        1_700_000_005.0,
                        1_700_000_000.0,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            db = VerifyStateDB(path)
            await db.init()
            try:
                state = await db.get("legacy-key")
                self.assertEqual(
                    state,
                    {
                        "status": "pending",
                        "question": "legacy question",
                        "answer": 9,
                        "wrong_count": 3,
                        "verify_method": "math",
                        "max_wrong_answers": 4,
                        "verify_time": 1_700_000_005.0,
                        "created_at": 1_700_000_000.0,
                        "verification_deadline": None,
                        "platform_type": None,
                        "platform_instance_id": None,
                        "bot_identity": None,
                        "round_id": None,
                        "kick_attempts": 0,
                        "next_retry_at": None,
                    },
                )

                async with db._conn.execute("PRAGMA table_info(verify_states)") as cur:
                    columns = {row[1] async for row in cur}
                self.assertTrue(
                    {
                        "verification_deadline",
                        "platform_type",
                        "platform_instance_id",
                        "bot_identity",
                        "round_id",
                        "kick_attempts",
                        "next_retry_at",
                    }.issubset(columns)
                )
            finally:
                await db.close()

    async def test_close_and_restart_preserve_identity_deadline_and_retry_fields(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "restart.db"
            state = _state(
                round_id="round-persisted",
                verification_deadline=1_700_100_300.0,
                platform_type="telegram",
                platform_instance_id="telegram-instance",
                bot_identity="987654321",
                kick_attempts=4,
                next_retry_at=1_700_100_120.0,
            )

            db = VerifyStateDB(path)
            await db.init()
            await db.set("identity-key", state)
            self.assertEqual(await db.get("identity-key"), state)
            await db.close()

            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(await restarted.get("identity-key"), state)
            finally:
                await restarted.close()

    async def test_cleanup_expired_removes_only_terminal_states(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "cleanup.db"
            old_created_at = time.time() - 10_000
            db = VerifyStateDB(path)
            await db.init()
            try:
                await db.set(
                    "verified-old",
                    _state(status="verified", created_at=old_created_at),
                )
                await db.set(
                    "bypassed-old",
                    _state(status="bypassed", created_at=old_created_at),
                )
                await db.set(
                    "pending-old",
                    _state(status="pending", created_at=old_created_at),
                )
                await db.set(
                    "failed-old",
                    _state(status="failed", created_at=old_created_at),
                )

                await db.cleanup_expired(max_age_seconds=60)

                self.assertFalse(db.contains("verified-old"))
                self.assertFalse(db.contains("bypassed-old"))
                self.assertTrue(db.contains("pending-old"))
                self.assertTrue(db.contains("failed-old"))
            finally:
                await db.close()

    async def test_cleanup_uses_captured_round_cas_when_state_is_replaced(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "cleanup-round-cas.db"
            db = VerifyStateDB(path)
            await db.init()
            old_state = _state(
                status="verified",
                round_id="old-round",
                created_at=time.time() - 10_000,
            )
            new_state = _state(
                status="pending",
                round_id="new-round",
                created_at=time.time(),
            )
            replaced = False
            original_delete = db.delete
            try:
                await db.set("raced-key", old_state)

                async def replace_before_delete(state_key, *, expected_round_id):
                    nonlocal replaced
                    if not replaced:
                        replaced = True
                        await db.set(state_key, new_state)
                    return await original_delete(
                        state_key,
                        expected_round_id=expected_round_id,
                    )

                with patch.object(db, "delete", new=replace_before_delete):
                    await db.cleanup_expired(max_age_seconds=60)

                self.assertTrue(replaced)
                self.assertEqual(await db.get("raced-key"), new_state)
            finally:
                await db.close()

            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(await restarted.get("raced-key"), new_state)
            finally:
                await restarted.close()

    async def test_round_cas_rejects_stale_rounds_and_handles_explicit_null(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "round-cas.db"
            db = VerifyStateDB(path)
            await db.init()
            try:
                key = "round-key"
                await db.set(key, _state(round_id="new-round"))

                self.assertFalse(
                    await db.set(
                        key,
                        _state(round_id="stale-round", status="verified"),
                        expected_round_id="old-round",
                    )
                )
                self.assertFalse(
                    await db.update_fields(
                        key,
                        {"status": "verified"},
                        expected_round_id="old-round",
                    )
                )
                self.assertFalse(
                    await db.update_fields(
                        key,
                        {"status": "verified"},
                        expected_round_id=None,
                    )
                )
                self.assertFalse(
                    await db.delete(key, expected_round_id="old-round")
                )
                self.assertFalse(await db.delete(key, expected_round_id=None))
                self.assertEqual(await db.get(key), _state(round_id="new-round"))

                null_key = "null-round-key"
                await db.set(null_key, _state(round_id=None))
                self.assertTrue(
                    await db.update_fields(
                        null_key,
                        {"next_retry_at": None},
                        expected_round_id=None,
                    )
                )
                self.assertTrue(
                    await db.delete(null_key, expected_round_id=None)
                )
                self.assertFalse(db.contains(null_key))
            finally:
                await db.close()

    async def test_wrong_count_increments_are_atomic_and_durable(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "atomic-count.db"
            db = VerifyStateDB(path)
            await db.init()
            try:
                await db.set("key", _state(wrong_count=0))
                counts = await asyncio.gather(*[
                    db.increment_wrong_count("key", expected_round_id="round-1")
                    for _ in range(20)
                ])
                self.assertEqual(sorted(counts), list(range(1, 21)))
                self.assertEqual(db.get_cached("key")["wrong_count"], 20)
                self.assertEqual(db.get_cached("key")["kick_attempts"], 2)
            finally:
                await db.close()
            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(restarted.get_cached("key")["wrong_count"], 20)
            finally:
                await restarted.close()

    async def test_wrong_count_rejects_stale_or_finished_rounds(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            db = VerifyStateDB(Path(temp_dir) / "count-round.db")
            await db.init()
            try:
                self.assertIsNone(await db.increment_wrong_count(
                    "missing", expected_round_id=None))
                for status in ("pending", "verified", "bypassed"):
                    original = _state(status=status)
                    await db.set("key", original)
                    self.assertIsNone(await db.increment_wrong_count(
                        "key", expected_round_id="old-round"))
                    if status != "pending":
                        self.assertIsNone(await db.increment_wrong_count(
                            "key", expected_round_id="round-1"))
                        self.assertFalse(await db.update_fields(
                            "key", {"question": "refresh"},
                            expected_round_id="round-1", expected_status="pending"))
                    self.assertEqual(db.get_cached("key"), original)
                await db.set("legacy", _state(round_id=None))
                self.assertEqual(await db.increment_wrong_count(
                    "legacy", expected_round_id=None), 2)
            finally:
                await db.close()

    async def test_wrong_count_commit_failure_rolls_back_without_publishing(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "count-failed-commit.db"
            db = VerifyStateDB(path)
            await db.init()
            original = _state()
            try:
                await db.set("key", original)
                with patch.object(db._conn, "commit", new=AsyncMock(
                    side_effect=RuntimeError("commit failed"),
                )):
                    with self.assertRaisesRegex(RuntimeError, "commit failed"):
                        await db.increment_wrong_count("key", expected_round_id="round-1")
                self.assertEqual(db.get_cached("key"), original)
                await db.set("unrelated", _state())
            finally:
                await db.close()
            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(restarted.get_cached("key"), original)
            finally:
                await restarted.close()

    async def test_set_commit_failure_keeps_cache_and_does_not_leak_to_later_write(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "set-commit-failure.db"
            db = VerifyStateDB(path)
            await db.init()
            old_state = _state(round_id="old-round")
            replacement = _state(round_id="new-round", status="verified")
            unrelated = _state(round_id="unrelated-round")
            try:
                await db.set("existing-key", old_state)

                with patch.object(
                    db._conn,
                    "commit",
                    new=AsyncMock(side_effect=RuntimeError("commit failed")),
                ):
                    with self.assertRaisesRegex(RuntimeError, "commit failed"):
                        await db.set("existing-key", replacement)

                self.assertEqual(await db.get("existing-key"), old_state)
                await db.set("unrelated-key", unrelated)
            finally:
                await db.close()

            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(await restarted.get("existing-key"), old_state)
                self.assertEqual(await restarted.get("unrelated-key"), unrelated)
            finally:
                await restarted.close()

    async def test_delete_cas_deletes_only_matching_round(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "delete-cas.db"
            db = VerifyStateDB(path)
            await db.init()
            try:
                await db.set("delete-key", _state(round_id="current-round"))

                self.assertFalse(
                    await db.delete("delete-key", expected_round_id="old-round")
                )
                self.assertTrue(db.contains("delete-key"))
                self.assertTrue(
                    await db.delete("delete-key", expected_round_id="current-round")
                )
                self.assertFalse(db.contains("delete-key"))
            finally:
                await db.close()

    async def test_delete_commit_failure_isolated_from_later_write_and_restart(self):
        with TemporaryDirectory(dir=RUNTIME_ROOT) as temp_dir:
            path = Path(temp_dir) / "delete-commit-failure.db"
            db = VerifyStateDB(path)
            await db.init()
            victim = _state(round_id="victim-round")
            unrelated = _state(round_id="unrelated-round")
            try:
                await db.set("victim-key", victim)

                with patch.object(
                    db._conn,
                    "commit",
                    new=AsyncMock(side_effect=RuntimeError("delete commit failed")),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "delete commit failed"
                    ):
                        await db.delete("victim-key")

                self.assertEqual(await db.get("victim-key"), victim)
                await db.set("unrelated-key", unrelated)
                self.assertEqual(await db.get("victim-key"), victim)
            finally:
                await db.close()

            restarted = VerifyStateDB(path)
            await restarted.init()
            try:
                self.assertEqual(await restarted.get("victim-key"), victim)
                self.assertEqual(await restarted.get("unrelated-key"), unrelated)
            finally:
                await restarted.close()


if __name__ == "__main__":
    unittest.main()
