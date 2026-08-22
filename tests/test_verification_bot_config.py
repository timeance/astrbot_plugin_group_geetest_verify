import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_new_member_permission_flow import REPO_ROOT

from astrbot_plugin_group_geetest_verify.config.config import (
    normalize_verification_platform_id,
)
from astrbot_plugin_group_geetest_verify.page_service import PageService


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class VerificationBotConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_accepts_blank_and_trimmed_astrbot_id(self):
        self.assertEqual(normalize_verification_platform_id(None), "")
        self.assertEqual(
            normalize_verification_platform_id(" 八千代 "), "八千代"
        )

    def test_normalization_rejects_non_string_values(self):
        for value in (10002, {"id": "八千代"}, ["八千代"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "必须是字符串"):
                    normalize_verification_platform_id(value)

    async def test_webui_schema_and_save_round_trip(self):
        config = FakeConfig(group_configs=[])
        cache = SimpleNamespace(list_groups=AsyncMock(return_value=[]))
        service = PageService(config, Path(REPO_ROOT), cache)

        bootstrap = await service.get_bootstrap()
        field = bootstrap["schema"]["default"]["verification_platform_id"]
        self.assertEqual(field["type"], "string")
        self.assertIn("Bot ID", field["hint"])

        saved = service.save_group_config(
            "123",
            {"enabled": True, "verification_platform_id": " 八千代 "},
        )

        self.assertEqual(
            saved["config"]["verification_platform_id"], "八千代"
        )
        self.assertEqual(
            config["group_configs"][0]["verification_platform_id"], "八千代"
        )
        self.assertEqual(config.save_count, 1)

    def test_webui_rejects_non_string_bot_id_without_saving(self):
        config = FakeConfig(group_configs=[])
        service = PageService(config, Path(REPO_ROOT), SimpleNamespace())

        with self.assertRaisesRegex(ValueError, "必须是字符串"):
            service.save_group_config(
                "123",
                {"verification_platform_id": 10002},
            )

        self.assertEqual(config.save_count, 0)
        self.assertEqual(config["group_configs"], [])


if __name__ == "__main__":
    unittest.main()
