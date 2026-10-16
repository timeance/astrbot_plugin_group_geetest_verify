import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_new_member_permission_flow import Plugin, REPO_ROOT
from test_geetest_verification import MemoryDB
from test_verification_bot_config import FakeConfig
from astrbot_plugin_group_geetest_verify.page_service import PageService


class Bot:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.kicked = []
        self.delete_errors = set()
        self.kick_error = False
        self.on_kick = None

    async def call_action(self, action, **kwargs):
        if action == "send_group_msg":
            message_id = len(self.sent) + 1
            self.sent.append((message_id, kwargs["message"]))
            return {"data": {"message_id": message_id}}
        if action == "delete_msg":
            self.deleted.append(kwargs["message_id"])
            if kwargs["message_id"] in self.delete_errors:
                raise RuntimeError("message already deleted or permission denied")
        if action == "set_group_kick":
            if self.kick_error:
                raise RuntimeError("kick denied")
            self.kicked.append(kwargs["user_id"])
            if self.on_kick:
                await self.on_kick()
        return {}


def event(bot, *, text="8SVK9T", uid="456", gid=123, platform_id="bot-a"):
    return SimpleNamespace(
        bot=bot,
        get_platform_name=lambda: "aiocqhttp",
        get_platform_id=lambda: platform_id,
        get_self_id=lambda: platform_id,
        get_sender_id=lambda: uid,
        message_str=text,
        message_obj=SimpleNamespace(raw_message={
            "group_id": gid, "user_id": int(uid), "message_id": 90001,
            "message": [{"type": "at", "data": {"qq": uid}}],
        }),
        stop_event=Mock(),
    )


def plugin(*, enabled=True, geetest=True, **settings):
    instance = object.__new__(Plugin)
    instance.config = FakeConfig(
        api_key="test-key", enable_geetest_verify=geetest,
        recall_verification_messages=enabled,
        group_configs=[{"group_id": "123", "enabled": True}],
        **settings,
    )
    instance._load_config()
    instance._tasks = {}
    instance._verification_messages = {}
    instance._new_member_inflight = set()
    instance._data_dir = REPO_ROOT
    instance.db = MemoryDB()
    instance._timeout_kick = AsyncMock()
    instance._create_geetest_verify = AsyncMock(return_value="https://verify.example/check")
    instance._check_geetest_verify = AsyncMock(return_value=True)
    instance._generate_math_problem = Mock(return_value=("1 + 1 = ?", 2))
    instance._check_permission = AsyncMock(return_value=True)
    instance._is_current_bot_group_admin = AsyncMock(return_value=True)
    return instance


class RecallConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_inheritance_override_and_page_round_trip(self):
        instance = plugin(enabled=False)
        self.assertFalse(instance.recall_verification_messages)
        self.assertFalse(instance._get_group_config(123)["recall_verification_messages"])
        instance.config["recall_verification_messages"] = True
        instance._load_config()
        self.assertTrue(instance._get_group_config(123)["recall_verification_messages"])
        self.assertTrue(instance._get_group_config(999)["recall_verification_messages"])

        service = PageService(instance.config, REPO_ROOT, SimpleNamespace(), instance._load_config)
        self.assertFalse(service.global_schema["recall_verification_messages"]["default"])
        self.assertEqual(service.default_schema["recall_verification_messages"]["type"], "bool")
        service.save_group_config("123", {"enabled": True, "recall_verification_messages": False})
        reread = await service.get_group_config("123")
        self.assertFalse(reread["config"]["recall_verification_messages"])
        self.assertFalse(instance._get_group_config(123)["recall_verification_messages"])
        self.assertTrue(instance.recall_verification_messages)

        with tempfile.TemporaryDirectory() as directory:
            service.plugin_dir = Path(directory)
            service._internal_cache = {}
            service.save_group_config("__default__", {}, {"recall_verification_messages": False})
        self.assertFalse(instance.recall_verification_messages)
        instance._update_group_config(789, enabled=True)
        self.assertFalse(instance._get_group_config(789)["recall_verification_messages"])


class RecallFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = plugin()
        self.bot = Bot()
        self.event = event(self.bot)

    async def asyncTearDown(self):
        tasks = list(self.plugin._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self):
        await self.plugin._start_verification_process(self.event, "456", 123)

    async def timeout(self):
        with patch("astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep", new=AsyncMock()):
            await Plugin._timeout_kick(self.plugin, "456", 123, 300, self.event)

    def assert_only_result_remains(self):
        self.assertEqual(set(self.bot.deleted), {mid for mid, _ in self.bot.sent[:-1]})
        self.assertNotIn(self.bot.sent[-1][0], self.bot.deleted)
        self.assertNotIn(90001, self.bot.deleted)
        self.assertFalse(self.plugin._verification_messages)

    async def test_success_cleans_level_link_error_and_keeps_welcome_image(self):
        self.plugin.config.update(enable_level_verify=True, welcome_image="https://verify.example/welcome.png")
        self.plugin._load_config()
        self.plugin._get_user_level = AsyncMock(return_value=0)
        with patch("astrbot_plugin_group_geetest_verify.main.asyncio.sleep", new=AsyncMock()):
            await self.plugin._process_new_member(self.event)
        await self.plugin._process_verification_message(event(self.bot, text="hello"))
        self.assertEqual(len(self.bot.sent), 3)
        self.assertFalse(self.bot.deleted)
        await self.plugin._process_verification_message(self.event)
        self.assert_only_result_remains()
        self.assertIn("CQ:image", self.bot.sent[-1][1])
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "verified")

    async def test_math_success_keeps_result(self):
        self.plugin = plugin(geetest=False)
        await self.start()
        await self.plugin._process_verification_message(event(self.bot, text="2"))
        self.assert_only_result_remains()

    async def test_level_pass_notification_is_preserved(self):
        self.plugin.config["enable_level_verify"] = True
        self.plugin._load_config()
        self.plugin._get_user_level = AsyncMock(return_value=42)
        with patch("astrbot_plugin_group_geetest_verify.main.asyncio.sleep", new=AsyncMock()):
            await self.plugin._process_new_member(self.event)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertFalse(self.bot.deleted)
        self.assertFalse(self.plugin._verification_messages)

    async def test_low_level_hint_is_cleaned_after_timeout(self):
        self.plugin.config["enable_level_verify"] = True
        self.plugin._load_config()
        self.plugin._get_user_level = AsyncMock(return_value=1)
        with patch("astrbot_plugin_group_geetest_verify.main.asyncio.sleep", new=AsyncMock()):
            await self.plugin._process_new_member(self.event)
        await self.timeout()
        self.assert_only_result_remains()
        self.assertEqual(self.bot.kicked, [456])
        self.assertFalse(self.plugin.db.contains("123:456"))

    async def test_math_reminder_and_timeout_countdown_are_cleaned(self):
        self.plugin = plugin(geetest=False)
        await self.start()
        await self.timeout()
        self.assertEqual(len(self.bot.sent), 4)
        self.assert_only_result_remains()

    async def test_success_during_kick_countdown_cleans_warning_without_kicking(self):
        await self.start()
        sleeps = 0

        async def sleep_and_verify(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                await self.plugin._process_verification_message(self.event)

        with patch("astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep", side_effect=sleep_and_verify):
            await Plugin._timeout_kick(self.plugin, "456", 123, 300, self.event)
        self.assert_only_result_remains()
        self.assertFalse(self.bot.kicked)
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "verified")

    async def test_geetest_fallback_math_prompt_is_cleaned(self):
        self.plugin._create_geetest_verify.return_value = None
        await self.start()
        await self.plugin._process_verification_message(event(self.bot, text="2"))
        self.assert_only_result_remains()

    async def test_wrong_code_retry_preserves_round_until_success(self):
        await self.start()
        original = self.plugin._find_verification_messages(self.event, 123, "456")
        self.plugin._check_geetest_verify.return_value = False
        await self.plugin._process_verification_message(self.event)
        self.assertIs(self.plugin._find_verification_messages(self.event, 123, "456"), original)
        self.assertFalse(self.bot.deleted)
        self.assertEqual(self.plugin.db.get_cached("123:456")["wrong_count"], 1)
        self.plugin._check_geetest_verify.return_value = True
        await self.plugin._process_verification_message(self.event)
        self.assert_only_result_remains()

    async def test_wrong_code_limit_preserves_only_final_kick_notice(self):
        self.plugin = plugin(max_wrong_answers=1)
        await self.start()
        self.plugin._check_geetest_verify.return_value = False
        with patch("astrbot_plugin_group_geetest_verify.main.asyncio.sleep", new=AsyncMock()):
            await self.plugin._process_verification_message(self.event)
        self.assert_only_result_remains()
        self.assertEqual(self.bot.kicked, [456])

    async def test_non_code_limit_cleans_warnings_and_sends_final_notice(self):
        for geetest in (True, False):
            with self.subTest(geetest=geetest):
                self.plugin = plugin(geetest=geetest, prompt_unverified_user=False, max_wrong_answers=1)
                self.bot = Bot()
                self.event = event(self.bot)
                await self.start()
                with patch("astrbot_plugin_group_geetest_verify.main.asyncio.sleep", new=AsyncMock()):
                    await self.plugin._process_verification_message(event(self.bot, text="hello"))
                self.assert_only_result_remains()
                self.assertEqual(self.bot.kicked, [456])

    async def test_bypass_keeps_its_notification(self):
        await self.start()
        await self.plugin.bypass_command(self.event)
        self.assert_only_result_remains()
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "bypassed")

    async def test_reverify_cleans_old_round_and_keeps_new_prompt_and_command_reply(self):
        await self.start()
        old = self.plugin._find_verification_messages(self.event, 123, "456")
        await self.plugin.reverify_command(self.event)
        new = self.plugin._find_verification_messages(self.event, 123, "456")
        self.assertTrue(old.closed)
        self.assertIsNot(old, new)
        self.assertEqual(self.bot.deleted, [1])
        self.assertEqual(new.message_ids, {2})
        await self.plugin._process_verification_message(self.event)
        self.assertEqual(set(self.bot.deleted), {1, 2})
        self.assertNotIn(3, self.bot.deleted)  # command reply

    async def test_member_leave_cleans_even_before_pending_state_exists(self):
        message_round = self.plugin._verification_message_round(self.event, 123, "456", self.plugin._get_group_config(123))
        await self.plugin._send_verification_message(self.event, 123, "level hint", message_round)
        await self.plugin._process_member_decrease(self.event)
        self.assertEqual(self.bot.deleted, [1])
        self.assertTrue(message_round.closed)

    async def test_member_leave_cleans_pending_round_and_cancels_timer(self):
        await self.start()
        await self.plugin._process_member_decrease(self.event)
        self.assertEqual(self.bot.deleted, [1])
        self.assertFalse(self.plugin.db.contains("123:456"))
        self.assertNotIn("123:456", self.plugin._tasks)

    async def test_own_kick_notice_does_not_cancel_final_result(self):
        await self.start()
        self.bot.on_kick = lambda: self.plugin._process_member_decrease(self.event)
        task = asyncio.create_task(self.timeout())
        self.plugin._tasks["123:456"] = task
        await task
        self.assert_only_result_remains()

    async def test_kick_failure_keeps_pending_and_sends_no_final_notice(self):
        await self.start()
        self.bot.kick_error = True
        await self.timeout()
        self.assertEqual(len(self.bot.sent), 3)
        self.assertFalse(self.bot.deleted)
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "pending")
        self.assertFalse(self.plugin._find_verification_messages(self.event, 123, "456").kicking)

    async def test_recall_failure_does_not_block_other_recalls_or_final_result(self):
        await self.start()
        self.bot.delete_errors.add(1)
        await self.timeout()
        self.assert_only_result_remains()
        self.assertEqual(self.bot.kicked, [456])

    async def test_switch_off_leaves_all_messages_and_does_not_delete_user_code(self):
        self.plugin = plugin(enabled=False)
        await self.start()
        await self.plugin._process_verification_message(self.event)
        self.assertFalse(self.bot.deleted)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertFalse(self.plugin._verification_messages)

    async def test_delayed_verification_response_does_not_finish_new_round(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_check(*args):
            entered.set()
            await release.wait()
            return True

        self.plugin._check_geetest_verify.side_effect = delayed_check
        pending = asyncio.create_task(self.plugin._process_verification_message(self.event))
        await entered.wait()
        await self.plugin.reverify_command(self.event)
        release.set()
        await pending
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "pending")
        self.assertEqual(self.bot.deleted, [1])
        self.assertEqual(len(self.bot.sent), 3)


class RecallConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_group_and_user_isolation(self):
        instance = plugin()
        bot_a, bot_b = Bot(), Bot()
        events = [event(bot_a), event(bot_b, platform_id="bot-b"),
                  event(bot_a, gid=789), event(bot_a, uid="999")]
        rounds = []
        for current in events:
            raw = current.message_obj.raw_message
            message_round = instance._verification_message_round(current, raw["group_id"], current.get_sender_id(), {"recall_verification_messages": True})
            rounds.append(message_round)
            await instance._send_verification_message(current, raw["group_id"], "prompt", message_round)
        await instance._finish_verification_messages(rounds[0])
        self.assertEqual(bot_a.deleted, [1])
        self.assertFalse(bot_b.deleted)
        self.assertEqual(len(instance._verification_messages), 3)

    async def test_cancelled_send_response_is_recalled_after_round_closes(self):
        instance = plugin()
        bot = Bot()
        current = event(bot)
        message_round = instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True})
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(*args):
            entered.set()
            await release.wait()
            return {"message_id": 10}

        instance._send_group_message = delayed_send
        sending = asyncio.create_task(instance._send_verification_message(current, 123, "prompt", message_round))
        await entered.wait()
        sending.cancel()
        await instance._finish_verification_messages(message_round)
        replacement = instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True})
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await sending
        self.assertEqual(bot.deleted, [10])
        await instance._finish_verification_messages(message_round)
        self.assertIs(instance._find_verification_messages(current, 123, "456"), replacement)

    async def test_cancelled_retry_send_remains_tracked_until_round_finishes(self):
        instance = plugin()
        bot = Bot()
        current = event(bot)
        message_round = instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True})
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_send(*args):
            entered.set()
            await release.wait()
            return {"data": {"message_id": 10}}

        instance._send_group_message = delayed_send
        sending = asyncio.create_task(instance._send_verification_message(current, 123, "prompt", message_round))
        await entered.wait()
        sending.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await sending
        self.assertFalse(bot.deleted)
        await instance._finish_verification_messages(message_round)
        self.assertEqual(bot.deleted, [10])

    async def test_missing_message_ids_and_send_failure_do_not_block_cleanup(self):
        instance = plugin()
        bot = Bot()
        current = event(bot)
        message_round = instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True})
        instance._send_group_message = AsyncMock(side_effect=[None, {}, {"data": {}}, {"message_id": 42}])
        for _ in range(4):
            await instance._send_verification_message(current, 123, "prompt", message_round)
        await instance._finish_verification_messages(message_round)
        self.assertEqual(bot.deleted, [42])

    async def test_failed_delete_response_is_logged_and_other_ids_are_attempted(self):
        instance = plugin()
        current = event(Bot())
        message_round = instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True})
        message_round.message_ids.update({1, 2})
        message_round.call_action = AsyncMock(return_value={"status": "failed", "retcode": 100})
        with self.assertLogs("astrbot_plugin_group_geetest_verify.core.recall", level="WARNING") as logs:
            await instance._finish_verification_messages(message_round)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(message_round.call_action.await_count, 2)

    async def test_telegram_does_not_create_a_qq_recall_round(self):
        instance = plugin()
        current = event(Bot())
        current.get_platform_name = lambda: "telegram"
        self.assertIsNone(instance._verification_message_round(current, 123, "456", {"recall_verification_messages": True}))
        self.assertFalse(instance._verification_messages)


if __name__ == "__main__":
    unittest.main()
