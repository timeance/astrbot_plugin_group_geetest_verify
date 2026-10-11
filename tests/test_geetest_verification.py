import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_new_member_permission_flow import Plugin, REPO_ROOT
from test_verification_bot_config import FakeConfig
from astrbot_plugin_group_geetest_verify.page_service import PageService


class MemoryDB:
    def __init__(self, state=None):
        self.states = state or {}

    def get_cached(self, key):
        return self.states.get(key)

    def contains(self, key):
        return key in self.states

    async def set(self, key, value):
        self.states[key] = value

    async def update_field(self, key, field, value):
        self.states[key][field] = value

    async def delete(self, key):
        del self.states[key]


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.json = AsyncMock(return_value=payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def api_plugin(payload, status=200):
    plugin = object.__new__(Plugin)
    plugin.api_key = "test-key"
    plugin.api_base_url = " https://verify.example/api/ "
    plugin.session = SimpleNamespace(post=Mock(return_value=FakeResponse(payload, status)))
    return plugin


def flow_plugin(geetest=True, url="https://verify.example/check?token=test#verify"):
    plugin = object.__new__(Plugin)
    plugin.api_key = "test-key"
    plugin._tasks = {}
    plugin.db = MemoryDB()
    plugin._load_config = Mock()
    config = {
        "enable_geetest_verify": geetest,
        "max_wrong_answers": 5,
        "verification_timeout": 300,
        "prompt_unverified_user": True,
    }
    plugin._get_group_config = Mock(return_value=config)
    plugin._timeout_kick = AsyncMock()
    plugin._create_geetest_verify = AsyncMock(return_value=url)
    plugin._generate_math_problem = Mock(return_value=("1 - 1 = ?", 0))
    plugin._format_user_mention = Mock(return_value="@user")
    plugin._send_group_message = AsyncMock()
    plugin._send_group_message_with_image = AsyncMock()
    plugin._get_platform = Mock(return_value="aiocqhttp")
    plugin._get_group_id = Mock(return_value=123)
    plugin._event_matches_verification_platform = Mock(return_value=True)
    plugin.geetest_new_member_prompt = "{at_user} {timeout} {url}"
    plugin.geetest_wrong_code_prompt = "{at_user} {url} {remaining}"
    plugin.new_member_prompt = "{at_user} {timeout} {question}"
    plugin.wrong_answer_prompt = "{at_user} {question} {remaining}"
    plugin.error_verification = "{at_user} {timeout} pending"
    plugin.welcome_message = "{at_user} welcome"
    plugin.welcome_image = ""
    return plugin, config


class GeetestAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_both_server_response_formats_pass(self):
        for payload in ({"code": 0, "passed": True}, {"code": 0, "data": {"passed": True}}):
            with self.subTest(payload=payload):
                plugin = api_plugin(payload)
                self.assertTrue(await plugin._check_geetest_verify(123, "456", "8SVK9T"))
                args, kwargs = plugin.session.post.call_args
                self.assertEqual(args[0], "https://verify.example/api/verify/check")
                self.assertEqual(kwargs["json"], {"group_id": "123", "user_id": "456", "code": "8SVK9T"})

    async def test_failed_or_malformed_responses_do_not_pass(self):
        for payload in (
            {"code": 1, "passed": True},
            {"code": 0, "passed": False, "data": {"passed": True}},
            {"code": 0, "data": {"passed": False}},
            {"code": 0, "passed": "false"},
            {"code": 0, "passed": 1},
            {"code": 0, "data": None},
            {"code": 0, "data": "invalid"},
            {},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(await api_plugin(payload)._check_geetest_verify(123, "456", "8SVK9T"))

    async def test_complete_links_keep_query_fragment_and_server_host(self):
        for returned, expected in (
            ("https://public.example/check?token=abc#verify", "https://public.example/check?token=abc#verify"),
            ("/check?token=abc#verify", "https://verify.example/check?token=abc#verify"),
            ("check?token=abc#verify", "https://verify.example/api/check?token=abc#verify"),
        ):
            with self.subTest(returned=returned):
                plugin = api_plugin({"code": 0, "data": {"url": returned}})
                self.assertEqual(await plugin._create_geetest_verify(123, "456"), expected)
                self.assertEqual(plugin.session.post.call_args.args[0], "https://verify.example/api/verify/create")

    async def test_missing_or_invalid_links_return_none(self):
        for payload in ({"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": {"url": ""}}, {"code": 0, "data": {"url": "javascript:alert(1)"}}):
            with self.subTest(payload=payload):
                self.assertIsNone(await api_plugin(payload)._create_geetest_verify(123, "456"))

    async def test_http_error_does_not_pass(self):
        self.assertFalse(await api_plugin({"code": 0, "passed": True}, 503)._check_geetest_verify(123, "456", "8SVK9T"))


class VerificationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for task in getattr(self, "plugin", SimpleNamespace(_tasks={}))._tasks.values():
            task.cancel()
        await asyncio.gather(*getattr(self, "plugin", SimpleNamespace(_tasks={}))._tasks.values(), return_exceptions=True)

    async def test_geetest_starts_without_generating_math_and_sends_complete_url(self):
        self.plugin, config = flow_plugin()
        await self.plugin._start_verification_process(object(), "456", 123, group_config=config)
        self.plugin._generate_math_problem.assert_not_called()
        self.assertEqual(self.plugin.db.get_cached("123:456")["verify_method"], "geetest")
        self.assertIn("https://verify.example/check?token=test#verify", self.plugin._send_group_message.await_args.args[2])

    async def test_retry_api_failure_falls_back_to_nonempty_math_preserving_count(self):
        self.plugin, config = flow_plugin(url=None)
        await self.plugin.db.set("123:456", {"wrong_count": 2, "verify_method": "geetest"})
        await self.plugin._start_verification_process(object(), "456", 123, is_new_member=False, group_config=config)
        state = self.plugin.db.get_cached("123:456")
        self.assertEqual((state["question"], state["answer"], state["verify_method"], state["wrong_count"]), ("1 - 1 = ?", 0, "math", 2))
        self.assertIn("1 - 1 = ?", self.plugin._send_group_message.await_args.args[2])

    async def test_math_mode_generates_question_and_accepts_zero(self):
        self.plugin, config = flow_plugin(geetest=False)
        await self.plugin._start_verification_process(object(), "456", 123, group_config=config)
        self.plugin._create_geetest_verify.assert_not_awaited()
        event = self.event("0")
        await self.plugin._process_verification_message(event)
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "verified")
        event.stop_event.assert_called_once()

    def event(self, text):
        return SimpleNamespace(message_str=text, get_sender_id=lambda: "456", message_obj=SimpleNamespace(raw_message={}), stop_event=Mock())

    async def test_nested_success_response_marks_user_verified(self):
        self.plugin, config = flow_plugin()
        api = api_plugin({"code": 0, "data": {"passed": True}})
        self.plugin.api_base_url = api.api_base_url
        self.plugin.session = api.session
        await self.plugin._start_verification_process(object(), "456", 123, group_config=config)
        event = self.event("验证码：8SVK9T")
        await self.plugin._process_verification_message(event)
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "verified")
        self.assertNotIn("123:456", self.plugin._tasks)
        self.plugin._send_group_message_with_image.assert_awaited_once()
        event.stop_event.assert_called_once()

    async def test_long_alphanumeric_message_does_not_submit_partial_code(self):
        self.plugin, config = flow_plugin()
        self.plugin._check_geetest_verify = AsyncMock(return_value=True)
        await self.plugin._start_verification_process(object(), "456", 123, group_config=config)
        await self.plugin._process_verification_message(self.event("prefix8SVK9Tsuffix"))
        self.plugin._check_geetest_verify.assert_not_awaited()
        self.assertEqual(self.plugin.db.get_cached("123:456")["status"], "pending")

    async def test_geetest_reminder_sends_complete_url(self):
        self.plugin, config = flow_plugin()
        # Exercise the real timeout method rather than the starter's mocked timer.
        config["kick_delay"] = 0
        self.plugin.kick_delay = 0
        self.plugin.timeout_reminder_geetest = "{at_user} {url}"
        self.plugin.failure_message = "{at_user} {countdown} failed"
        self.plugin.kick_message = "{at_user} kicked"
        self.plugin._kick_member = AsyncMock()
        await self.plugin.db.set("123:456", {"verify_method": "geetest"})
        with patch("astrbot_plugin_group_geetest_verify.core.verifier.asyncio.sleep", new=AsyncMock()):
            await Plugin._timeout_kick(self.plugin, "456", 123, 300, object())
        reminder = self.plugin._send_group_message.await_args_list[0].args[2]
        self.assertIn("https://verify.example/check?token=test#verify", reminder)
        self.plugin._kick_member.assert_awaited_once()


class VerificationConfigTests(unittest.TestCase):
    def test_missing_default_fields_inherit_global_geetest_setting(self):
        config = FakeConfig(enable_geetest_verify=True, api_base_url="https://verify.example", group_configs=[])
        service = PageService(config, REPO_ROOT, SimpleNamespace())
        service._internal_cache = {"disposal": {"default": {"enabled": True}}}
        self.assertTrue(service._get_default_group_config()["enable_geetest_verify"])
        service._internal_cache["disposal"]["default"]["enable_geetest_verify"] = False
        self.assertFalse(service._get_default_group_config()["enable_geetest_verify"])

    def test_group_command_save_preserves_new_global_config_and_reloads(self):
        plugin = object.__new__(Plugin)
        plugin.config = FakeConfig(enable_geetest_verify=True, api_key="new-key", group_configs=[])
        plugin._load_config()
        plugin.enable_geetest_verify = False
        plugin.api_key = "stale-key"
        plugin._update_group_config(123, enabled=True, verification_platform_id="bot-a")
        self.assertTrue(plugin.config["enable_geetest_verify"])
        self.assertEqual(plugin.config["api_key"], "new-key")
        self.assertTrue(plugin.enable_geetest_verify)
        self.assertEqual(plugin.api_key, "new-key")
        self.assertTrue(plugin.config["group_configs"][0]["enable_geetest_verify"])
        self.assertEqual(plugin.config["group_configs"][0]["group_id"], "123")
        self.assertEqual(plugin.config["group_configs"][0]["verification_platform_id"], "bot-a")


if __name__ == "__main__":
    unittest.main()
