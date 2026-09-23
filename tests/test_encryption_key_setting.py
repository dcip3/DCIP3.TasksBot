"""ENCRYPTION_KEY has to be a usable Fernet key before the bot starts.

Otherwise the bot runs until the first login, and then cannot store the
password. The startup error names the setting and never prints the value,
which is often a mistyped copy of the real key.
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

from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings


class EncryptionKeySettingTests(unittest.TestCase):
    def test_accepts_a_fernet_key(self) -> None:
        key = Fernet.generate_key().decode()
        self.assertEqual(Settings(encryption_key=key).encryption_key, key)

    def test_refuses_anything_else(self) -> None:
        valid = Fernet.generate_key().decode()
        # The .env.example placeholder, nothing at all, and a real key that
        # lost its padding or its second half when it was pasted.
        for key in ("YOUR_FERNET_ENCRYPTION_KEY", "", valid[:-1], valid[:22]):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError) as ctx:
                    Settings(encryption_key=key)
                error = str(ctx.exception)
                self.assertIn("ENCRYPTION_KEY is not a valid Fernet key", error)
                if key:
                    self.assertNotIn(key, error)


if __name__ == "__main__":
    unittest.main()
