import os
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_DEPS_AVAILABLE = True
PROJECT_DEPS_SKIP_REASON = ""
try:
    from cryptography.fernet import Fernet
    import aiogram  # noqa: F401
    import aiohttp  # noqa: F401
    import aiosqlite  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - local env guard
    PROJECT_DEPS_AVAILABLE = False
    PROJECT_DEPS_SKIP_REASON = f"Project dependencies unavailable: {exc.name}"
    Fernet = None


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCDEFabcdef1234567890")
os.environ.setdefault("DEADLINE_API_URL", "https://deadline.example/api")
os.environ.setdefault(
    "ENCRYPTION_KEY",
    (
        Fernet.generate_key().decode("ascii")
        if Fernet is not None
        else "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    ),
)


def _import_preview_upload_module():
    if "app.core.preview_upload" in sys.modules:
        return sys.modules["app.core.preview_upload"]

    video_helpers_stub = types.ModuleType("app.integrations.video_helpers")
    video_helpers_stub.prepare_video_for_delivery = mock.AsyncMock()
    with mock.patch.dict(
        sys.modules,
        {"app.integrations.video_helpers": video_helpers_stub},
    ):
        return importlib.import_module("app.core.preview_upload")


def _load_jobs_handler_module():
    module_path = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers" / "jobs.py"
    spec = importlib.util.spec_from_file_location("jobs_handler_under_test", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load jobs handler module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(PROJECT_DEPS_AVAILABLE, PROJECT_DEPS_SKIP_REASON)
class AuthReloginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.auth import service as auth_service
        from app.core.config import settings
        from app.storage.database import init_db

        self.auth_service = auth_service
        self.settings = settings
        self.original_db_path = settings.sqlite_db_path
        self.original_key = settings.encryption_key
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        settings.sqlite_db_path = str(Path(self.temp_dir.name) / "app.db")
        settings.encryption_key = Fernet.generate_key().decode("ascii")
        auth_service._cipher = None
        await init_db()

    async def asyncTearDown(self) -> None:
        from app.storage.database import close_db

        await close_db()
        self.settings.sqlite_db_path = self.original_db_path
        self.settings.encryption_key = self.original_key
        self.auth_service._cipher = None

    async def test_relogin_preserves_user_settings(self) -> None:
        from app.storage.database import get_db_connection

        user_id = 4242
        conn = get_db_connection()
        self.assertIsNotNone(conn)
        await conn.execute(
            """
            INSERT INTO user_sessions (
                telegram_user_id,
                deadline_login,
                deadline_password,
                notifications_enabled,
                notification_scope,
                preview_default_worker,
                preview_default_method,
                preview_auto_enabled,
                preview_auto_scope
            )
            VALUES (?, ?, ?, 1, 'own', 'worker-a', 'deadline', 1, 'own')
            """,
            (user_id, "old-login", "old-password"),
        )
        await conn.commit()

        saved = await self.auth_service.save_deadline_credentials(
            user_id,
            "new-login",
            "new-password",
        )

        self.assertTrue(saved)
        credentials = await self.auth_service.get_deadline_credentials(user_id)
        self.assertEqual(credentials, ("new-login", "new-password"))
        async with conn.execute(
            """
            SELECT notifications_enabled,
                   notification_scope,
                   preview_default_worker,
                   preview_default_method,
                   preview_auto_enabled,
                   preview_auto_scope
            FROM user_sessions
            WHERE telegram_user_id = ?
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

        self.assertEqual(row, (1, "own", "worker-a", "deadline", 1, "own"))


@unittest.skipUnless(PROJECT_DEPS_AVAILABLE, PROJECT_DEPS_SKIP_REASON)
class PreviewUploadTokenTests(unittest.TestCase):
    def test_extract_token_from_header(self) -> None:
        preview_upload = _import_preview_upload_module()

        request = SimpleNamespace(headers={"X-Preview-Token": "header-token"}, query={})

        self.assertEqual(preview_upload._extract_token(request), "header-token")

    def test_extract_token_from_bearer_header(self) -> None:
        preview_upload = _import_preview_upload_module()

        request = SimpleNamespace(
            headers={"Authorization": "Bearer bearer-token"},
            query={},
        )

        self.assertEqual(preview_upload._extract_token(request), "bearer-token")

    def test_extract_token_ignores_query_token(self) -> None:
        preview_upload = _import_preview_upload_module()

        request = SimpleNamespace(headers={}, query={"token": "query-token"})

        self.assertIsNone(preview_upload._extract_token(request))

    def test_resolve_upload_temp_path_ignores_partial_files(self) -> None:
        preview_upload = _import_preview_upload_module()

        original_temp_dir = preview_upload.settings.temp_dir
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                preview_upload.settings.temp_dir = temp_dir
                upload_dir = Path(temp_dir) / "upload_token-1"
                upload_dir.mkdir()
                partial_path = upload_dir / "preview.mp4.part"
                partial_path.write_bytes(b"partial")

                payload = preview_upload.PreviewUploadPayload(
                    telegram_user_id=101,
                    job_name="Preview",
                    expected_dropbox_path=None,
                    expected_filename="preview.mp4",
                    expected_local_path=None,
                    preview_job_id="preview-job",
                    source_job_id="source-job",
                )
                state = preview_upload.PreviewUploadState(
                    token="token-1",
                    expires_at=9999999999,
                    created_at=1,
                    claimed_until=0,
                    payload=payload,
                    preview_job_id="preview-job",
                    source_job_id="source-job",
                    status=preview_upload.STATUS_CLAIMED,
                    temp_path=None,
                    bytes_written=0,
                    received_at=0,
                    delivery_attempts=0,
                    next_retry_at=0,
                    last_error=None,
                )

                self.assertIsNone(preview_upload._resolve_upload_temp_path(state))

                final_path = upload_dir / "preview.mp4"
                final_path.write_bytes(b"complete")
                self.assertEqual(preview_upload._resolve_upload_temp_path(state), final_path)
        finally:
            preview_upload.settings.temp_dir = original_temp_dir


class _FakeResponse:
    def __init__(self, *, status: int = 200, payload=None) -> None:
        self.status = status
        self.payload = [] if payload is None else payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self.payload

    async def text(self):
        return ""


class _FakeDeadlineSession:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return _FakeResponse(payload=[])

    def delete(self, url, **kwargs):
        self.calls.append(("delete", url, kwargs))
        return _FakeResponse()


@unittest.skipUnless(PROJECT_DEPS_AVAILABLE, PROJECT_DEPS_SKIP_REASON)
class DeadlineParamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_job_tasks_uses_params(self) -> None:
        from app.core.config import settings
        from app.services import deadline

        session = _FakeDeadlineSession()
        with mock.patch.object(deadline, "get_aiosession", mock.AsyncMock(return_value=session)):
            result = await deadline.get_job_tasks("login", "password", "job?with&chars")

        self.assertEqual(result, [])
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "get")
        self.assertEqual(url, f"{settings.deadline_api_url}/tasks")
        self.assertEqual(kwargs["params"], {"JobID": "job?with&chars"})

    async def test_delete_job_uses_params(self) -> None:
        from app.core.config import settings
        from app.services import deadline

        session = _FakeDeadlineSession()
        with mock.patch.object(deadline, "get_aiosession", mock.AsyncMock(return_value=session)):
            result = await deadline.delete_job("login", "password", "job?with&chars")

        self.assertTrue(result)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "delete")
        self.assertEqual(url, f"{settings.deadline_api_url}/jobs")
        self.assertEqual(kwargs["params"], {"JobID": "job?with&chars"})


class _FakeCallbackMessage:
    def __init__(self) -> None:
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=101)
        self.message = _FakeCallbackMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@unittest.skipUnless(PROJECT_DEPS_AVAILABLE, PROJECT_DEPS_SKIP_REASON)
class DeleteConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_job_first_click_only_asks_for_confirmation(self) -> None:
        jobs = _load_jobs_handler_module()

        callback = _FakeCallback("delete_job:job-123")
        with mock.patch.object(jobs, "delete_job_by_user_id", mock.AsyncMock()) as delete_mock:
            await jobs.delete_job_callback(callback)

        delete_mock.assert_not_awaited()
        self.assertEqual(len(callback.message.answers), 1)
        _, kwargs = callback.message.answers[0]
        keyboard = kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].callback_data,
            "delete_job_confirm:job-123",
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            "delete_job_cancel:job-123",
        )

    async def test_delete_job_confirm_deletes_job(self) -> None:
        jobs = _load_jobs_handler_module()

        callback = _FakeCallback("delete_job_confirm:job-123")
        with mock.patch.object(
            jobs,
            "delete_job_by_user_id",
            mock.AsyncMock(return_value=True),
        ) as delete_mock:
            await jobs.delete_job_confirm_callback(callback)

        delete_mock.assert_awaited_once_with(101, "job-123")
        self.assertEqual(callback.message.edits[0][0], "Job has been deleted.")
