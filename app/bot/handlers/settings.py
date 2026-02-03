import logging
from pathlib import Path
from typing import cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import is_authorized
from app.user_settings import (
    NotificationScope,
    PREVIEW_DEFAULT_WORKER_AUTO,
    get_notification_settings,
    get_preview_default_method,
    get_preview_default_worker,
    get_preview_auto_enabled,
    get_preview_auto_scope,
    set_notification_enabled,
    set_notification_scope,
    set_preview_auto_enabled,
    set_preview_auto_scope,
    set_preview_default_method,
    set_preview_default_worker,
)
from app.core.utils import authorized_only
from app.services import get_workers_list

logger = logging.getLogger(__name__)

router = Router()


async def _edit_or_send(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if "message is not modified" in error_text:
            return
        if (
            "message can't be edited" in error_text
            or "message to edit not found" in error_text
            or "message is too old" in error_text
        ):
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        raise


def _render_settings_root_text() -> str:
    return "Settings\nSelect a section to configure."


def _build_settings_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔔 Notifications",
                    callback_data="settings:notifications",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎬 Preview",
                    callback_data="settings:preview",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛠️ Worker Setup",
                    callback_data="settings:setup_script",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✖️ Close",
                    callback_data="settings:close",
                ),
            ],
        ]
    )


def _render_notification_settings_text(enabled: bool, scope: str) -> str:
    status_text = "On" if enabled else "Off"
    scope_lower = scope.lower()
    scope_text = "My jobs only" if scope_lower == "own" else "All jobs"

    details = [
        "🔔 Notifications",
        "",
        f"• Status: {status_text}",
        f"• Scope: {scope_text}",
        "",
        "Choose when you want to receive job alerts.",
    ]
    if not enabled:
        details.append("• Alerts are currently turned off")
    return "\n".join(details)


def _build_notification_keyboard(enabled: bool, scope: str) -> InlineKeyboardMarkup:
    scope_normalized = scope.lower()
    enable_label = ("✅ " if enabled else "◻ ") + "Receive notifications"
    all_jobs_label = ("✅ " if scope_normalized == "all" else "◻ ") + "All jobs"
    own_jobs_label = ("✅ " if scope_normalized == "own" else "◻ ") + "My jobs only"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=enable_label,
                    callback_data="settings:notif:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=all_jobs_label,
                    callback_data="settings:notif:scope:all",
                ),
                InlineKeyboardButton(
                    text=own_jobs_label,
                    callback_data="settings:notif:scope:own",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:back:root",
                ),
            ],
        ]
    )


@router.message(F.text == "⚙️ Settings")
@authorized_only
async def handle_settings_menu(message: Message) -> None:
    """Display the settings menu with inline navigation."""
    if message.from_user is None:
        await message.answer("Error: User information not available.")
        return

    await message.answer(
        _render_settings_root_text(),
        reply_markup=_build_settings_root_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("settings:"))
async def settings_callback_handler(callback_query: CallbackQuery) -> None:
    """Handle inline settings navigation and updates."""
    if callback_query.data is None:
        await callback_query.answer("Invalid callback data.", show_alert=True)
        return

    if callback_query.from_user is None:
        await callback_query.answer("Error: User information not available.", show_alert=True)
        return

    if callback_query.message is None:
        await callback_query.answer("Error: Message not available.", show_alert=True)
        return

    if not await is_authorized(callback_query.from_user.id):
        await callback_query.answer("Please login first.", show_alert=True)
        return

    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    async def show_root() -> None:
        await _edit_or_send(
            callback_query.message,
            _render_settings_root_text(),
            reply_markup=_build_settings_root_keyboard(),
        )

    async def show_notifications() -> None:
        enabled, scope = await get_notification_settings(user_id)
        await _edit_or_send(
            callback_query.message,
            _render_notification_settings_text(enabled, scope),
            reply_markup=_build_notification_keyboard(enabled, scope),
        )

    async def show_preview_worker() -> None:
        default_worker = await get_preview_default_worker(user_id)
        workers = await get_workers_list(user_id)

        text_lines = ["Preview Worker Settings", ""]
        text_lines = [
            "🖥️ Preview Worker",
            "",
        ]
        if default_worker == PREVIEW_DEFAULT_WORKER_AUTO:
            default_label = "Auto"
        elif default_worker:
            default_label = default_worker
        else:
            default_label = "Always ask"
        text_lines.extend(
            [
                f"• Default: {default_label}",
                "• Applies to: Deadline previews only",
                "• Server method ignores this setting",
                "",
                "Pick where previews should be rendered:",
                "• ✅ Auto — prefer the job creator or workers that rendered the job",
                "• 🖥️ Specific worker — always use that machine",
                "• ❓ Always ask — always show the worker picker",
            ]
        )

        keyboard_rows = []

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Auto"
                    if default_worker == PREVIEW_DEFAULT_WORKER_AUTO
                    else "Auto",
                    callback_data="settings:preview:worker:set:auto",
                )
            ]
        )

        if workers:
            for i in range(0, len(workers), 2):
                row = []
                for j in range(i, min(i + 2, len(workers))):
                    worker = workers[j]
                    info = worker.get("Info", {})
                    worker_name = info.get("Name", "Unknown")
                    display_name = (
                        f"✅ {worker_name}" if worker_name == default_worker else worker_name
                    )
                    row.append(
                        InlineKeyboardButton(
                            text=display_name,
                            callback_data=f"settings:preview:worker:set:{worker_name}",
                        )
                    )
                keyboard_rows.append(row)

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="✅ ❓ Always ask"
                    if default_worker is None
                    else "❓ Always ask",
                    callback_data="settings:preview:worker:set:none",
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:preview",
                ),
            ]
        )

        await _edit_or_send(
            callback_query.message,
            "\n".join(text_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def show_preview_method() -> None:
        default_method = await get_preview_default_method(user_id)
        method_display = {
            "server": "Server",
            "deadline": "Deadline",
            None: "Always ask",
        }

        text_lines = [
            "🎛 Preview Method",
            "",
            f"• Default: {method_display.get(default_method, 'Always ask')}",
            "• Applies to: how previews are rendered",
            "",
            "Choose the default render method:",
            "• 🖥️ Server — build preview on the bot host",
            "• ☁️ Deadline — build preview on the farm",
            "• ❓ Always ask — show the menu every time",
        ]

        def _button(label: str, method_value: str, selected: bool) -> InlineKeyboardButton:
            prefix = "✅ " if selected else ""
            return InlineKeyboardButton(
                text=f"{prefix}{label}",
                callback_data=f"settings:preview:method:set:{method_value}",
            )

        keyboard_rows = [
            [
                _button("🖥️ Server", "server", default_method == "server"),
                _button("☁️ Deadline", "deadline", default_method == "deadline"),
            ],
            [
                _button(
                    "❓ Always ask",
                    "none",
                    default_method is None,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:preview",
                ),
            ],
        ]

        await _edit_or_send(
            callback_query.message,
            "\n".join(text_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def show_preview_menu() -> None:
        auto_enabled = await get_preview_auto_enabled(user_id)
        auto_scope = await get_preview_auto_scope(user_id)
        scope_label = "My jobs only" if auto_scope == "own" else "All jobs"
        auto_label = "On" if auto_enabled else "Off"
        text_lines = [
            "🎬 Preview Settings",
            "",
            f"• Auto preview: {auto_label}",
            f"• Auto preview scope: {scope_label}",
            "• Uses: your default method",
            "",
            "Choose what to configure for previews.",
        ]
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text="⚡ Auto Preview",
                    callback_data="settings:preview:auto",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Default Method",
                    callback_data="settings:preview:method",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🖥️ Default Worker",
                    callback_data="settings:preview:worker",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:back:root",
                ),
            ],
        ]
        await _edit_or_send(
            callback_query.message,
            "\n".join(text_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def show_preview_auto() -> None:
        auto_enabled = await get_preview_auto_enabled(user_id)
        auto_scope = await get_preview_auto_scope(user_id)
        scope_label = "My jobs only" if auto_scope == "own" else "All jobs"
        status_label = "On" if auto_enabled else "Off"

        text_lines = [
            "⚡ Auto Preview",
            "",
            f"• Status: {status_label}",
            f"• Scope: {scope_label}",
            "",
            "Auto preview creates previews automatically when jobs finish.",
        ]

        toggle_label = "✅ Auto preview enabled" if auto_enabled else "☐ Auto preview enabled"
        all_jobs_label = ("✅ " if auto_scope == "all" else "◻ ") + "All jobs"
        own_jobs_label = ("✅ " if auto_scope == "own" else "◻ ") + "My jobs only"

        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text=toggle_label,
                    callback_data="settings:preview:auto:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=all_jobs_label,
                    callback_data="settings:preview:auto:scope:all",
                ),
                InlineKeyboardButton(
                    text=own_jobs_label,
                    callback_data="settings:preview:auto:scope:own",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="settings:preview",
                ),
            ],
        ]

        await _edit_or_send(
            callback_query.message,
            "\n".join(text_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    if action == "close":
        await _edit_or_send(callback_query.message, "Settings closed.")
        await callback_query.answer()
        return

    if action == "update":
        target = parts[2] if len(parts) > 2 else "root"
        if target == "notifications":
            await show_notifications()
            await callback_query.answer("Updated")
            return
        if target == "preview":
            if len(parts) > 3:
                sub_target = parts[3]
                if sub_target == "worker":
                    await show_preview_worker()
                    await callback_query.answer("Updated")
                    return
                if sub_target == "method":
                    await show_preview_method()
                    await callback_query.answer("Updated")
                    return
            await show_preview_menu()
            await callback_query.answer("Updated")
            return
        if target == "root":
            await show_root()
            await callback_query.answer("Updated")
            return
        await callback_query.answer()
        return

    if action == "notifications":
        await show_notifications()
        await callback_query.answer()
        return

    if action == "preview":
        if len(parts) == 2:
            await show_preview_menu()
            await callback_query.answer()
            return
        if len(parts) > 2:
            sub_action = parts[2]
            if sub_action == "worker":
                if len(parts) == 3:
                    await show_preview_worker()
                    await callback_query.answer()
                    return
                if len(parts) > 3 and parts[3] == "set" and len(parts) > 4:
                    worker_name = parts[4]
                    if worker_name == "none":
                        await set_preview_default_worker(user_id, None)
                        await callback_query.answer(
                            "Default worker cleared. Menu will be shown for each preview."
                        )
                    elif worker_name == "auto":
                        await set_preview_default_worker(user_id, PREVIEW_DEFAULT_WORKER_AUTO)
                        await callback_query.answer("Default worker set to: Auto (render worker)")
                    else:
                        await set_preview_default_worker(user_id, worker_name)
                        await callback_query.answer(f"Default worker set to: {worker_name}")
                    await show_preview_worker()
                    return
            if sub_action == "method":
                if len(parts) == 3:
                    await show_preview_method()
                    await callback_query.answer()
                    return
                if len(parts) > 3 and parts[3] == "set" and len(parts) > 4:
                    method_value = parts[4]
                    if method_value == "none":
                        await set_preview_default_method(user_id, None)
                        await callback_query.answer("Preview method set to: Always ask")
                    elif method_value in {"server", "deadline"}:
                        await set_preview_default_method(user_id, method_value)
                        await callback_query.answer(
                            "Preview method set to: Server"
                            if method_value == "server"
                            else "Preview method set to: Deadline"
                        )
                    else:
                        await callback_query.answer("Unsupported option.", show_alert=True)
                        return
                    await show_preview_method()
                    return
            if sub_action == "auto":
                if len(parts) == 3:
                    await show_preview_auto()
                    await callback_query.answer()
                    return
                if len(parts) > 3 and parts[3] == "toggle":
                    current_enabled = await get_preview_auto_enabled(user_id)
                    await set_preview_auto_enabled(user_id, not current_enabled)
                    await show_preview_auto()
                    await callback_query.answer(
                        "Auto preview enabled" if not current_enabled else "Auto preview disabled"
                    )
                    return
                if len(parts) > 4 and parts[3] == "scope":
                    scope_value = parts[4]
                    if scope_value not in {"all", "own"}:
                        await callback_query.answer("Unsupported option.", show_alert=True)
                        return
                    await set_preview_auto_scope(user_id, cast(NotificationScope, scope_value))
                    await show_preview_auto()
                    await callback_query.answer(
                        "Scope set to all jobs" if scope_value == "all" else "Scope set to my jobs only"
                    )
                    return

    if action == "setup_script":
        script_path = Path(__file__).resolve().parents[3] / "scripts" / "worker_setup.ps1"
        if not script_path.exists():
            await callback_query.answer("Setup script not found.", show_alert=True)
            return
        try:
            async def send_document(path: Path, caption: str, parse_mode: str | None = None) -> None:
                document = FSInputFile(str(path))
                if callback_query.message:
                    await callback_query.message.answer_document(
                        document=document,
                        caption=caption,
                        parse_mode=parse_mode,
                    )
                elif callback_query.from_user:
                    from app.core.bot_core import bot

                    await bot.send_document(
                        chat_id=callback_query.from_user.id,
                        document=document,
                        caption=caption,
                        parse_mode=parse_mode,
                    )
                else:
                    raise RuntimeError("Unable to resolve target for sending document")

            caption_lines = [
                "*Deadline worker setup helper*",
                "",
                "Run this script on a Deadline worker to install the required dependencies.",
                "",
                "If PowerShell blocks running the script, execute from the same folder:",
                "```powershell",
                "powershell -ExecutionPolicy Bypass -File .\\worker_setup.ps1",
                "```",
                "You will also receive `worker_setup.bat`, which applies the bypass automatically.",
                "",
                "Re-running the script is safe; existing installations will be reused.",
            ]
            caption = "\n".join(caption_lines)

            await send_document(script_path, caption, parse_mode="Markdown")

            batch_path = script_path.with_suffix(".bat")
            if batch_path.exists():
                await send_document(
                    batch_path,
                    "Windows launcher (`worker_setup.bat`) that bypasses execution policy for `worker_setup.ps1`.",
                    parse_mode="Markdown",
                )

            await callback_query.answer("Setup script sent!")
        except Exception as exc:
            logger.error("Failed to send Deadline setup script: %s", exc)
            await callback_query.answer("Failed to send setup script.", show_alert=True)
        return

    if action == "notif" and len(parts) > 2:
        sub_action = parts[2]
        if sub_action == "toggle":
            current_enabled, _ = await get_notification_settings(user_id)
            enabled, scope = await set_notification_enabled(user_id, not current_enabled)
            await show_notifications()
            await callback_query.answer(
                "Notifications enabled" if enabled else "Notifications disabled"
            )
            return

        if sub_action == "scope" and len(parts) > 3:
            scope_value = parts[3]
            if scope_value not in {"all", "own"}:
                await callback_query.answer("Unsupported option.", show_alert=True)
                return

            enabled, scope = await set_notification_scope(
                user_id,
                cast(NotificationScope, scope_value),
            )
            await show_notifications()
            if scope == "own":
                await callback_query.answer("Scope set to my jobs only")
            else:
                await callback_query.answer("Scope set to all jobs")
            return

    if action == "back":
        target = parts[2] if len(parts) > 2 else "root"
        if target == "root":
            await show_root()
            await callback_query.answer()
            return

    await callback_query.answer()
