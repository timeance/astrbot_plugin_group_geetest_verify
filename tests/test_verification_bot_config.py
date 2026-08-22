import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_new_member_permission_flow import REPO_ROOT

from astrbot_plugin_group_geetest_verify.config.config import (
    normalize_verification_bot_qq,
)
from astrbot_plugin_group_geetest_verify.page_service import PageService


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class VerificationBotConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_accepts_blank_and_trimmed_digits(self):
        self.assertEqual(normalize_verification_bot_qq(None), "")
        self.assertEqual(normalize_verification_bot_qq(" 10002 "), "10002")

    def test_normalization_rejects_sid_bot_id_and_other_non_digits(self):
        for value in ("八千代", "bot-10002", "10002.0"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "只能包含数字"):
                    normalize_verification_bot_qq(value)

    async def test_webui_schema_and_save_round_trip(self):
        config = FakeConfig(group_configs=[])
        cache = SimpleNamespace(list_groups=AsyncMock(return_value=[]))
        service = PageService(config, Path(REPO_ROOT), cache)

        bootstrap = await service.get_bootstrap()
        field = bootstrap["schema"]["default"]["verification_bot_qq"]
        self.assertEqual(field["type"], "string")
        self.assertIn("self_id", field["hint"])

        saved = service.save_group_config(
            "123",
            {"enabled": True, "verification_bot_qq": " 10002 "},
        )

        self.assertEqual(saved["config"]["verification_bot_qq"], "10002")
        self.assertEqual(
            config["group_configs"][0]["verification_bot_qq"], "10002"
        )
        self.assertEqual(config.save_count, 1)

    def test_webui_rejects_invalid_bot_qq_without_saving(self):
        config = FakeConfig(group_configs=[])
        service = PageService(config, Path(REPO_ROOT), SimpleNamespace())

        with self.assertRaisesRegex(ValueError, "只能包含数字"):
            service.save_group_config(
                "123",
                {"verification_bot_qq": "八千代"},
            )

        self.assertEqual(config.save_count, 0)
        self.assertEqual(config["group_configs"], [])


if __name__ == "__main__":
    unittest.main()
