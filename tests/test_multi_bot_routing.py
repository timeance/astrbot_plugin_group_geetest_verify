import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_new_member_permission_flow import Plugin, build_plugin, make_event


class FakeActionClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.responses.get(action, {})


def make_bound_event(client, *, self_id="10002", platform_id="八千代"):
    return SimpleNamespace(
        bot=client,
        message_obj=SimpleNamespace(
            raw_message={"group_id": 123, "self_id": self_id}
        ),
        get_platform_name=lambda: "aiocqhttp",
        get_platform_id=lambda: platform_id,
        get_self_id=lambda: self_id,
    )


class MultiBotRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_qq_actions_use_the_event_bound_client(self):
        selected_client = FakeActionClient(
            {"get_stranger_info": {"data": {"qqLevel": 42}}}
        )
        wrong_client = FakeActionClient()
        plugin = object.__new__(Plugin)
        plugin.context = SimpleNamespace(
            get_platform=Mock(
                side_effect=AssertionError("deprecated type lookup was used")
            ),
            get_platform_inst=Mock(
                return_value=SimpleNamespace(get_client=lambda: wrong_client)
            ),
        )
        event = make_bound_event(selected_client)

        await plugin._send_group_message(event, 123, "hello")
        level = await plugin._get_user_level(event, "20001")
        await plugin._delete_message(event, 30001)
        await plugin._kick_member(event, 123, "20001")

        self.assertEqual(level, 42)
        self.assertEqual(
            selected_client.calls,
            [
                (
                    "send_group_msg",
                    {"group_id": 123, "message": "hello"},
                ),
                ("get_stranger_info", {"user_id": 20001}),
                ("delete_msg", {"message_id": 30001}),
                (
                    "set_group_kick",
                    {
                        "group_id": 123,
                        "user_id": 20001,
                        "reject_add_request": False,
                    },
                ),
            ],
        )
        self.assertEqual(wrong_client.calls, [])
        plugin.context.get_platform.assert_not_called()
        plugin.context.get_platform_inst.assert_not_called()

    async def test_platform_id_fallback_uses_exact_adapter_instance(self):
        selected_client = FakeActionClient()
        plugin = object.__new__(Plugin)
        plugin.context = SimpleNamespace(
            get_platform=Mock(
                side_effect=AssertionError("deprecated type lookup was used")
            ),
            get_platform_inst=Mock(
                return_value=SimpleNamespace(get_client=lambda: selected_client)
            ),
        )
        event = make_bound_event(None, platform_id="八千代")

        await plugin._send_group_message(event, 123, "hello")

        plugin.context.get_platform_inst.assert_called_once_with("八千代")
        plugin.context.get_platform.assert_not_called()
        self.assertEqual(selected_client.calls[0][0], "send_group_msg")

    async def test_non_designated_bot_does_not_process_verification_reply(self):
        plugin = build_plugin(verification_platform_id="八千代")
        plugin.db.get_cached = Mock(return_value={"status": "pending"})
        event = make_event(
            {"group_id": 123, "message_id": 30001}, platform_id="zar"
        )
        event.get_sender_id = lambda: "20001"
        event.message_str = "2"

        await plugin._process_verification_message(event)

        plugin.db.get_cached.assert_not_called()
        plugin._send_group_message.assert_not_awaited()

    async def test_non_designated_bot_does_not_run_plugin_command(self):
        plugin = build_plugin(verification_platform_id="八千代")
        plugin._check_permission = AsyncMock(return_value=True)
        plugin._update_group_config = Mock()
        event = make_event({"group_id": 123}, platform_id="zar")
        event.get_sender_id = lambda: "20001"

        await plugin.enable_verify_command(event)

        plugin._check_permission.assert_not_awaited()
        plugin._update_group_config.assert_not_called()
        plugin._send_group_message.assert_not_awaited()

    def test_unicode_astrbot_id_matches_platform_id(self):
        plugin = build_plugin()
        event = make_event({"group_id": 123}, platform_id="八千代")

        self.assertTrue(
            plugin._event_matches_verification_platform(
                event,
                123,
                {"verification_platform_id": "八千代"},
            )
        )


if __name__ == "__main__":
    unittest.main()
