import logging
from pathlib import Path
from typing import cast

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from app.auth import is_authorized
from app.storage.user_settings import (
    NotificationScope,
    PREVIEW_DEFAULT_WORKER_AUTO,
    PREVIEW_POST_EFFECTS,
    get_notification_settings,
    get_preview_default_worker,
    get_preview_auto_enabled,
    get_preview_auto_scope,
    get_preview_post_effects,
    set_notification_enabled,
    set_notification_scope,
    set_preview_auto_enabled,
    set_preview_auto_scope,
    set_preview_default_worker,
    set_preview_post_effect,
)
from app.core.ui_helpers import (
    authorized_only,
    back_inline_button,
    close_inline_button,
    inline_button,
    selectable_inline_button,
)
from app.services.deadline import get_workers_list

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


_POST_EFFECT_LABELS = {
    "color_transform": "Color transform",
    "lut": "Camera LUT",
    "color_controls": "Color Controls",
}


def _post_effects_summary(effects: dict) -> str:
    """Describe the enabled preview post effects in one line."""
    disabled = [
        _POST_EFFECT_LABELS[key] for key in PREVIEW_POST_EFFECTS if not effects.get(key, True)
    ]
    if not disabled:
        return "All on"
    if len(disabled) == len(PREVIEW_POST_EFFECTS):
        return "All off (raw render)"
    return "Off: " + ", ".join(disabled)


def _build_settings_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="🔔 Notifications",
                    callback_data="settings:notifications",
                )
            ],
            [
                inline_button(
                    text="🎬 Preview",
                    callback_data="settings:preview",
                )
            ],
            [
                inline_button(
                    text="🛠️ Worker Setup",
                    callback_data="settings:setup_script",
                )
            ],
            [
                close_inline_button(callback_data="settings:close"),
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

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                selectable_inline_button(
                    text="Receive notifications",
                    callback_data="settings:notif:toggle",
                    selected=enabled,
                    prefix_unselected="◻ ",
                )
            ],
            [
                selectable_inline_button(
                    text="All jobs",
                    callback_data="settings:notif:scope:all",
                    selected=scope_normalized == "all",
                    prefix_unselected="◻ ",
                ),
                selectable_inline_button(
                    text="My jobs only",
                    callback_data="settings:notif:scope:own",
                    selected=scope_normalized == "own",
                    prefix_unselected="◻ ",
                ),
            ],
            [
                back_inline_button(callback_data="settings:back:root"),
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

        text_lines = [
            "🖥️ Default Worker",
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
                "• Applies to: Deadline previews",
                "",
                "Choose where Deadline previews should run:",
                "• ✅ Auto — inherit the source job machine list (whitelist/blacklist)",
                "• 🖥️ Specific worker — always use that machine",
                "• ❓ Always ask — always show the worker picker",
            ]
        )

        keyboard_rows = []

        keyboard_rows.append(
            [
                selectable_inline_button(
                    text="Auto",
                    callback_data="settings:preview:worker:set:auto",
                    selected=default_worker == PREVIEW_DEFAULT_WORKER_AUTO,
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
                    row.append(
                        selectable_inline_button(
                            text=worker_name,
                            callback_data=f"settings:preview:worker:set:{worker_name}",
                            selected=worker_name == default_worker,
                        )
                    )
                keyboard_rows.append(row)

        keyboard_rows.append(
            [
                selectable_inline_button(
                    text="❓ Always ask",
                    callback_data="settings:preview:worker:set:none",
                    selected=default_worker is None,
                )
            ]
        )
        keyboard_rows.append(
            [
                back_inline_button(callback_data="settings:preview"),
            ]
        )

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
        effects = await get_preview_post_effects(user_id)
        effects_label = _post_effects_summary(effects)
        text_lines = [
            "🎬 Preview Settings",
            "",
            f"• Auto preview: {auto_label}",
            f"• Auto preview scope: {scope_label}",
            f"• Post effects: {effects_label}",
            "",
            "Choose what to configure for previews.",
        ]
        keyboard_rows = [
            [
                inline_button(
                    text="⚡ Auto Preview",
                    callback_data="settings:preview:auto",
                ),
            ],
            [
                inline_button(
                    text="🎨 Post Effects",
                    callback_data="settings:preview:effects",
                ),
            ],
            [
                inline_button(
                    text="🖥️ Default Worker",
                    callback_data="settings:preview:worker",
                ),
            ],
            [
                back_inline_button(callback_data="settings:back:root"),
            ],
        ]
        await _edit_or_send(
            callback_query.message,
            "\n".join(text_lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    async def show_preview_effects() -> None:
        effects = await get_preview_post_effects(user_id)

        text_lines = [
            "🎨 Preview Post Effects",
            "",
            f"• Status: {_post_effects_summary(effects)}",
            "",
            "What the preview reproduces from the render camera:",
            "• 🌈 Color transform — ACES view transform (ACEScg → sRGB).",
            "   Off gives the raw linear image: very dark, for inspection only.",
            "• 🎨 Camera LUT — the LUT assigned on the Redshift camera.",
            "• 🎛 Color Controls — camera contrast and RGB curves.",
            "",
            "Turn things off to see a rawer render. Effects Redshift already "
            "bakes into the frames (exposure, bloom/glare) are always there.",
        ]

        keyboard_rows = [
            [
                selectable_inline_button(
                    text="🌈 Color transform (ACES)",
                    callback_data="settings:preview:effects:toggle:color_transform",
                    selected=effects["color_transform"],
                    prefix_unselected="◻ ",
                )
            ],
            [
                selectable_inline_button(
                    text="🎨 Camera LUT",
                    callback_data="settings:preview:effects:toggle:lut",
                    selected=effects["lut"],
                    prefix_unselected="◻ ",
                )
            ],
            [
                selectable_inline_button(
                    text="🎛 Color Controls",
                    callback_data="settings:preview:effects:toggle:color_controls",
                    selected=effects["color_controls"],
                    prefix_unselected="◻ ",
                )
            ],
            [
                back_inline_button(callback_data="settings:preview"),
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

        keyboard_rows = [
            [
                selectable_inline_button(
                    text="Auto preview enabled",
                    callback_data="settings:preview:auto:toggle",
                    selected=auto_enabled,
                    prefix_unselected="◻ ",
                )
            ],
            [
                selectable_inline_button(
                    text="All jobs",
                    callback_data="settings:preview:auto:scope:all",
                    selected=auto_scope == "all",
                    prefix_unselected="◻ ",
                ),
                selectable_inline_button(
                    text="My jobs only",
                    callback_data="settings:preview:auto:scope:own",
                    selected=auto_scope == "own",
                    prefix_unselected="◻ ",
                ),
            ],
            [
                back_inline_button(callback_data="settings:preview"),
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
                if sub_target == "effects":
                    await show_preview_effects()
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
                        await callback_query.answer("Default worker set to: Auto (job machine list)")
                    else:
                        await set_preview_default_worker(user_id, worker_name)
                        await callback_query.answer(f"Default worker set to: {worker_name}")
                    await show_preview_worker()
                    return
            if sub_action == "method":
                # Legacy menu removed: previews are always rendered via Deadline.
                await show_preview_menu()
                await callback_query.answer()
                return
            if sub_action == "effects":
                if len(parts) == 3:
                    await show_preview_effects()
                    await callback_query.answer()
                    return
                if len(parts) > 4 and parts[3] == "toggle":
                    effect_key = parts[4]
                    if effect_key not in PREVIEW_POST_EFFECTS:
                        await callback_query.answer("Unsupported option.", show_alert=True)
                        return
                    effects = await get_preview_post_effects(user_id)
                    new_value = not effects.get(effect_key, True)
                    await set_preview_post_effect(user_id, effect_key, new_value)
                    await show_preview_effects()
                    label = _POST_EFFECT_LABELS[effect_key]
                    await callback_query.answer(
                        f"{label} {'enabled' if new_value else 'disabled'}"
                    )
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
