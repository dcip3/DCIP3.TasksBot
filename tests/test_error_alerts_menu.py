"""The Error Alerts screen (formerly "Notifications").

The old name promised something the feature never did - job progress or "your
render finished". All it sends is an alert when Deadline files an error report,
so the screen says which errors those are.
"""

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
    _build_settings_root_keyboard,
    _render_notification_settings_text,
)


def _buttons(keyboard) -> list:
    return [button for row in keyboard.inline_keyboard for button in row]


class ErrorAlertsMenuTests(unittest.TestCase):
    def test_root_button_says_what_it_does(self) -> None:
        entry = [
            b
            for b in _buttons(_build_settings_root_keyboard())
            if b.callback_data == "settings:notifications"
        ]
        self.assertEqual(len(entry), 1)
        self.assertIn("Error Alerts", entry[0].text)

    def test_screen_is_titled_error_alerts(self) -> None:
        self.assertTrue(
            _render_notification_settings_text(True, "own").startswith("🚨 Error Alerts")
        )

    def test_screen_lists_what_actually_triggers_an_alert(self) -> None:
        text = _render_notification_settings_text(True, "all")
        self.assertIn("Redshift activation", text)
        self.assertIn("local C: path", text)

    def test_toggle_is_about_errors(self) -> None:
        toggle = [
            b
            for b in _buttons(_build_notification_keyboard(True, "own"))
            if b.callback_data == "settings:notif:toggle"
        ]
        self.assertEqual(len(toggle), 1)
        self.assertIn("errors", toggle[0].text)

    def test_off_state_is_still_called_out(self) -> None:
        text = _render_notification_settings_text(False, "own")
        self.assertIn("Status: Off", text)
        self.assertIn("turned off", text)

    def test_scope_is_still_shown(self) -> None:
        self.assertIn("Scope: All jobs", _render_notification_settings_text(True, "all"))
        self.assertIn(
            "Scope: My jobs only", _render_notification_settings_text(True, "own")
        )


if __name__ == "__main__":
    unittest.main()
