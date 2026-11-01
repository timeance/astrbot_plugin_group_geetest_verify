import asyncio
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from astrbot.api import logger


_ROUND_ID_UNSET = object()


class VerifyStateDB:
    _MIGRATIONS = {
        "verification_deadline": "REAL",
        "platform_type": "TEXT",
        "platform_instance_id": "TEXT",
        "bot_identity": "TEXT",
        "round_id": "TEXT",
        "kick_attempts": "INTEGER DEFAULT 0",
        "next_retry_at": "REAL",
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._cache: dict = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def init(self):
        async with self._init_lock:
            if self._initialized:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            self._conn.row_factory = aiosqlite.Row

            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS verify_states (
                    state_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    question TEXT,
                    answer INTEGER,
                    wrong_count INTEGER DEFAULT 0,
                    verify_method TEXT,
                    max_wrong_answers INTEGER DEFAULT 5,
                    verify_time REAL,
                    created_at REAL,
                    verification_deadline REAL,
                    platform_type TEXT,
                    platform_instance_id TEXT,
                    bot_identity TEXT,
                    round_id TEXT,
                    kick_attempts INTEGER DEFAULT 0,
                    next_retry_at REAL
                );
            """)

            async with self._conn.execute("PRAGMA table_info(verify_states);") as cur:
                existing_columns = {
                    row[1] if not isinstance(row, dict) else row["name"]
                    async for row in cur
                }

            # Existing installations are upgraded in place.  The identifiers in
            # _MIGRATIONS are static, so this ALTER TABLE cannot be influenced by
            # a state key or another persisted value.
            for column, definition in self._MIGRATIONS.items():
                if column not in existing_columns:
                    await self._conn.execute(
                        f"ALTER TABLE verify_states ADD COLUMN {column} {definition};"
                    )
            await self._conn.commit()

            async with self._conn.execute("SELECT * FROM verify_states;") as cur:
                async for row in cur:
                    self._cache[row["state_key"]] = {
                        "status": row["status"],
                        "question": row["question"],
                        "answer": row["answer"],
                        "wrong_count": row["wrong_count"],
                        "verify_method": row["verify_method"],
                        "max_wrong_answers": row["max_wrong_answers"],
                        "verify_time": row["verify_time"],
                        "created_at": row["created_at"],
                        "verification_deadline": row["verification_deadline"],
                        "platform_type": row["platform_type"],
                        "platform_instance_id": row["platform_instance_id"],
                        "bot_identity": row["bot_identity"],
                        "round_id": row["round_id"],
                        "kick_attempts": row["kick_attempts"] or 0,
                        "next_retry_at": row["next_retry_at"],
                    }

            self._initialized = True
            logger.info(f"[Geetest Verify] 初始化验证状态数据库 ({len(self._cache)} 个状态)")

    async def _save_to_db(self, state_key: str, data: dict):
        if not self._conn:
            raise RuntimeError("VerifyStateDB not initialized")

        try:
            await self._conn.execute(
                """
                INSERT INTO verify_states(
                    state_key, status, question, answer, wrong_count, verify_method,
                    max_wrong_answers, verify_time, created_at, verification_deadline,
                    platform_type, platform_instance_id, bot_identity, round_id,
                    kick_attempts, next_retry_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    status=excluded.status,
                    question=excluded.question,
                    answer=excluded.answer,
                    wrong_count=excluded.wrong_count,
                    verify_method=excluded.verify_method,
                    max_wrong_answers=excluded.max_wrong_answers,
                    verify_time=excluded.verify_time,
                    created_at=excluded.created_at,
                    verification_deadline=excluded.verification_deadline,
                    platform_type=excluded.platform_type,
                    platform_instance_id=excluded.platform_instance_id,
                    bot_identity=excluded.bot_identity,
                    round_id=excluded.round_id,
                    kick_attempts=excluded.kick_attempts,
                    next_retry_at=excluded.next_retry_at;
                """,
                (
                    state_key,
                    data.get("status", "pending"),
                    data.get("question"),
                    data.get("answer"),
                    data.get("wrong_count", 0),
                    data.get("verify_method"),
                    data.get("max_wrong_answers", 5),
                    data.get("verify_time"),
                    data.get("created_at"),
                    data.get("verification_deadline"),
                    data.get("platform_type"),
                    data.get("platform_instance_id"),
                    data.get("bot_identity"),
                    data.get("round_id"),
                    data.get("kick_attempts", 0),
                    data.get("next_retry_at"),
                ),
            )
            await self._conn.commit()
        except BaseException:
            # A failed execute/commit must not leave an open transaction that
            # a later successful write could commit accidentally.  This also
            # covers task cancellation while a write is in flight.
            try:
                await self._conn.rollback()
            except BaseException:
                logger.warning(
                    "[Geetest Verify] 回滚验证状态写入失败",
                    exc_info=True,
                )
            raise

    async def get(self, state_key: str) -> Optional[dict]:
        return self._cache.get(state_key)

    async def set(
        self,
        state_key: str,
        data: dict,
        *,
        expected_round_id: object = _ROUND_ID_UNSET,
    ) -> bool:
        async with self._write_lock:
            existing = self._cache.get(state_key)
            if (
                expected_round_id is not _ROUND_ID_UNSET
                and (existing is None or existing.get("round_id") != expected_round_id)
            ):
                return False

            data = dict(data)
            if "created_at" not in data:
                if existing and "created_at" in existing:
                    data["created_at"] = existing["created_at"]
                else:
                    data["created_at"] = time.time()

            await self._save_to_db(state_key, data)
            # Publish the cache only after SQLite has committed.  A failed
            # write must leave callers observing the previous durable state.
            self._cache[state_key] = data
            return True

    async def increment_wrong_count(
        self,
        state_key: str,
        *,
        expected_round_id: object,
    ) -> Optional[int]:
        """Count one invalid answer only for the still-pending round."""
        async with self._write_lock:
            existing = self._cache.get(state_key)
            if (
                existing is None
                or existing.get("status") != "pending"
                or existing.get("round_id") != expected_round_id
            ):
                return None
            updated = dict(existing)
            count = int(existing.get("wrong_count") or 0) + 1
            updated["wrong_count"] = count
            await self._save_to_db(state_key, updated)
            self._cache[state_key] = updated
            return count

    async def update_field(
        self,
        state_key: str,
        field: str,
        value,
        *,
        expected_round_id: object = _ROUND_ID_UNSET,
    ) -> bool:
        return await self.update_fields(
            state_key,
            {field: value},
            expected_round_id=expected_round_id,
        )

    async def update_fields(
        self,
        state_key: str,
        fields: dict,
        *,
        expected_round_id: object = _ROUND_ID_UNSET,
        expected_status: Optional[str] = None,
    ) -> bool:
        """Atomically update a set of fields in the in-memory and SQLite state."""
        async with self._write_lock:
            existing = self._cache.get(state_key)
            if (
                existing is None
                or (expected_status is not None and existing.get("status") != expected_status)
                or (
                    expected_round_id is not _ROUND_ID_UNSET
                    and existing.get("round_id") != expected_round_id
                )
            ):
                return False
            updated = dict(existing)
            updated.update(fields)
            await self._save_to_db(state_key, updated)
            self._cache[state_key] = updated
            return True

    async def delete(
        self,
        state_key: str,
        *,
        expected_round_id: object = _ROUND_ID_UNSET,
    ) -> bool:
        async with self._write_lock:
            if not self._conn:
                return False

            cursor = None
            try:
                if expected_round_id is _ROUND_ID_UNSET:
                    cursor = await self._conn.execute(
                        "DELETE FROM verify_states WHERE state_key = ?",
                        (state_key,),
                    )
                elif expected_round_id is None:
                    cursor = await self._conn.execute(
                        "DELETE FROM verify_states WHERE state_key = ? AND round_id IS NULL",
                        (state_key,),
                    )
                else:
                    cursor = await self._conn.execute(
                        "DELETE FROM verify_states WHERE state_key = ? AND round_id = ?",
                        (state_key, expected_round_id),
                    )
                await self._conn.commit()
                deleted = cursor.rowcount > 0
            except BaseException:
                try:
                    await self._conn.rollback()
                except BaseException:
                    logger.warning(
                        "[Geetest Verify] 回滚验证状态删除失败",
                        exc_info=True,
                    )
                raise
            finally:
                if cursor is not None:
                    await cursor.close()
            if deleted:
                cached = self._cache.get(state_key)
                if (
                    expected_round_id is _ROUND_ID_UNSET
                    or cached is None
                    or cached.get("round_id") == expected_round_id
                ):
                    self._cache.pop(state_key, None)
            return deleted

    def contains(self, state_key: str) -> bool:
        return state_key in self._cache

    def get_cached(self, state_key: str) -> Optional[dict]:
        return self._cache.get(state_key)

    def all_keys(self) -> list:
        return list(self._cache.keys())

    async def cleanup_expired(self, max_age_seconds: float = 86400):
        now = time.time()
        expired_keys = []
        for key, data in self._cache.items():
            if data.get("status") in ("verified", "bypassed"):
                created_at = data.get("created_at") or data.get("verify_time") or 0
                if now - created_at > max_age_seconds:
                    expired_keys.append((key, data.get("round_id")))

        deleted = 0
        for key, round_id in expired_keys:
            if await self.delete(key, expected_round_id=round_id):
                deleted += 1

        if deleted:
            logger.info(f"[Geetest Verify] 清理过期验证状态 {deleted} 个")

    async def close(self):
        if self._conn:
            conn = self._conn
            self._conn = None
            self._initialized = False
            # Every successful mutation commits before publishing its cache.
            # Closing must not commit a failed or interrupted transaction.
            await conn.close()
            await asyncio.sleep(0)
