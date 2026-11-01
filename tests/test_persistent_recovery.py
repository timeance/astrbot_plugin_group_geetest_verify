import asyncio
import time
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_geetest_verification import MemoryDB, flow_plugin
from time_utils import advancing_clock
from test_new_member_permission_flow import Plugin, REPO_ROOT
from test_verification_message_recall import Bot, event, plugin as recall_plugin
from astrbot_plugin_group_geetest_verify.core.database.db import VerifyStateDB


TEST_TMP_ROOT = REPO_ROOT.parent / "runtime" / "test-tmp"


def _db_path(prefix):
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TEST_TMP_ROOT / f"{prefix}-{uuid.uuid4().hex}.db"


def _pending_state(round_id, **extra):
    state = {
        "status": "pending",
        "round_id": round_id,
        "verification_deadline": time.time() + 300,
        "platform_type": "aiocqhttp",
        "platform_instance_id": "bot-a",
        "bot_identity": "10001",
        "wrong_count": 0,
        "kick_attempts": 0,
        "next_retry_at": None,
    }
    state.update(extra)
    return state


def _kick_plugin(db, *, response=False, presence="present"):
    instance = object.__new__(Plugin)
    instance.db = db
    instance._tasks = {}
    instance._verification_messages = {}
    instance._kick_exhaustion_notified = set()
    instance._event_for_state = Mock(side_effect=lambda ev, state, gid, uid: ev)
    instance._finish_verification_messages = AsyncMock()
    instance.kick_message = "{at_user} kicked"
    instance._get_member_presence = AsyncMock(return_value=presence)
    instance._kick_verifying_member = AsyncMock(return_value=response)
    instance._send_group_message = AsyncMock()
    instance._get_platform = Mock(return_value="aiocqhttp")
    return instance


class BlockingSetDB(MemoryDB):
    def __init__(self, state):
        super().__init__(state)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def set(self, key, value, *, expected_round_id=object()):
        self.entered.set()
        await self.release.wait()
        raise RuntimeError("simulated durable write failure")


class KickAttemptRaceDB(MemoryDB):
    def __init__(self, state, replacement):
        super().__init__(state)
        self.replacement = replacement
        self.replaced = False

    async def update_fields(self, key, fields, *, expected_round_id=object()):
        result = await super().update_fields(
            key,
            fields,
            expected_round_id=expected_round_id,
        )
        if "kick_attempts" in fields and not self.replaced:
            self.replaced = True
            self.states[key] = dict(self.replacement)
        return result


class OfflineRetryRaceDB(MemoryDB):
    def __init__(self, state, replacement):
        super().__init__(state)
        self.replacement = replacement
        self.replaced = False

    async def update_fields(self, key, fields, *, expected_round_id=object()):
        if "next_retry_at" in fields and not self.replaced:
            self.replaced = True
            self.states[key] = dict(self.replacement)
        return await super().update_fields(
            key,
            fields,
            expected_round_id=expected_round_id,
        )


class PersistentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_new_round_write_keeps_old_task_and_state(self):
        key = "123:456"
        old_state = _pending_state("old-round")
        db = BlockingSetDB({key: old_state})
        instance = object.__new__(Plugin)
        instance.db = db
        old_task = Mock()
        old_task.done.return_value = False
        instance._tasks = {key: old_task}
        instance._kick_exhaustion_notified = set()
        instance.api_key = ""
        instance._get_group_config = Mock(return_value={
            "max_wrong_answers": 5,
            "verification_timeout": 300,
            "enable_geetest_verify": False,
        })
        instance._get_platform = Mock(return_value="aiocqhttp")
        instance._get_event_platform_id = Mock(return_value="bot-a")
        instance._get_event_self_id = Mock(return_value="10001")

        started = asyncio.create_task(
            instance._start_verification_process(
                SimpleNamespace(), "456", 123, is_new_member=True
            )
        )
        await db.entered.wait()
        self.assertFalse(old_task.cancel.called)
        self.assertEqual(db.get_cached(key), old_state)

        db.release.set()
        with self.assertRaises(RuntimeError):
            await started
        self.assertFalse(old_task.cancel.called)
        self.assertEqual(db.get_cached(key), old_state)

    async def test_sqlite_lock_and_round_cas_preserve_new_round(self):
        path = _db_path("cas")
        db = VerifyStateDB(path)
        await db.init()
        key = "123:456"
        try:
            await db.set(key, _pending_state("old-round"))

            original_save = db._save_to_db
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocked_save(state_key, state):
                entered.set()
                await release.wait()
                await original_save(state_key, state)

            db._save_to_db = blocked_save
            replacement = _pending_state("new-round")
            new_write = asyncio.create_task(db.set(key, replacement))
            await entered.wait()
            old_delete = asyncio.create_task(
                db.delete(key, expected_round_id="old-round")
            )
            self.assertFalse(old_delete.done())
            release.set()

            self.assertTrue(await new_write)
            self.assertFalse(await old_delete)
            self.assertEqual(db.get_cached(key)["round_id"], "new-round")
            self.assertFalse(
                await db.update_fields(
                    key,
                    {"status": "verified"},
                    expected_round_id="old-round",
                )
            )
            self.assertTrue(
                await db.update_fields(
                    key,
                    {"next_retry_at": 123.0},
                    expected_round_id="new-round",
                )
            )
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_legacy_null_round_cas_cannot_finish_new_uuid_round(self):
        plugin, _ = flow_plugin()
        key = "123:456"
        entered = asyncio.Event()
        release = asyncio.Event()

        await plugin.db.set(
            key,
            _pending_state(None, verify_method="geetest"),
        )

        async def delayed_check(*_args):
            entered.set()
            await release.wait()
            return True

        plugin._check_geetest_verify = AsyncMock(side_effect=delayed_check)
        reply = SimpleNamespace(
            message_str="验证码：8SVK9T",
            get_sender_id=lambda: "456",
            message_obj=SimpleNamespace(raw_message={}),
            stop_event=Mock(),
        )
        checking = asyncio.create_task(
            plugin._process_verification_message(reply)
        )
        await entered.wait()
        await plugin.db.set(key, _pending_state("new-round", verify_method="geetest"))
        release.set()
        await checking

        self.assertEqual(plugin.db.get_cached(key)["round_id"], "new-round")
        self.assertEqual(plugin.db.get_cached(key)["status"], "pending")
        plugin._send_group_message_with_image.assert_not_awaited()

    async def test_false_kick_response_retries_and_keeps_pending_record(self):
        key = "123:456"
        db = MemoryDB({key: _pending_state("round-a")})
        instance = _kick_plugin(db, response=False)
        message_round = SimpleNamespace(closed=False)

        with advancing_clock() as clock:
            result = await instance._kick_with_retries(
                SimpleNamespace(),
                123,
                "456",
                "round-a",
                message_round,
            )

        self.assertEqual(result, "exhausted")
        self.assertEqual(instance._kick_verifying_member.await_count, 4)
        self.assertEqual(clock.sleeps, [5, 15, 30])
        self.assertTrue(db.contains(key))
        self.assertEqual(db.get_cached(key)["kick_attempts"], 4)
        self.assertTrue(
            any("已尝试 4 次" in call.args[2]
                for call in instance._send_group_message.await_args_list)
        )

    async def test_none_or_unknown_kick_response_needs_absent_post_presence(self):
        for response in (None, object()):
            with self.subTest(response_type=type(response).__name__):
                key = "123:456"
                db = MemoryDB({key: _pending_state("round-a")})
                instance = _kick_plugin(db, response=response, presence="present")
                with advancing_clock():
                    result = await instance._kick_with_retries(
                        SimpleNamespace(),
                        123,
                        "456",
                        "round-a",
                        SimpleNamespace(closed=False),
                    )
                self.assertEqual(result, "exhausted")
                self.assertTrue(db.contains(key))

        db = MemoryDB({"123:456": _pending_state("round-a")})
        instance = _kick_plugin(db, response=None, presence="absent")
        result = await instance._kick_with_retries(
            SimpleNamespace(),
            123,
            "456",
            "round-a",
            SimpleNamespace(closed=False),
        )
        self.assertEqual(result, "removed")
        self.assertFalse(db.contains("123:456"))

    async def test_legacy_timeout_and_kick_entrypoints_keep_record(self):
        key = "123:456"
        legacy = {
            "status": "pending",
            "round_id": None,
            "verify_method": "geetest",
        }
        instance = _kick_plugin(MemoryDB({key: legacy}), response=True)
        instance._get_group_config = Mock(return_value={
            "kick_delay": 0,
            "verification_timeout": 300,
        })
        live_event = SimpleNamespace()
        with patch(
            "astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep",
            new=AsyncMock(),
        ):
            await instance._timeout_kick("456", 123, timeout=0, event=live_event)
        self.assertTrue(instance.db.contains(key))
        self.assertEqual(
            await instance._kick_with_retries(
                live_event,
                123,
                "456",
                None,
                SimpleNamespace(closed=False),
            ),
            "legacy",
        )
        self.assertTrue(instance.db.contains(key))
        instance._kick_verifying_member.assert_not_awaited()

    async def test_kick_attempt_cas_stops_before_old_external_action(self):
        key = "123:456"
        replacement = _pending_state("new-round")
        db = KickAttemptRaceDB({key: _pending_state("old-round")}, replacement)
        instance = _kick_plugin(db, response=True)

        result = await instance._kick_with_retries(
            SimpleNamespace(),
            123,
            "456",
            "old-round",
            SimpleNamespace(closed=False),
        )

        self.assertEqual(result, "stale")
        instance._kick_verifying_member.assert_not_awaited()
        self.assertEqual(db.get_cached(key)["round_id"], "new-round")

    async def test_offline_retry_timestamp_is_round_cas_bound(self):
        key = "123:456"
        replacement = _pending_state("new-round")
        db = OfflineRetryRaceDB({key: _pending_state("old-round")}, replacement)
        instance = _kick_plugin(db, response=True)
        instance._event_for_state = Mock(return_value=None)

        with patch(
            "astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            result = await instance._kick_with_retries(
                SimpleNamespace(),
                123,
                "456",
                "old-round",
                SimpleNamespace(closed=False),
            )

        self.assertEqual(result, "stale")
        sleep.assert_not_awaited()
        self.assertEqual(db.get_cached(key)["round_id"], "new-round")
        self.assertIsNone(db.get_cached(key)["next_retry_at"])

    async def test_delayed_non_code_kick_cannot_target_reverified_round(self):
        instance = recall_plugin(prompt_unverified_user=False, max_wrong_answers=1)
        bot = Bot()
        current = event(bot, text="not-a-code")
        await instance._start_verification_process(current, "456", 123)
        old_round = instance.db.get_cached("123:456")["round_id"]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_sleep(delay):
            if delay == 2:
                entered.set()
                await release.wait()

        processing = asyncio.create_task(instance._process_verification_message(current))
        with patch(
            "astrbot_plugin_group_geetest_verify.main.asyncio.sleep",
            side_effect=delayed_sleep,
        ):
            await entered.wait()
            await instance.reverify_command(current)
            release.set()
            await processing

        self.assertNotEqual(instance.db.get_cached("123:456")["round_id"], old_round)
        self.assertEqual(instance.db.get_cached("123:456")["status"], "pending")
        self.assertEqual(bot.kicked, [])

    async def test_complete_identity_rejects_same_instance_different_bot_reply(self):
        instance = recall_plugin()
        owner_bot = Bot()
        owner_event = event(owner_bot, text="8SVK9T")
        await instance._start_verification_process(owner_event, "456", 123)

        foreign_event = event(Bot(), platform_id="bot-a", text="8SVK9T")
        foreign_event.get_self_id = lambda: "10002"
        await instance._process_verification_message(foreign_event)

        self.assertEqual(instance.db.get_cached("123:456")["status"], "pending")
        instance._check_geetest_verify.assert_not_awaited()
        self.assertEqual(owner_bot.kicked, [])

    async def test_leave_notice_is_identity_and_round_scoped(self):
        instance = recall_plugin()
        key = "123:456"
        old = _pending_state(
            "old-round",
            platform_type="aiocqhttp",
            platform_instance_id="bot-a",
            bot_identity="10001",
        )
        instance.db = MemoryDB({key: old})

        foreign = event(Bot(), platform_id="bot-a")
        foreign.get_self_id = lambda: "10002"
        await instance._process_member_decrease(foreign)
        self.assertEqual(instance.db.get_cached(key)["round_id"], "old-round")

        new = dict(old, round_id="new-round")
        await instance.db.set(key, new)
        new_task = Mock()
        new_task.done.return_value = False
        instance._tasks[key] = new_task
        await instance._process_member_decrease(event(Bot(), platform_id="bot-a"))

        self.assertEqual(instance.db.get_cached(key)["round_id"], "new-round")
        new_task.cancel.assert_not_called()

    async def test_real_client_identity_and_platform_calls(self):
        class CQHttp:
            def __init__(self):
                self._wsr_api_clients = {"10001": object()}
                self.calls = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                return {}

        cq = CQHttp()

        class ExtBot:
            id = 987654
            username = "verify_bot"

            def __init__(self):
                self.banned = []

            async def get_chat_member(self, **kwargs):
                return SimpleNamespace(status="member", is_member=True)

            async def ban_chat_member(self, **kwargs):
                self.banned.append(kwargs)
                return True

        telegram = ExtBot()
        adapters = {
            "qq-a": SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp"),
                get_client=lambda: cq,
            ),
            "tg-a": SimpleNamespace(
                meta=lambda: SimpleNamespace(name="telegram"),
                get_client=lambda: telegram,
            ),
        }
        instance = object.__new__(Plugin)
        instance.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: adapters.get(platform_id)
        )

        qq_state = {
            "platform_type": "aiocqhttp",
            "platform_instance_id": "qq-a",
            "bot_identity": "10001",
            "round_id": "qq-round",
        }
        qq_event = instance._resolve_persisted_event(qq_state, 123, "456")
        self.assertIsNotNone(qq_event)
        await instance._call_event_action(qq_event, "get_login_info")
        self.assertEqual(cq.calls, [("get_login_info", {"self_id": "10001"})])
        self.assertIsNone(
            instance._resolve_persisted_event(
                dict(qq_state, bot_identity="10002"), 123, "456"
            )
        )

        class UninitializedExtBot:
            @property
            def id(self):
                raise RuntimeError("ExtBot is not initialized")

            @property
            def username(self):
                raise RuntimeError("ExtBot is not initialized")

        adapters["tg-broken"] = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="telegram"),
            get_client=UninitializedExtBot,
        )
        self.assertIsNone(
            instance._resolve_persisted_event(
                {
                    "platform_type": "telegram",
                    "platform_instance_id": "tg-broken",
                    "bot_identity": "987654",
                    "round_id": "broken-round",
                },
                123,
                "456",
            )
        )

        tg_state = {
            "platform_type": "telegram",
            "platform_instance_id": "tg-a",
            "bot_identity": "987654",
            "round_id": "tg-round",
        }
        tg_event = instance._resolve_persisted_event(tg_state, 123, "456")
        self.assertIsNotNone(tg_event)
        self.assertEqual(await instance._get_member_presence(tg_event, 123, "456"), "present")
        await instance._kick_member(tg_event, 123, "456")
        self.assertEqual(telegram.banned, [{"chat_id": 123, "user_id": 456}])


class RecoveryAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def timer_plugin(self, deadline):
        instance = _kick_plugin(MemoryDB({"123:456": _pending_state(
            "round-timer", verification_deadline=deadline, verify_method="math", question="1+1=?"
        )}))
        instance._get_group_config = Mock(return_value={
            "kick_delay": 0, "verification_timeout": 300,
            "recall_verification_messages": False,
        })
        instance.kick_delay = 0
        instance.timeout_reminder_math = "{at_user} {question}"
        instance.failure_message = "{at_user} {countdown} failed"
        instance._resolve_persisted_event = Mock(return_value=SimpleNamespace())
        instance._kick_with_retries = AsyncMock(return_value="removed")
        return instance

    async def test_live_kick_and_recovery_share_one_retry_budget(self):
        instance = self.timer_plugin(time.time() - 1)
        del instance._kick_with_retries
        entered, release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep
        recorded_attempts = []

        async def presence(*_args):
            entered.set()
            await release.wait()
            return "present"

        async def kick(*_args):
            recorded_attempts.append(instance.db.get_cached("123:456")["kick_attempts"])
            await real_sleep(0)
            return False

        instance._get_member_presence = AsyncMock(side_effect=presence)
        instance._kick_verifying_member = AsyncMock(side_effect=kick)
        with advancing_clock():
            # Wrong-answer handlers enter the kick flow outside the timer map.
            live = asyncio.create_task(instance._kick_with_retries(
                SimpleNamespace(), 123, "456", "round-timer", None,
            ))
            recovered = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await instance._recover_pending_verifications()
                recovered = instance._tasks["123:456"]
                for _ in range(3):
                    await real_sleep(0)
                release.set()
                await asyncio.wait_for(asyncio.gather(live, recovered), 1)
            finally:
                release.set()
                for task in (live, recovered):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*[t for t in (live, recovered) if t is not None],
                                     return_exceptions=True)

        self.assertEqual(recorded_attempts, [1, 2, 3, 4])
        self.assertEqual(instance.db.get_cached("123:456")["kick_attempts"], 4)
        self.assertEqual(instance.db.get_cached("123:456")["status"], "pending")

    async def test_reload_near_deadline_uses_only_remaining_seconds(self):
        deadline = time.time() + 2
        instance = self.timer_plugin(deadline)
        called_at = []
        async def kick(*args):
            called_at.append(time.time())
        instance._kick_with_retries.side_effect = kick
        with advancing_clock() as clock:
            await instance._recover_pending_verifications(startup=True)
            await asyncio.gather(*list(instance._tasks.values()))
        self.assertEqual(len(called_at), 1)
        self.assertAlmostEqual(called_at[0], deadline, places=4)
        self.assertLess(sum(clock.sleeps), 2.01)
        self.assertEqual(instance.db.get_cached("123:456")["verification_deadline"], deadline)

    async def test_expired_recovery_does_not_start_a_new_timeout(self):
        instance = self.timer_plugin(time.time() - 100)
        with advancing_clock() as clock:
            await instance._recover_pending_verifications(startup=True)
            await asyncio.gather(*list(instance._tasks.values()))
        self.assertEqual(clock.sleeps, [])
        instance._kick_with_retries.assert_awaited_once()

    async def test_legacy_startup_summary_does_not_create_tasks(self):
        instance = self.timer_plugin(time.time())
        instance.db = MemoryDB({
            "123:1": {"status": "pending"},
            "123:2": {"status": "pending"},
            "789:3": {"status": "pending"},
        })
        with self.assertLogs("astrbot_plugin_group_geetest_verify.core.verifier", level="WARNING") as log:
            await instance._recover_pending_verifications(startup=True)
        self.assertEqual(instance._tasks, {})
        self.assertEqual(len(instance.db.all_keys()), 3)
        self.assertIn("群 123: 2 人", " ".join(log.output))
        self.assertIn("群 789: 1 人", " ".join(log.output))
        instance._resolve_persisted_event.assert_not_called()

    async def test_exact_account_waits_offline_and_recovers_after_reconnect(self):
        client = SimpleNamespace(_wsr_api_clients={}, call_action=AsyncMock())
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: client)
        instance = _kick_plugin(MemoryDB({"123:456": _pending_state("round-offline")}))
        instance.context = SimpleNamespace(
            get_platform_inst=Mock(side_effect=lambda pid: adapter if pid == "bot-a" else None),
            get_platform=Mock(side_effect=AssertionError("must not choose another bot")),
        )
        instance._timeout_kick = AsyncMock()
        await instance._recover_pending_verifications()
        self.assertEqual(instance._tasks, {})
        client._wsr_api_clients["different-account"] = object()
        await instance._recover_pending_verifications()
        self.assertEqual(instance._tasks, {})
        stale_event = SimpleNamespace(get_platform_name=lambda:"aiocqhttp", get_platform_id=lambda:"bot-a", get_self_id=lambda:"10001")
        # Exercise the real resolver, including rejection of the old event.
        self.assertIsNone(Plugin._event_for_state(instance, stale_event, instance.db.get_cached("123:456"),123,"456"))
        client._wsr_api_clients["10001"] = object()
        await instance._recover_pending_verifications()
        await asyncio.gather(*list(instance._tasks.values()))
        instance._timeout_kick.assert_awaited_once()
        self.assertEqual(instance._timeout_kick.await_args.kwargs["event"].get_self_id(), "10001")
        self.assertEqual(instance.db.get_cached("123:456")["kick_attempts"], 0)
        instance.context.get_platform.assert_not_called()

    async def test_uninitialized_telegram_identity_is_deferred(self):
        class OfflineBot:
            @property
            def id(self):
                raise RuntimeError("ExtBot is not initialized")
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="telegram"),get_client=lambda:OfflineBot())
        instance = self.timer_plugin(time.time())
        del instance._resolve_persisted_event
        instance.context = SimpleNamespace(get_platform_inst=Mock(return_value=adapter))
        state = _pending_state("tg-round", platform_type="telegram")
        self.assertIsNone(instance._resolve_persisted_event(state,123,"456"))
        self.assertEqual(instance.db.get_cached("123:456")["status"], "pending")

    async def test_offline_retry_does_not_consume_a_kick_attempt(self):
        instance = _kick_plugin(MemoryDB({"123:456": _pending_state("round-a")}),response=True)
        instance._event_for_state = Mock(side_effect=[None, SimpleNamespace()])
        with advancing_clock() as clock:
            result = await instance._kick_with_retries(SimpleNamespace(),123,"456","round-a",None)
        self.assertEqual(result, "removed")
        self.assertEqual(clock.sleeps, [30])
        instance._kick_verifying_member.assert_awaited_once()

    async def test_success_or_new_round_during_retry_stops_old_kick(self):
        for verified in (True, False):
            with self.subTest(verified=verified):
                instance = _kick_plugin(MemoryDB({"123:456":_pending_state("round-a")}),response=False)
                with advancing_clock():
                    advancing_sleep = asyncio.sleep
                    async def finish_during_wait(delay):
                        await advancing_sleep(delay)
                        await instance.db.set("123:456",_pending_state("round-a" if verified else "new-round",status="verified" if verified else "pending"))
                    with patch("astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep",side_effect=finish_during_wait):
                        result = await instance._kick_with_retries(SimpleNamespace(),123,"456","round-a",None)
                self.assertEqual(result,"stale")
                instance._kick_verifying_member.assert_awaited_once()
                self.assertTrue(instance.db.contains("123:456"))

    async def test_wrong_answer_keeps_deadline_and_admin_reverify_creates_new_round(self):
        instance = recall_plugin()
        owner = event(Bot())
        with advancing_clock() as clock:
            await instance._start_verification_process(owner,"456",123)
            original = dict(instance.db.get_cached("123:456"))
            clock.now += 100
            instance._check_geetest_verify.return_value = False
            await instance._process_verification_message(owner)
            current = instance.db.get_cached("123:456")
            self.assertEqual(current["round_id"],original["round_id"])
            self.assertEqual(current["verification_deadline"],original["verification_deadline"])
            await instance.reverify_command(owner)
            current = instance.db.get_cached("123:456")
            self.assertNotEqual(current["round_id"],original["round_id"])
            self.assertGreater(current["verification_deadline"],original["verification_deadline"])
            self.assertEqual(current["kick_attempts"],0)
        tasks=list(instance._tasks.values())
        await asyncio.gather(*tasks,return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
