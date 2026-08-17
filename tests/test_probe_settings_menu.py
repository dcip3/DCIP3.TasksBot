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
    _build_notification_keyboard,
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
        self.assertIn("Status: On", _render_probe_settings_text("all"))
        self.assertIn("Scope: All jobs", _render_probe_settings_text("all"))
        self.assertIn("Scope: My jobs only", _render_probe_settings_text("own"))

    def test_text_warns_that_all_jobs_needs_rights(self) -> None:
        """The setting can silently do nothing without the Deadline permission."""
        self.assertIn("rights", _render_probe_settings_text("own"))

    def test_scope_line_is_dropped_when_probing_is_off(self) -> None:
        """Nothing is being probed, so a scope would be noise."""
        text = _render_probe_settings_text("off")
        self.assertIn("Status: Off", text)
        self.assertNotIn("Scope:", text)


class SettingsScreenLayoutTests(unittest.TestCase):
    """House style, learned the hard way: three buttons in a row get truncated.

    Every settings screen in the bot puts at most two buttons on a row, and the
    two only when both labels are short - "All jobs" / "My jobs only".
    """

    def _keyboards(self) -> list:
        return [
            _build_settings_root_keyboard(),
            _build_notification_keyboard(True, "own"),
            *[_build_probe_keyboard(scope) for scope in VALID_PROBE_SCOPES],
        ]

    def test_no_row_holds_more_than_two_buttons(self) -> None:
        for keyboard in self._keyboards():
            for row in keyboard.inline_keyboard:
                self.assertLessEqual(len(row), 2, [b.text for b in row])

    def test_paired_buttons_stay_short_enough_to_read(self) -> None:
        for keyboard in self._keyboards():
            for row in keyboard.inline_keyboard:
                if len(row) < 2:
                    continue
                for button in row:
                    self.assertLessEqual(len(button.text), 16, button.text)


if __name__ == "__main__":
    unittest.main()
