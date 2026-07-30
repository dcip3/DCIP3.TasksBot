import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

import aiosqlite

from app.storage import user_settings
from app.bot.handlers.settings import _post_effects_summary


class PostEffectsStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.conn = await aiosqlite.connect(":memory:")
        await self.conn.execute(
            """
            CREATE TABLE user_sessions (
                telegram_user_id INTEGER UNIQUE,
                preview_apply_color_transform INTEGER DEFAULT 1,
                preview_apply_lut INTEGER DEFAULT 1,
                preview_apply_color_controls INTEGER DEFAULT 1
            )
            """
        )
        await self.conn.execute("INSERT INTO user_sessions (telegram_user_id) VALUES (1)")
        await self.conn.commit()
        self.patcher = mock.patch.object(
            user_settings, "get_db_connection", return_value=self.conn
        )
        self.patcher.start()

    async def asyncTearDown(self) -> None:
        self.patcher.stop()
        await self.conn.close()

    async def test_defaults_all_enabled(self) -> None:
        effects = await user_settings.get_preview_post_effects(1)
        self.assertEqual(
            effects, {"color_transform": True, "lut": True, "color_controls": True}
        )

    async def test_unknown_user_defaults_to_enabled(self) -> None:
        effects = await user_settings.get_preview_post_effects(999)
        self.assertTrue(all(effects.values()))

    async def test_toggle_persists_per_effect(self) -> None:
        self.assertTrue(await user_settings.set_preview_post_effect(1, "lut", False))
        effects = await user_settings.get_preview_post_effects(1)
        self.assertFalse(effects["lut"])
        self.assertTrue(effects["color_transform"])
        self.assertTrue(effects["color_controls"])

        self.assertTrue(await user_settings.set_preview_post_effect(1, "lut", True))
        self.assertTrue((await user_settings.get_preview_post_effects(1))["lut"])

    async def test_unknown_effect_rejected(self) -> None:
        self.assertFalse(await user_settings.set_preview_post_effect(1, "bogus", False))

    async def test_null_column_reads_as_enabled(self) -> None:
        await self.conn.execute(
            "UPDATE user_sessions SET preview_apply_lut = NULL WHERE telegram_user_id = 1"
        )
        await self.conn.commit()
        self.assertTrue((await user_settings.get_preview_post_effects(1))["lut"])


class PostEffectsSummaryTests(unittest.TestCase):
    def test_summary_variants(self) -> None:
        all_on = {"color_transform": True, "lut": True, "color_controls": True}
        self.assertEqual(_post_effects_summary(all_on), "All on")

        all_off = {key: False for key in all_on}
        self.assertEqual(_post_effects_summary(all_off), "All off (raw render)")

        partial = {"color_transform": True, "lut": False, "color_controls": True}
        self.assertEqual(_post_effects_summary(partial), "Off: Camera LUT")

        two_off = {"color_transform": True, "lut": False, "color_controls": False}
        self.assertEqual(_post_effects_summary(two_off), "Off: Camera LUT, Color Controls")


if __name__ == "__main__":
    unittest.main()
