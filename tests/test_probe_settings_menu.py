"""The ETA Probing settings screen."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://example.local/api")
os.environ.setdefault(
    "ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.bot.handlers.settings import (
    _build_probe_keyboard,
    _build_settings_root_keyboard,
    _render_probe_settings_text,
)
from app.storage.user_settings import VALID_PROBE_SCOPES


def _buttons(keyboard) -> list:
    return [button for row in keyboard.inline_keyboard for button in row]


class ProbeSettingsMenuTests(unittest.TestCase):
    def test_root_menu_has_an_entry(self) -> None:
        callbacks = [b.callback_data for b in _buttons(_build_settings_root_keyboard())]
        self.assertIn("settings:probe", callbacks)

    def test_every_scope_is_offered(self) -> None:
        buttons = _buttons(_build_probe_keyboard("own"))
        callbacks = {b.callback_data for b in buttons}
        for scope in VALID_PROBE_SCOPES:
            self.assertIn(f"settings:probe:scope:{scope}", callbacks)

    def test_exactly_one_option_is_marked(self) -> None:
        for scope in VALID_PROBE_SCOPES:
            with self.subTest(scope=scope):
                buttons = [
                    b
                    for b in _buttons(_build_probe_keyboard(scope))
                    if (b.callback_data or "").startswith("settings:probe:scope:")
                ]
                selected = [b for b in buttons if b.text.startswith("✅")]
                self.assertEqual(len(selected), 1)
                self.assertEqual(
                    selected[0].callback_data, f"settings:probe:scope:{scope}"
                )

    def test_text_names_the_current_scope(self) -> None:
        self.assertIn("Scope: All jobs", _render_probe_settings_text("all"))
        self.assertIn("Scope: My jobs only", _render_probe_settings_text("own"))
        self.assertIn("Scope: Off", _render_probe_settings_text("off"))

    def test_text_warns_that_all_jobs_needs_rights(self) -> None:
        """The setting can silently do nothing without the Deadline permission."""
        self.assertIn("rights", _render_probe_settings_text("own"))


if __name__ == "__main__":
    unittest.main()
