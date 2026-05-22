import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DEPS_AVAILABLE = True
PROJECT_DEPS_SKIP_REASON = ""
try:
    import aiogram  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - local env guard
    PROJECT_DEPS_AVAILABLE = False
    PROJECT_DEPS_SKIP_REASON = f"Project dependencies unavailable: {exc.name}"


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://deadline.example/api")
os.environ.setdefault("DROPBOX_APP_KEY", "app-key")
os.environ.setdefault("DROPBOX_APP_SECRET", "app-secret")
os.environ.setdefault("DROPBOX_REFRESH_TOKEN", "refresh-token")
os.environ.setdefault("DROPBOX_TEAM_MEMBER_ID", "team-member")
os.environ.setdefault("DROPBOX_ROOT_NAMESPACE_ID", "root-namespace")


def _import_delivery_module():
    video_helpers_stub = types.ModuleType("app.integrations.video_helpers")
    video_helpers_stub.VideoDeliveryPreparation = object
    with unittest.mock.patch.dict(
        sys.modules,
        {"app.integrations.video_helpers": video_helpers_stub},
    ):
        return importlib.import_module("app.services.preview.delivery")


class _FakeBot:
    def __init__(self, *, send_video_error=None) -> None:
        self.events = []
        self.send_video_error = send_video_error
        self.next_message_id = 1000

    async def edit_message_text(self, text, **kwargs):
        self.events.append(("edit", kwargs["chat_id"], kwargs["message_id"], text))
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def send_message(self, chat_id, text, **kwargs):
        self.next_message_id += 1
        self.events.append(("message", chat_id, self.next_message_id, text))
        return SimpleNamespace(message_id=self.next_message_id)

    async def send_video(self, chat_id, video, **kwargs):
        self.events.append(("video", chat_id, kwargs.get("caption")))
        if self.send_video_error is not None:
            raise self.send_video_error
        return SimpleNamespace(message_id=2000)

    async def delete_message(self, chat_id, message_id):
        self.events.append(("delete", chat_id, message_id))
        return True


@unittest.skipUnless(PROJECT_DEPS_AVAILABLE, PROJECT_DEPS_SKIP_REASON)
class PreviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.delivery = _import_delivery_module()
        self.delivery.preview_state.message_registry.clear()
        self.delivery.notified_jobs.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.video_path = Path(self.temp_dir.name) / "preview.mp4"
        self.video_path.write_bytes(b"video")

    async def asyncTearDown(self) -> None:
        self.delivery.preview_state.message_registry.clear()
        self.delivery.notified_jobs.clear()

    async def test_ready_status_is_deleted_after_successful_video_send(self) -> None:
        fake_bot = _FakeBot()
        self.delivery.bot = fake_bot
        self.delivery.preview_state.message_registry["preview-job"] = (101, 555)

        await self.delivery.send_ready_preview_video(
            target_user_id=101,
            preview_job_id="preview-job",
            job_name="Example",
            preparation=SimpleNamespace(
                video_path=self.video_path,
                size_mb=1.0,
                fallback_message=None,
            ),
            caption="caption",
            delete_job=unittest.mock.AsyncMock(return_value=True),
        )

        self.assertEqual(
            fake_bot.events,
            [
                ("edit", 101, 555, "🎬 Preview for Example is ready."),
                ("video", 101, "caption"),
                ("delete", 101, 555),
            ],
        )
        self.assertNotIn("preview-job", self.delivery.preview_state.message_registry)

    async def test_ready_status_stays_when_video_send_fails(self) -> None:
        fake_bot = _FakeBot(send_video_error=RuntimeError("telegram failed"))
        self.delivery.bot = fake_bot
        self.delivery.preview_state.message_registry["preview-job"] = (101, 555)

        with self.assertRaises(RuntimeError):
            await self.delivery.send_ready_preview_video(
                target_user_id=101,
                preview_job_id="preview-job",
                job_name="Example",
                preparation=SimpleNamespace(
                    video_path=self.video_path,
                    size_mb=1.0,
                    fallback_message=None,
                ),
                caption="caption",
                delete_job=unittest.mock.AsyncMock(return_value=True),
            )

        self.assertNotIn(("delete", 101, 555), fake_bot.events)
        self.assertIn("preview-job", self.delivery.preview_state.message_registry)

    async def test_transient_ready_message_is_deleted_after_fallback_send(self) -> None:
        fake_bot = _FakeBot()
        self.delivery.bot = fake_bot

        await self.delivery.send_ready_preview_video(
            target_user_id=101,
            preview_job_id=None,
            job_name="Example",
            preparation=SimpleNamespace(
                video_path=self.video_path,
                size_mb=99.0,
                fallback_message="Too large.",
            ),
            caption="caption",
            delete_job=unittest.mock.AsyncMock(return_value=True),
        )

        self.assertEqual(
            fake_bot.events,
            [
                ("message", 101, 1001, "🎬 Preview for Example is ready."),
                ("message", 101, 1002, "Too large."),
                ("delete", 101, 1001),
            ],
        )
