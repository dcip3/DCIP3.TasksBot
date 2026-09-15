"""Background watcher for preview completion and auto-preview workflows."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Optional

from app.auth import _decrypt_password
from app.core.bot_core import bot
from app.core.config import settings
from app.core.ttl_cache import TTLCache
from app.storage import probe_state
from app.storage.database import get_db_connection
from app.services.job_state import auto_preview_jobs, notified_jobs
from app.services.preview.runtime import (
    _notify_preview_job_completion,
    _notify_preview_job_failure,
    _run_auto_preview_for_job,
    pop_preview_message,
    preview_message_registry,
    preview_missing_strikes,
    preview_tracked_jobs,
)
from app.storage.user_settings import (
    DEFAULT_PROBE_SCOPE,
    _normalize_probe_scope,
    _normalize_scope,
)

logger = logging.getLogger(__name__)

_UNABLE_TO_OPEN_FILE_RE = re.compile(
    r"unable to open file:\s*([a-z]:[^\r\n]+)",
    re.IGNORECASE,
)
_INPUT_FILE_RE = re.compile(
    r"input file:\s*([a-z]:[^\r\n]+)",
    re.IGNORECASE,
)
# Houdini reports a scene it cannot load either way, depending on where it gave
# up: "Unable to open file: Y:/...hip" from hou.hipFile.load, "Error loading:
# Y:/...hip" from the plugin around it. Same failure, same file, same advice.
_ERROR_LOADING_RE = re.compile(
    r"error loading:\s*([a-z]:[^\r\n]+)",
    re.IGNORECASE,
)

_AUTO_PREVIEW_SCAN_INTERVAL_SECONDS = 30
# How long a preview may sit on unusable machines before it is moved. Long
# enough that a Worker restarting is not mistaken for one that is gone.
_STRANDED_PREVIEW_GRACE_SECONDS = 180
# How many task errors a preview may collect before it is treated as a job that
# will keep failing rather than one having a bad run. Deadline requeues a failed
# task, so without this a preview whose upload the bot refuses runs, fails and
# runs again for as long as the render sits on the farm.
_PREVIEW_ERROR_LIMIT = 3
# How long a suspended render may hold its pending preview before the preview is
# dropped. Short pauses (fixing something and resuming) keep their preview; a
# render left paused releases it, and resuming re-queues a fresh one.
_SUSPENDED_SOURCE_GRACE_SECONDS = 60 * 60
_suspended_source_since: dict[str, float] = {}
# Consecutive failed lookups before a preview job is treated as deleted.
_MISSING_PREVIEW_STRIKES = 3
_AUTO_PREVIEW_HISTORY_RETENTION_SECONDS = 14 * 24 * 60 * 60
_AUTO_PREVIEW_HISTORY_CLEANUP_INTERVAL_SECONDS = 60 * 60
_ERROR_REPORT_SCAN_INTERVAL_SECONDS = 10
# Probing only acts at the very start of a render and once its probes land, so
# it does not need to run as often as the preview scans.
_PROBE_SCAN_INTERVAL_SECONDS = 10
_ERROR_REPORT_MAX_AGE_SECONDS = 24 * 60 * 60
_ERROR_ALERT_CACHE_TTL_SECONDS = 14 * 24 * 60 * 60
_last_auto_preview_history_cleanup_monotonic = 0.0
# Previews waiting on machines that cannot take them: id -> first seen.
_stranded_preview_since: dict[str, float] = {}
# Renders whose preview was replaced recently, so a replacement that fails the
# same way is not replaced again in an endless circle.
_recently_replaced_previews = TTLCache(ttl_seconds=3600, max_size=1000)
_error_alert_cache = TTLCache(
    ttl_seconds=_ERROR_ALERT_CACHE_TTL_SECONDS,
    max_size=100000,
)
_ERROR_ALERT_RECIPIENT_MODE_JOB_USER = "job_user"
_ERROR_ALERT_RECIPIENT_MODE_ERROR_WORKER = "error_worker"
_ERROR_ALERT_RECIPIENT_MODE_BOTH = "both"
_ERROR_ALERT_SEVERITY_CRITICAL = "critical"
_ERROR_ALERT_SEVERITY_WARNING = "warning"
_ERROR_ALERT_SEVERITY_INFO = "info"
_ErrorAlertRecipientMode = Literal[
    "job_user",
    "error_worker",
    "both",
]
_ErrorAlertSeverity = Literal[
    "critical",
    "warning",
    "info",
]


@dataclass(slots=True)
class _WatcherUser:
    telegram_user_id: int
    login: str
    password: str
    notifications_enabled: bool
    notification_scope: str
    auto_scope: str
    preview_worker: Optional[str]
    auto_preview_enabled: bool
    probe_scope: str = DEFAULT_PROBE_SCOPE


@dataclass(frozen=True, slots=True)
class _ErrorAlertRule:
    key: str
    label: str
    matcher: Callable[[dict], bool]
    recipient_mode: _ErrorAlertRecipientMode
    severity: _ErrorAlertSeverity
    # How many matching reports a job needs before this is worth a message.
    # Above 1 for failures whose first few occurrences are business as usual.
    min_occurrences: int = 1


def _normalize_identity(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return raw.split("\\")[-1].split("/")[-1]


def _identity_variants(value: object) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return set()
    short = raw.split("\\")[-1].split("/")[-1]
    return {raw, short}


def _job_matches_scope(scope_value: str, login_value: str, props: dict, job_entry: dict) -> bool:
    if scope_value != "own":
        return True
    job_owner = props.get("User") or job_entry.get("UserName") or ""
    if not job_owner:
        return False
    normalized_login = str(login_value).split("\\")[-1].split("/")[-1].lower()
    normalized_owner = str(job_owner).split("\\")[-1].split("/")[-1].lower()
    return normalized_owner == normalized_login


def _extract_preview_owner_id(props: dict, job_id: str) -> Optional[int]:
    extra_dict = props.get("ExDic") or {}
    if not isinstance(extra_dict, dict):
        extra_dict = {}

    preview_owner_str = extra_dict.get("PreviewTelegram")
    for key in (
        "ExtraInfoKeyValue0",
        "ExtraInfoKeyValue1",
        "ExtraInfoKeyValue2",
        "ExtraInfoKeyValue3",
        "ExtraInfoKeyValue4",
    ):
        value = props.get(key)
        if not value or "=" not in value:
            continue
        prefix, payload = value.split("=", 1)
        if prefix == "PreviewTelegram":
            preview_owner_str = payload

    if not preview_owner_str:
        return None

    try:
        return int(str(preview_owner_str).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Invalid PreviewTelegram value '%s' for job %s",
            preview_owner_str,
            job_id,
        )
        return None


def _is_preview_job(props: dict) -> bool:
    comment = str(props.get("Cmmt") or "")
    name = str(props.get("Name") or "").split("/")[-1]
    extra_dict = props.get("ExDic") or {}
    if not isinstance(extra_dict, dict):
        extra_dict = {}
    return (
        "Preview job generated by TasksBot" in comment
        or name.endswith(" - Preview")
        or extra_dict.get("PreviewJob") == "1"
    )


def _is_recently_completed(job: dict, props: dict) -> bool:
    date_comp_str = job.get("DateComp") or props.get("DateComp")
    if not date_comp_str or date_comp_str == "0001-01-01T00:00:00Z":
        return False

    try:
        date_comp = datetime.fromisoformat(date_comp_str.replace("Z", "+00:00"))
    except Exception:
        return False

    now = datetime.now(timezone.utc)
    return (now - date_comp) <= timedelta(minutes=10)


def _parse_datetime_utc(raw_value: object) -> Optional[datetime]:
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    if not raw or raw == "0001-01-01T00:00:00Z":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_job_candidate_for_error_scan(job: dict) -> bool:
    stat = int(job.get("Stat", 0) or 0)
    # Scan only jobs that are rendering or waiting to render.
    return stat in {1, 6}  # Active, Pending


def _matches_redshift_activation_error(report: dict) -> bool:
    title = str(report.get("Title") or "")
    log_err = str(report.get("LogErr") or "")
    combined = f"{title}\n{log_err}".lower()
    return "redshift activation" in combined


def _report_search_text(report: dict) -> str:
    chunks: list[str] = []

    def walk(node: object) -> None:
        if len(chunks) >= 64:
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            text = node.strip()
            if text:
                chunks.append(text)

    walk(report)
    return "\n".join(chunks)


def _clean_report_path(raw_path: str) -> str:
    return raw_path.strip().strip('"').strip("'").rstrip(".")


def _extract_report_path(report: dict) -> Optional[str]:
    search_text = _report_search_text(report)
    for pattern in (_UNABLE_TO_OPEN_FILE_RE, _ERROR_LOADING_RE, _INPUT_FILE_RE):
        match = pattern.search(search_text)
        if match:
            candidate = _clean_report_path(match.group(1))
            if candidate:
                return candidate
    return None


# A render that starts before Dropbox has finished syncing the scene fails a few
# tasks and then picks up as the file lands. That is normal here and must not
# raise an alarm - only a job that keeps failing has a real problem, which is
# either the rendering machine not syncing or the scene never finishing its
# upload from the machine that submitted it.
_SCENE_NOT_READY_MIN_OCCURRENCES = 10


_SCENE_LOAD_FAILURE_PHRASES = ("unable to open file:", "error loading:")


def _scene_load_failure_path(report: dict) -> Optional[str]:
    """The file a failed scene load names, whichever wording Deadline used.

    Deadline puts nothing useful in the report title for these - it reads
    "Caught exception: The attempted operation failed." - so a match only
    happens once the watcher has fetched the full report contents, where the
    real line lives.
    """
    search_text = _report_search_text(report).lower()
    if not any(phrase in search_text for phrase in _SCENE_LOAD_FAILURE_PHRASES):
        return None
    return _extract_report_path(report)


def _matches_scene_not_ready_error(report: dict) -> bool:
    """Houdini could not load the scene from shared storage."""
    path = _scene_load_failure_path(report)
    if not path:
        return False
    # A local C: path is somebody submitting from their own workstation, which
    # is a different problem with different advice - see the rule below.
    return not path.lower().startswith("c:")


def _matches_plugin_sandbox_error(report: dict) -> bool:
    """The worker cannot start the sandbox process its plugins run in.

    Seen on NodeC during SHA_0070_DS_v019: 100 tasks failed in 23 minutes, every
    one of them on that machine while the rest of the farm rendered the same job
    fine. The sandbox child process could not connect back to the worker's own
    command listener on loopback:

        SocketException (10013): An attempt was made to access a socket in a way
        forbidden by its access permissions. [::1]:29293

    Nothing about the job is wrong, so a resubmit changes nothing and the machine
    keeps eating tasks and failing them - which is what makes this worth an alert
    rather than a line in the log.
    """
    search_text = _report_search_text(report).lower()
    if not search_text:
        return False
    # Deliberately narrow. "Failed to load the plugin because:" alone also covers
    # a missing DCC version or a bad plugin config, and this alert's advice - the
    # loopback port - would be wrong for those. Across the 65 jobs currently on
    # the farm these two phrases caught all 119 sandbox failures and nothing else.
    return (
        "could not initialize the plugin sandbox" in search_text
        or "sandbox process exited unexpectedly" in search_text
    )


def _matches_local_c_drive_open_error(report: dict) -> bool:
    path = _scene_load_failure_path(report)
    return bool(path) and path.lower().startswith("c:")


# Add new report alert types here: key + display label + predicate.
_ERROR_ALERT_RULES: tuple[_ErrorAlertRule, ...] = (
    _ErrorAlertRule(
        key="redshift_activation",
        label="Redshift activation error",
        matcher=_matches_redshift_activation_error,
        recipient_mode=_ERROR_ALERT_RECIPIENT_MODE_BOTH,
        severity=_ERROR_ALERT_SEVERITY_CRITICAL,
    ),
    _ErrorAlertRule(
        key="local_c_drive_open_error",
        label="Local C: path is not accessible on worker",
        matcher=_matches_local_c_drive_open_error,
        recipient_mode=_ERROR_ALERT_RECIPIENT_MODE_JOB_USER,
        severity=_ERROR_ALERT_SEVERITY_WARNING,
    ),
    _ErrorAlertRule(
        key="scene_not_ready",
        label="Scene file cannot be opened on the farm",
        matcher=_matches_scene_not_ready_error,
        recipient_mode=_ERROR_ALERT_RECIPIENT_MODE_BOTH,
        severity=_ERROR_ALERT_SEVERITY_WARNING,
        min_occurrences=_SCENE_NOT_READY_MIN_OCCURRENCES,
    ),
    # Both recipients: only the machine's owner can fix it, but the job's user
    # is the one watching their frames fail.
    _ErrorAlertRule(
        key="plugin_sandbox_error",
        label="Worker cannot start the plugin sandbox",
        matcher=_matches_plugin_sandbox_error,
        recipient_mode=_ERROR_ALERT_RECIPIENT_MODE_BOTH,
        severity=_ERROR_ALERT_SEVERITY_CRITICAL,
    ),
)


def _match_error_alert_rule(report: dict) -> Optional[_ErrorAlertRule]:
    for rule in _ERROR_ALERT_RULES:
        try:
            if rule.matcher(report):
                return rule
        except Exception:
            continue
    return None


def _is_recent_error_report(report: dict) -> bool:
    reported_at = _parse_datetime_utc(report.get("Date"))
    if reported_at is None:
        return True
    return (
        datetime.now(timezone.utc) - reported_at
    ) <= timedelta(seconds=_ERROR_REPORT_MAX_AGE_SECONDS)


def _report_dedupe_id(job_id: str, report: dict, rule_key: str) -> str:
    if rule_key in ("local_c_drive_open_error", "scene_not_ready"):
        # One message about the job, however many of its tasks tripped over it.
        return f"{rule_key}:{str(job_id or '').strip()}"

    if rule_key in ("plugin_sandbox_error", "redshift_activation"):
        # A worker in this state fails every task it is handed - SHA_0070_DS_v019
        # collected 100 sandbox reports from one machine in 23 minutes, and an
        # unlicensed Redshift on NodeB sent nine identical alerts for
        # SHC_0170_ID_v022 in half an hour. Alert once per machine per job; per
        # report would be a flood, and the message would say the same thing
        # every time.
        slave = _normalize_identity(report.get("Slave")) or "unknown"
        return f"{rule_key}:{str(job_id or '').strip()}:{slave}"

    report_id = str(report.get("_id") or "").strip()
    if report_id:
        return f"{rule_key}:{report_id}"
    fallback = "|".join(
        [
            str(rule_key or "").strip(),
            str(job_id or "").strip(),
            str(report.get("Task") or "").strip(),
            str(report.get("Slave") or "").strip(),
            str(report.get("Date") or "").strip(),
            str(report.get("Title") or "").strip(),
        ]
    )
    return fallback


def _build_error_alert_text(
    report: dict,
    rule: _ErrorAlertRule,
    occurrences: int = 1,
) -> str:
    worker_name = html.escape(str(report.get("Slave") or "Unknown"))
    job_name = html.escape(str(report.get("JobName") or report.get("Job") or "Unknown job"))
    title_raw = str(report.get("Title") or report.get("LogErr") or "Unknown error").strip()
    # Deadline appends the .NET frames it raised from ("at Deadline.Plugins.
    # PluginWrapper.RenderTasks(...)"). They say where the worker noticed the
    # failure, never why, and in a chat message they bury the line that does.
    title_raw = "\n".join(
        line
        for line in title_raw.splitlines()
        if not (line.strip().startswith("at ") and "(" in line)
    ).strip() or title_raw
    if len(title_raw) > 900:
        title_raw = title_raw[:897].rstrip() + "..."
    error_text = html.escape(title_raw)
    rule_label = html.escape(rule.label)
    severity_icon, severity_label = _format_error_alert_severity(rule.severity)

    header_lines = [
        f"{severity_icon} <b>{severity_label}</b>",
        "",
        f"🏷️ <b>Issue</b>: <code>{rule_label}</code>",
        "",
        f"🎬 <b>Job</b>: <code>{job_name}</code>",
        f"🖥️ <b>Worker</b>: <code>{worker_name}</code>",
    ]

    if rule.key == "local_c_drive_open_error":
        local_path = _extract_report_path(report)
        details = list(header_lines)
        if local_path:
            details.extend(
                [
                    "",
                    "📁 <b>Path</b>:",
                    f"<code>{html.escape(local_path)}</code>",
                ]
            )
        details.extend(
            [
                "",
                "💡 <b>What To Do</b>:",
                "• Submit the scene from a shared or network path.",
                "• Do not submit from a local <code>C:</code> path that exists only on your workstation.",
            ]
        )
        return "\n".join(details)

    if rule.key == "scene_not_ready":
        scene_path = _extract_report_path(report)
        details = list(header_lines)
        details.append(f"📉 <b>Failed tasks</b>: {occurrences}")
        if scene_path:
            details.extend(
                [
                    "",
                    "📁 <b>File</b>:",
                    f"<code>{html.escape(scene_path)}</code>",
                ]
            )
        details.extend(
            [
                "",
                "💡 <b>What To Do</b>:",
                "• A few of these at the start of a render are normal - the file "
                "is still syncing. This many means it is not arriving.",
                "• On the machine that submitted it: check Dropbox finished "
                "uploading the scene and everything it references.",
                f"• On <code>{worker_name}</code>: check Dropbox is running and "
                "the file is downloaded, not just a placeholder.",
            ]
        )
        return "\n".join(details)

    if rule.key == "redshift_activation":
        # Two wordings reach this rule from the same machine: the licence
        # server could not be reached (WinHTTP 12002 is a timeout), or Redshift
        # has no licence at all and pops the key prompt. Only one alert per
        # machine per job gets sent, so the advice has to cover both.
        return "\n".join(
            header_lines
            + [
                "",
                f"📝 <b>Message</b>: <code>{error_text}</code>",
                "",
                "💡 <b>What To Do</b>:",
                f"• The job is fine - Redshift on <code>{worker_name}</code> has no "
                "licence, so that machine fails every task it takes. Resubmitting "
                "will not help.",
                "• <code>HTTP send failure (12002)</code> means it could not reach the "
                "Maxon licence server: check internet, proxy and firewall on that machine.",
                "• <code>Enter activation key</code> means it is not signed in: open the "
                "Maxon App there, sign in and check a Redshift licence is assigned to it "
                "and not held by another machine.",
                "• Until it is fixed, take the machine offline so it stops eating tasks.",
            ]
        )

    if rule.key == "plugin_sandbox_error":
        return "\n".join(
            header_lines
            + [
                "",
                f"📝 <b>Message</b>: <code>{error_text}</code>",
                "",
                "💡 <b>What To Do</b>:",
                f"• The job is fine - <code>{worker_name}</code> is failing every task "
                "it takes. Resubmitting will not help.",
                "• On that machine the plugin sandbox cannot reach the worker's own "
                "loopback port, usually because the port sits in a reserved range "
                "(Hyper-V/WSL/Docker) or a security tool blocks it.",
                "• Check with <code>netsh int ipv4 show excludedportrange protocol=tcp</code>, "
                "then restart the worker; <code>net stop winnat</code> / "
                "<code>net start winnat</code> frees a Hyper-V reservation.",
                "• Until it is fixed, take the machine offline so it stops eating tasks.",
            ]
        )

    return "\n".join(
        header_lines
        + [
            "",
            f"📝 <b>Message</b>: <code>{error_text}</code>",
        ]
    )


def _format_error_alert_severity(severity: _ErrorAlertSeverity) -> tuple[str, str]:
    if severity == _ERROR_ALERT_SEVERITY_CRITICAL:
        return "🔴", "Critical"
    if severity == _ERROR_ALERT_SEVERITY_WARNING:
        return "🟡", "Warning"
    return "🟢", "Info"


def _build_notification_users_by_identity(
    users: list[_WatcherUser],
) -> dict[str, set[int]]:
    identity_map: dict[str, set[int]] = {}
    for user in users:
        for variant in _identity_variants(user.login):
            bucket = identity_map.setdefault(variant, set())
            bucket.add(user.telegram_user_id)
    return identity_map


def _report_debug_summary(report: dict) -> str:
    report_id = str(report.get("_id") or "-").strip()
    slave = str(report.get("Slave") or "-").strip()
    job_user = str(report.get("JobUser") or "-").strip()
    title = str(report.get("Title") or report.get("LogErr") or "").strip().replace("\r", " ").replace("\n", " ")
    if len(title) > 140:
        title = title[:137].rstrip() + "..."
    path = _extract_report_path(report) or "-"
    has_contents = bool(str(report.get("ErrorContents") or "").strip())
    return (
        f"report_id={report_id} slave={slave} job_user={job_user} "
        f"has_contents={int(has_contents)} path={path} title={title or '-'}"
    )


def _resolve_error_alert_recipient_ids(
    report: dict,
    recipient_mode: _ErrorAlertRecipientMode,
    users_by_identity: dict[str, set[int]],
) -> set[int]:
    recipient_ids: set[int] = set()

    if recipient_mode in {
        _ERROR_ALERT_RECIPIENT_MODE_JOB_USER,
        _ERROR_ALERT_RECIPIENT_MODE_BOTH,
    }:
        job_user = _normalize_identity(report.get("JobUser"))
        if job_user:
            recipient_ids.update(users_by_identity.get(job_user, set()))

    if recipient_mode in {
        _ERROR_ALERT_RECIPIENT_MODE_ERROR_WORKER,
        _ERROR_ALERT_RECIPIENT_MODE_BOTH,
    }:
        error_worker = _normalize_identity(report.get("Slave"))
        if error_worker:
            recipient_ids.update(users_by_identity.get(error_worker, set()))

    return recipient_ids


async def _scan_error_reports_candidates(users: list[_WatcherUser]) -> int:
    """Scan Deadline job error reports and notify users by configured alert rules."""
    from app.services.deadline import (
        get_job_error_reports,
        get_job_report_contents,
        get_jobs_by_credentials,
    )

    notification_users = [user for user in users if user.notifications_enabled]
    if not notification_users:
        return 0
    users_by_identity = _build_notification_users_by_identity(notification_users)

    max_parallel = min(4, max(1, len(notification_users)))
    semaphore = asyncio.Semaphore(max_parallel)

    async def _scan_user(user: _WatcherUser) -> int:
        async with semaphore:
            try:
                jobs = await get_jobs_by_credentials(
                    user.login,
                    user.password,
                    use_cache=True,
                )
            except Exception as exc:
                logger.error(
                    "Watcher: failed loading jobs for notifications user %s: %s",
                    user.telegram_user_id,
                    exc,
                )
                return 0

            logger.info(
                "Watcher: notification scan start user_id=%s login=%s scope=%s jobs=%s",
                user.telegram_user_id,
                user.login,
                user.notification_scope,
                len(jobs),
            )

            candidate_job_ids: list[str] = []
            seen_job_ids: set[str] = set()
            for job in jobs:
                if not isinstance(job, dict):
                    continue

                job_id_raw = job.get("_id")
                job_id = str(job_id_raw).strip() if job_id_raw else ""
                if not job_id or job_id in seen_job_ids:
                    continue

                props = job.get("Props") or {}
                if not isinstance(props, dict):
                    props = {}

                if _is_preview_job(props):
                    continue
                if not _job_matches_scope(user.notification_scope, user.login, props, job):
                    continue
                if not _is_job_candidate_for_error_scan(job):
                    continue

                seen_job_ids.add(job_id)
                candidate_job_ids.append(job_id)
                if len(candidate_job_ids) >= 40:
                    break

            logger.info(
                "Watcher: notification scan candidates user_id=%s login=%s candidate_jobs=%s ids=%s",
                user.telegram_user_id,
                user.login,
                len(candidate_job_ids),
                candidate_job_ids[:10],
            )

            sent_count = 0
            for job_id in candidate_job_ids:
                try:
                    reports = await get_job_error_reports(user.login, user.password, job_id)
                except Exception as exc:
                    logger.error(
                        "Watcher: failed loading error reports for user %s job %s: %s",
                        user.telegram_user_id,
                        job_id,
                        exc,
                    )
                    continue

                logger.info(
                    "Watcher: loaded error reports user_id=%s login=%s job_id=%s count=%s",
                    user.telegram_user_id,
                    user.login,
                    job_id,
                    len(reports),
                )

                # How many of this job's reports each alert has matched so far.
                # A rule with min_occurrences above 1 stays quiet until its count
                # gets there; the dedupe cache then keeps it to one message.
                occurrences: dict[str, int] = {}

                for report in reports:
                    if not isinstance(report, dict):
                        continue
                    if not _is_recent_error_report(report):
                        logger.info(
                            "Watcher: skipped stale report user_id=%s job_id=%s %s",
                            user.telegram_user_id,
                            job_id,
                            _report_debug_summary(report),
                        )
                        continue
                    report_payload = report
                    matched_rule = _match_error_alert_rule(report_payload)
                    logger.info(
                        "Watcher: short report evaluation user_id=%s job_id=%s matched_rule=%s %s",
                        user.telegram_user_id,
                        job_id,
                        matched_rule.key if matched_rule else "-",
                        _report_debug_summary(report_payload),
                    )
                    if matched_rule is None:
                        report_id = str(report.get("_id") or "").strip()
                        if report_id:
                            try:
                                report_contents = await get_job_report_contents(
                                    user.login,
                                    user.password,
                                    job_id,
                                    report_id,
                                )
                            except Exception as exc:
                                logger.error(
                                    "Watcher: failed loading error contents for user %s job %s report %s: %s",
                                    user.telegram_user_id,
                                    job_id,
                                    report_id,
                                    exc,
                                )
                                report_contents = None

                            if report_contents:
                                logger.info(
                                    "Watcher: loaded error contents user_id=%s job_id=%s report_id=%s chars=%s",
                                    user.telegram_user_id,
                                    job_id,
                                    report_id,
                                    len(report_contents),
                                )
                                report_payload = dict(report)
                                report_payload["ErrorContents"] = report_contents
                                matched_rule = _match_error_alert_rule(report_payload)
                                logger.info(
                                    "Watcher: full report evaluation user_id=%s job_id=%s matched_rule=%s %s",
                                    user.telegram_user_id,
                                    job_id,
                                    matched_rule.key if matched_rule else "-",
                                    _report_debug_summary(report_payload),
                                )
                            else:
                                logger.info(
                                    "Watcher: no error contents user_id=%s job_id=%s report_id=%s",
                                    user.telegram_user_id,
                                    job_id,
                                    report_id,
                                )

                    if matched_rule is None:
                        logger.info(
                            "Watcher: no alert rule matched user_id=%s job_id=%s %s",
                            user.telegram_user_id,
                            job_id,
                            _report_debug_summary(report_payload),
                        )
                        continue

                    dedupe_id = _report_dedupe_id(job_id, report, matched_rule.key)
                    if not dedupe_id:
                        logger.info(
                            "Watcher: empty dedupe id user_id=%s job_id=%s rule=%s",
                            user.telegram_user_id,
                            job_id,
                            matched_rule.key,
                        )
                        continue
                    seen_so_far = occurrences.get(dedupe_id, 0) + 1
                    occurrences[dedupe_id] = seen_so_far
                    if seen_so_far < matched_rule.min_occurrences:
                        logger.info(
                            "Watcher: below alert threshold user_id=%s job_id=%s rule=%s %s/%s",
                            user.telegram_user_id,
                            job_id,
                            matched_rule.key,
                            seen_so_far,
                            matched_rule.min_occurrences,
                        )
                        continue

                    recipient_ids = _resolve_error_alert_recipient_ids(
                        report_payload,
                        matched_rule.recipient_mode,
                        users_by_identity,
                    )
                    logger.info(
                        "Watcher: recipient resolution user_id=%s job_id=%s rule=%s mode=%s recipients=%s",
                        user.telegram_user_id,
                        job_id,
                        matched_rule.key,
                        matched_rule.recipient_mode,
                        sorted(recipient_ids),
                    )
                    if not recipient_ids:
                        continue

                    alert_text = _build_error_alert_text(
                        report_payload, matched_rule, seen_so_far
                    )
                    for recipient_user_id in recipient_ids:
                        dedupe_key = (dedupe_id, recipient_user_id)
                        if dedupe_key in _error_alert_cache:
                            logger.info(
                                "Watcher: skipped duplicate alert user_id=%s job_id=%s recipient=%s dedupe_id=%s",
                                user.telegram_user_id,
                                job_id,
                                recipient_user_id,
                                dedupe_id,
                            )
                            continue

                        try:
                            await bot.send_message(
                                recipient_user_id,
                                alert_text,
                                parse_mode="HTML",
                            )
                        except Exception as exc:
                            logger.error(
                                "Watcher: failed sending report alert to user %s for job %s: %s",
                                recipient_user_id,
                                job_id,
                                exc,
                            )
                            continue

                        _error_alert_cache.add(dedupe_key)
                        sent_count += 1
                        logger.info(
                            "Watcher: alert sent user_id=%s job_id=%s recipient=%s rule=%s dedupe_id=%s",
                            user.telegram_user_id,
                            job_id,
                            recipient_user_id,
                            matched_rule.key,
                            dedupe_id,
                        )

            return sent_count

    tasks = [asyncio.create_task(_scan_user(user)) for user in notification_users]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_sent = 0
    for user, result in zip(notification_users, results):
        if isinstance(result, Exception):
            logger.error(
                "Watcher: error report scan failed for user %s: %s",
                user.telegram_user_id,
                result,
            )
            continue
        total_sent += int(result)

    return total_sent


async def _load_watcher_users() -> list[_WatcherUser]:
    conn = get_db_connection()
    if conn is None:
        return []

    user_rows = []
    try:
        async with conn.execute(
            """
            SELECT telegram_user_id,
                   deadline_login,
                   deadline_password,
                   notifications_enabled,
                   notification_scope,
                   preview_default_worker,
                   preview_auto_enabled,
                   preview_auto_scope,
                   probe_scope
            FROM user_sessions
            """
        ) as cursor:
            user_rows = await cursor.fetchall()
    except Exception as exc:
        logger.error("Error fetching users for monitoring: %s", exc)
        return []

    users: list[_WatcherUser] = []
    for row in user_rows:
        (
            user_id,
            login,
            encrypted_password,
            notifications_enabled,
            scope_raw,
            preview_worker,
            preview_auto_enabled,
            preview_auto_scope_raw,
            probe_scope_raw,
        ) = row

        if not user_id or not login or not encrypted_password:
            continue

        try:
            decrypted_password = _decrypt_password(encrypted_password)
        except Exception:
            decrypted_password = encrypted_password

        scope = _normalize_scope(scope_raw)
        auto_scope = _normalize_scope(preview_auto_scope_raw) if preview_auto_scope_raw else scope

        users.append(
            _WatcherUser(
                telegram_user_id=int(user_id),
                login=str(login),
                password=str(decrypted_password),
                notifications_enabled=bool(notifications_enabled),
                notification_scope=scope,
                auto_scope=auto_scope,
                preview_worker=preview_worker,
                auto_preview_enabled=bool(preview_auto_enabled),
                probe_scope=_normalize_probe_scope(probe_scope_raw),
            )
        )

    return users


async def _collect_active_preview_targets(
    users: list[_WatcherUser],
) -> list[tuple[str, _WatcherUser]]:
    """Preview jobs to check this tick.

    The authoritative source is Deadline itself: every unfinished preview job
    owned by a watched user is picked up, so a bot restart does not lose track
    of previews it queued earlier. In-memory registries only add jobs that were
    submitted moments ago and may not be in the cached job list yet.
    """
    if not users:
        return []

    from app.services.deadline import get_jobs_by_credentials

    users_by_id = {user.telegram_user_id: user for user in users}
    targets: list[tuple[str, _WatcherUser]] = []
    seen: set[str] = set()

    def _add(preview_job_id: str, user: _WatcherUser) -> None:
        if not preview_job_id or preview_job_id in seen:
            return
        seen.add(preview_job_id)
        targets.append((preview_job_id, user))

    for user in users:
        try:
            jobs = await get_jobs_by_credentials(user.login, user.password, use_cache=True)
        except Exception as exc:
            logger.warning(
                "Watcher: could not list jobs while collecting previews for %s: %s",
                user.telegram_user_id,
                exc,
            )
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            props = job.get("Props") or {}
            if not isinstance(props, dict) or not _is_preview_job(props):
                continue
            if job.get("Stat", 0) == 3 and not _is_recently_completed(job, props):
                continue  # long-finished previews were already handled
            owner_id = _extract_preview_owner_id(props, user.telegram_user_id)
            if owner_id != user.telegram_user_id:
                continue
            preview_job_id = str(job.get("_id") or "").strip()
            _add(preview_job_id, user)

            # Re-learn which render each preview waits on, so a restarted bot
            # can still release them the moment that render finishes.
            if preview_job_id and preview_job_id not in preview_tracked_jobs:
                from app.services.preview.runtime import (
                    _extract_preview_context,
                    track_preview_job,
                )

                try:
                    *_, source_job_id = _extract_preview_context(props, owner_id)
                except Exception:
                    source_job_id = None
                if source_job_id and preview_job_id not in preview_message_registry:
                    track_preview_job(preview_job_id, owner_id, source_job_id)

    # Freshly submitted previews may not be in the cached listing yet.
    for preview_job_id, (chat_id, _message_id) in preview_message_registry.items():
        try:
            user = users_by_id.get(int(chat_id))
        except (TypeError, ValueError):
            continue
        if user is not None:
            _add(preview_job_id, user)

    for preview_job_id, (owner_id, _source_id) in preview_tracked_jobs.items():
        user = users_by_id.get(owner_id)
        if user is not None:
            _add(preview_job_id, user)

    return targets


async def _handle_missing_preview_job(preview_job_id: str, user: _WatcherUser) -> None:
    """Stop following a preview job that disappeared from Deadline.

    Usually it was deleted by hand (or cleaned up by the farm). A few misses are
    tolerated first, because a lookup can also fail transiently. Once it is
    considered gone, the dedupe records are cleared so the render can get a new
    preview instead of being skipped forever - unless that preview is gone
    precisely because it delivered, which is not a run owed anything.
    """
    strikes = preview_missing_strikes.get(preview_job_id, 0) + 1
    preview_missing_strikes[preview_job_id] = strikes
    if strikes < _MISSING_PREVIEW_STRIKES:
        return

    from app.services.preview.runtime import untrack_preview_job

    tracked = untrack_preview_job(preview_job_id)
    pop_preview_message(preview_job_id)
    preview_missing_strikes.pop(preview_job_id, None)

    source_job_id = tracked[1] if tracked else None
    logger.info(
        "Watcher: preview job %s is gone from Deadline; no longer following it",
        preview_job_id,
    )
    if not source_job_id:
        return

    if (preview_job_id, user.telegram_user_id) in notified_jobs:
        # The bot deletes a preview once its video is in the chat, so this is
        # the ordinary end of a preview, not a lost one. Clearing the records
        # here sent a second copy of the video the user had just received: the
        # render still counted as recently completed, so the next scan saw a
        # completion with nothing on record and made another preview of it.
        logger.debug(
            "Watcher: preview %s was delivered before it vanished; run stays on record",
            preview_job_id,
        )
        return

    auto_preview_jobs.remove((source_job_id, user.telegram_user_id))
    await _unregister_auto_preview_history(user.telegram_user_id, source_job_id)


async def _process_active_preview_job(
    preview_job_id: str,
    user: _WatcherUser,
) -> None:
    from app.services.deadline import get_job_info_direct

    try:
        job = await get_job_info_direct(user.login, user.password, preview_job_id)
    except Exception as exc:
        logger.error(
            "Watcher: failed loading preview job %s for user %s: %s",
            preview_job_id,
            user.telegram_user_id,
            exc,
        )
        return

    if not job:
        await _handle_missing_preview_job(preview_job_id, user)
        return

    preview_missing_strikes.pop(preview_job_id, None)

    props = job.get("Props", {})
    if not _is_preview_job(props):
        return

    preview_owner_id = _extract_preview_owner_id(props, preview_job_id)
    target_user = preview_owner_id or user.telegram_user_id
    if target_user != user.telegram_user_id:
        return

    stat = job.get("Stat", 0)
    name = str(props.get("Name") or "").split("/")[-1]
    notified_key = (preview_job_id, target_user)

    if stat == 4:
        if notified_key in notified_jobs:
            return
        notified_user_id = await _notify_preview_job_failure(
            user.telegram_user_id,
            job,
            name,
            user.login,
            user.password,
        )
        resolved_user_id = notified_user_id or target_user
        notified_jobs.add((preview_job_id, resolved_user_id))
        return

    if stat != 3 or not _is_recently_completed(job, props):
        return

    if notified_key in notified_jobs:
        return

    completion_result = await _notify_preview_job_completion(
        user.telegram_user_id,
        job,
        name,
        user.login,
        user.password,
    )
    if completion_result.status == "deferred":
        return
    resolved_user_id = completion_result.user_id or target_user
    notified_jobs.add((preview_job_id, resolved_user_id))


async def _process_active_previews_parallel(
    targets: list[tuple[str, _WatcherUser]],
) -> None:
    if not targets:
        return

    max_parallel = min(8, max(1, len(targets)))
    semaphore = asyncio.Semaphore(max_parallel)

    async def _run(job_id: str, user: _WatcherUser) -> None:
        async with semaphore:
            await _process_active_preview_job(job_id, user)

    tasks = [asyncio.create_task(_run(job_id, user)) for job_id, user in targets]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for (job_id, user), result in zip(targets, results):
        if isinstance(result, Exception):
            logger.error(
                "Watcher: active preview processing failed for user %s job %s: %s",
                user.telegram_user_id,
                job_id,
                result,
            )


# What a history row says about the render run it stands for: its preview is
# queued ("armed"), or that run has been handled ("delivered").
_RUN_ARMED = "armed"
_RUN_DELIVERED = "delivered"

# Answers to "does this run still need a preview?". UNKNOWN means the database
# could not say, and the in-memory dedupe is all there is to go on.
_CLAIM_NEW = "new"
_CLAIM_KNOWN = "known"
_CLAIM_UNKNOWN = "unknown"


async def _read_auto_preview_run(
    conn, telegram_user_id: int, job_id: str
) -> Optional[tuple]:
    async with conn.execute(
        """
        SELECT run_state, completed_at
        FROM auto_preview_history
        WHERE telegram_user_id = ? AND job_id = ?
        LIMIT 1
        """,
        (telegram_user_id, job_id),
    ) as cursor:
        return await cursor.fetchone()


async def _write_auto_preview_run(
    conn, telegram_user_id: int, job_id: str, run_state: str, completed_at: str
) -> None:
    await conn.execute(
        """
        INSERT INTO auto_preview_history
            (telegram_user_id, job_id, created_at, run_state, completed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id, job_id) DO UPDATE SET
            created_at = excluded.created_at,
            run_state = excluded.run_state,
            completed_at = excluded.completed_at
        """,
        (telegram_user_id, job_id, int(time.time()), run_state, completed_at),
    )
    await conn.commit()


async def _claim_completed_run(
    telegram_user_id: int, job_id: str, completed_at: str
) -> str:
    """Does this completion still need a preview?

    A render can finish more than once: requeueing a few tasks of a finished
    job starts another run, and those repaired frames are exactly what the
    user wants to see. So the question is not "was this job previewed" but
    "was *this completion* previewed", which is what the stored completion
    time answers.
    """
    conn = get_db_connection()
    if conn is None:
        return _CLAIM_UNKNOWN

    stamp = str(completed_at or "").strip()
    try:
        row = await _read_auto_preview_run(conn, telegram_user_id, job_id)
        if row is None:
            await _write_auto_preview_run(
                conn, telegram_user_id, job_id, _RUN_DELIVERED, stamp
            )
            return _CLAIM_NEW

        known = str(row[1] or "").strip()
        if not known:
            # Either a preview is already on its way for this run, or the row
            # predates run tracking. Adopt this completion as the one it
            # stands for rather than sending a preview twice.
            await _write_auto_preview_run(
                conn, telegram_user_id, job_id, _RUN_DELIVERED, stamp
            )
            return _CLAIM_KNOWN
        if not stamp or known == stamp:
            return _CLAIM_KNOWN

        await _write_auto_preview_run(
            conn, telegram_user_id, job_id, _RUN_DELIVERED, stamp
        )
        return _CLAIM_NEW
    except Exception as exc:
        logger.warning(
            "Watcher: failed to access auto_preview_history for user %s job %s: %s",
            telegram_user_id,
            job_id,
            exc,
        )
        return _CLAIM_UNKNOWN


async def _claim_new_run(telegram_user_id: int, job_id: str) -> str:
    """Is this render running a run that has no preview queued yet?

    True for a render seen for the first time, and again for one that is back
    at work after a run we already delivered - that is a requeue, and it earns
    its own preview.
    """
    conn = get_db_connection()
    if conn is None:
        return _CLAIM_UNKNOWN

    try:
        row = await _read_auto_preview_run(conn, telegram_user_id, job_id)
        if row is not None and str(row[0] or "").strip().lower() == _RUN_ARMED:
            return _CLAIM_KNOWN
        await _write_auto_preview_run(conn, telegram_user_id, job_id, _RUN_ARMED, "")
        return _CLAIM_NEW
    except Exception as exc:
        logger.warning(
            "Watcher: failed to access auto_preview_history for user %s job %s: %s",
            telegram_user_id,
            job_id,
            exc,
        )
        return _CLAIM_UNKNOWN


async def forget_auto_preview_run(job_id: str, telegram_user_id: Optional[int] = None) -> int:
    """Drop what we remember about a render's runs, for all of its watchers.

    Called when the farm says a job was requeued: whatever was previewed
    before, the render is producing new frames now and the next scan should
    treat it as a fresh run.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        return 0

    owners: list[int] = []
    conn = get_db_connection()
    if conn is not None:
        try:
            if telegram_user_id is None:
                async with conn.execute(
                    "SELECT telegram_user_id FROM auto_preview_history WHERE job_id = ?",
                    (job_id,),
                ) as cursor:
                    owners = [int(row[0]) for row in await cursor.fetchall()]
                await conn.execute(
                    "DELETE FROM auto_preview_history WHERE job_id = ?", (job_id,)
                )
            else:
                cursor = await conn.execute(
                    "DELETE FROM auto_preview_history WHERE job_id = ? AND telegram_user_id = ?",
                    (job_id, telegram_user_id),
                )
                if (cursor.rowcount or 0) > 0:
                    owners = [int(telegram_user_id)]
            await conn.commit()
        except Exception as exc:
            logger.warning(
                "Watcher: failed to clear auto_preview_history for job %s: %s",
                job_id,
                exc,
            )

    if telegram_user_id is None:
        auto_preview_jobs.remove_where(
            lambda key: isinstance(key, tuple) and key and key[0] == job_id
        )
    else:
        auto_preview_jobs.remove((job_id, telegram_user_id))
    return len(owners)


async def _maybe_cleanup_auto_preview_history() -> None:
    global _last_auto_preview_history_cleanup_monotonic

    now = time.monotonic()
    if (now - _last_auto_preview_history_cleanup_monotonic) < _AUTO_PREVIEW_HISTORY_CLEANUP_INTERVAL_SECONDS:
        return

    _last_auto_preview_history_cleanup_monotonic = now
    conn = get_db_connection()
    if conn is None:
        return

    cutoff = int(time.time()) - _AUTO_PREVIEW_HISTORY_RETENTION_SECONDS
    try:
        await conn.execute(
            "DELETE FROM auto_preview_history WHERE created_at < ?",
            (cutoff,),
        )
        await conn.commit()
    except Exception as exc:
        logger.warning("Watcher: failed to cleanup auto_preview_history: %s", exc)


async def _unregister_auto_preview_history(telegram_user_id: int, job_id: str) -> None:
    """Remove a (user, job) pair so the auto-preview flow can retry it later."""
    conn = get_db_connection()
    if conn is None:
        return
    try:
        await conn.execute(
            "DELETE FROM auto_preview_history WHERE telegram_user_id = ? AND job_id = ?",
            (telegram_user_id, job_id),
        )
        await conn.commit()
    except Exception as exc:
        logger.warning(
            "Watcher: failed to unregister auto_preview_history for user %s job %s: %s",
            telegram_user_id,
            job_id,
            exc,
        )


def _job_chunk_count(job: dict, key: str) -> int:
    try:
        return int(job.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _presubmit_ready(job: dict, props: dict) -> bool:
    """True when a running render should already have its preview queued.

    The preview is submitted as a Deadline dependency (Pending) of the render,
    so it costs nothing while it waits and cannot compete with the render for
    machines. That means it can be queued as soon as the render is really
    running, instead of trying to catch the moment its task queue drains.
    """
    del props  # queueing no longer depends on the render's machine list
    if _job_chunk_count(job, "FailedChunks") > 0:
        return False
    # Rendering or already producing frames: the job is genuinely underway.
    return (
        _job_chunk_count(job, "RenderingChunks") > 0
        or _job_chunk_count(job, "CompletedChunks") > 0
    )


async def _run_auto_preview_presubmit(
    telegram_user_id: int,
    job_id: str,
    job_name: str,
    default_worker: Optional[str],
) -> None:
    """Queue the auto preview as a Pending dependency of the running render."""
    from app.services.preview.runtime import _submit_auto_preview_deadline

    try:
        submitted = await _submit_auto_preview_deadline(
            telegram_user_id,
            job_id,
            job_name,
            default_worker,
            input_wait_seconds=settings.preview_presubmit_input_wait,
            notify_on_failure=False,
            waiting_for_render=True,
            depends_on=job_id,
        )
    except Exception as exc:
        logger.error("Auto preview presubmission failed for job %s: %s", job_id, exc)
        submitted = False

    if not submitted:
        # Fall back to the completion-time flow: clear the dedupe records so the
        # regular scan picks this job up once it completes.
        auto_preview_jobs.remove((job_id, telegram_user_id))
        await _unregister_auto_preview_history(telegram_user_id, job_id)


def _is_presubmitted_preview(props: dict) -> bool:
    """True only for previews the watcher queued before the render finished.

    Manually requested previews must never be suspended or deleted by the
    reconciler — the user asked for them and is waiting.
    """
    extra_dict = props.get("ExDic") or {}
    if isinstance(extra_dict, dict) and str(extra_dict.get("PreviewPresubmit") or "").strip() == "1":
        return True
    for key in (
        "ExtraInfoKeyValue0",
        "ExtraInfoKeyValue1",
        "ExtraInfoKeyValue2",
        "ExtraInfoKeyValue3",
        "ExtraInfoKeyValue4",
        "ExtraInfoKeyValue5",
        "ExtraInfoKeyValue6",
    ):
        value = props.get(key)
        if not value or "=" not in str(value):
            continue
        prefix, payload = str(value).split("=", 1)
        if prefix == "PreviewPresubmit" and payload.strip() == "1":
            return True
    return False


def _sources_with_live_previews(jobs: list, telegram_user_id: int) -> set[str]:
    """Renders that already have a preview job of their own on the farm.

    This holds back the preview a *running* render would otherwise get queued
    twice, and it is read before the run records are touched, so a render whose
    earlier preview is still up is left exactly as it was and its new run is
    picked up on a later pass, once that preview has delivered and gone.

    A completion is never held back this way. Whether a preview covers it is
    already recorded - an armed run says one is on its way - and a completion
    nobody has covered must be sent even while an older preview is still
    rendering the frames it replaced.
    """
    from app.services.preview.runtime import _extract_preview_context

    live: set[str] = set()
    for entry in jobs:
        if not isinstance(entry, dict):
            continue
        props = entry.get("Props") or {}
        if not isinstance(props, dict) or not _is_preview_job(props):
            continue
        # 1=queued/rendering, 2=suspended, 6=pending on its render
        if entry.get("Stat", 0) not in {1, 2, 6}:
            continue
        try:
            *_, owner_id, _, source_job_id = _extract_preview_context(
                props, telegram_user_id
            )
        except Exception:
            continue
        if source_job_id and owner_id == telegram_user_id:
            live.add(str(source_job_id))
    return live


async def _strand_check(user: "_WatcherUser", previews: list) -> set[str]:
    """Previews that are queued on machines which cannot take them.

    A Worker can be disabled or go offline after its preview was queued, and
    then the job waits in the queue with no error and no worker - the farm
    cannot tell anyone, because from Deadline's side nothing is wrong. Held for
    a grace period first, so a Worker restarting does not count.
    """
    from app.services.deadline import get_workers_by_credentials
    from app.services.preview.render import preview_cannot_run_anywhere

    # Only jobs waiting to start can be stranded; a rendering one has a worker.
    waiting = [
        (preview_id, props)
        for preview_id, preview, props in previews
        if preview.get("Stat", 0) == 1 and _job_chunk_count(preview, "RenderingChunks") == 0
    ]
    if not waiting:
        _stranded_preview_since.clear()
        return set()

    try:
        workers = await get_workers_by_credentials(user.login, user.password)
    except Exception as exc:
        logger.warning("Watcher: could not read workers for preview check: %s", exc)
        return set()
    if not workers:
        return set()

    stranded: set[str] = set()
    for preview_id, props in waiting:
        if not preview_cannot_run_anywhere(props, workers):
            _stranded_preview_since.pop(preview_id, None)
            continue
        since = _stranded_preview_since.setdefault(preview_id, time.monotonic())
        if (time.monotonic() - since) >= _STRANDED_PREVIEW_GRACE_SECONDS:
            stranded.add(preview_id)
    return stranded


async def _rescue_failing_preview(
    user: "_WatcherUser", preview_id: str, preview: dict, props: dict
) -> None:
    """Deal with a preview that keeps failing instead of letting it loop.

    Deadline hands a failed task straight back to the queue, so a preview that
    cannot finish - an upload the bot refuses, say - runs, fails and runs again
    every twenty minutes for as long as its render is on the farm, taking a
    machine each time and telling nobody. One replacement is worth trying,
    because a fresh preview job carries a fresh upload token and that is the
    failure worth retrying. If the replacement fails the same way, the render
    is out of luck and the user hears about it rather than the farm churning.
    """
    from app.services.preview.runtime import _extract_preview_context

    *_, owner_id, _, source_job_id = _extract_preview_context(
        props, user.telegram_user_id
    )
    if not source_job_id or owner_id != user.telegram_user_id:
        return

    errors = _job_chunk_count(preview, "Errs")
    if (source_job_id, owner_id) not in _recently_replaced_previews:
        _recently_replaced_previews.add((source_job_id, owner_id))
        logger.warning(
            "Watcher: preview %s failed %s times; replacing it", preview_id, errors
        )
        await _rescue_stranded_preview(
            user, preview_id, props, reason=f"it failed {errors} times"
        )
        return

    from app.services.deadline import delete_job

    logger.error(
        "Watcher: replacement preview %s failed again (%s errors); giving up",
        preview_id,
        errors,
    )
    with contextlib.suppress(Exception):
        await delete_job(user.login, user.password, preview_id)
    name = str(props.get("Name") or "").split("/")[-1].removesuffix(" - Preview")
    with contextlib.suppress(Exception):
        await bot.send_message(
            owner_id,
            f"❌ Preview for <b>{name or source_job_id}</b> keeps failing; "
            "the farm has stopped retrying it. Try 🔍 Preview on the job.",
            parse_mode="HTML",
        )


async def _rescue_stranded_preview(
    user: "_WatcherUser", preview_id: str, props: dict, *, reason: str = "it was stranded"
) -> None:
    """Replace a preview that cannot finish where it is with a fresh one."""
    from app.services.deadline import delete_job
    from app.services.preview.runtime import (
        _extract_preview_context,
        _submit_auto_preview_deadline,
        untrack_preview_job,
    )

    *_, owner_id, _, source_job_id = _extract_preview_context(
        props, user.telegram_user_id
    )
    if not source_job_id or owner_id != user.telegram_user_id:
        return

    try:
        deleted = await delete_job(user.login, user.password, preview_id)
    except Exception as exc:
        logger.warning("Watcher: could not delete stranded preview %s: %s", preview_id, exc)
        return
    if not deleted:
        return

    _stranded_preview_since.pop(preview_id, None)
    untrack_preview_job(preview_id)
    pop_preview_message(preview_id)
    logger.warning(
        "Watcher: preview %s replaced (%s); resubmitting for %s",
        preview_id,
        reason,
        source_job_id,
    )

    job_name = str(props.get("Name") or "").split("/")[-1].removesuffix(" - Preview")
    # The render has long finished by now, so this one starts straight away.
    # Submission re-reads the machine list and drops workers that cannot take
    # it, which is what keeps this from queueing another stranded preview.
    submitted = await _submit_auto_preview_deadline(
        user.telegram_user_id,
        source_job_id,
        job_name or source_job_id,
        user.preview_worker,
        notify_on_failure=False,
    )
    if not submitted:
        logger.error(
            "Watcher: could not resubmit preview for %s after it was stranded",
            source_job_id,
        )
        auto_preview_jobs.remove((source_job_id, user.telegram_user_id))
        await _unregister_auto_preview_history(user.telegram_user_id, source_job_id)


async def _reconcile_presubmitted_previews(user: "_WatcherUser", jobs: list) -> None:
    """Clean up pre-submitted previews whose render will never complete.

    Deadline keeps these previews Pending until their dependency finishes, so
    the only case left to handle is a render that failed or was deleted: its
    preview would wait forever (ResumeOnFailed/DeletedDependencies are off).
    Manually requested previews are never touched.
    """
    from app.services.deadline import delete_job
    from app.services.preview.runtime import _extract_preview_context

    jobs_by_id: dict[str, dict] = {}
    previews: list[tuple[str, dict, dict]] = []
    for entry in jobs:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("_id") or "").strip()
        if not entry_id:
            continue
        jobs_by_id[entry_id] = entry
        props = entry.get("Props") or {}
        if (
            isinstance(props, dict)
            and _is_preview_job(props)
            and _is_presubmitted_preview(props)
        ):
            previews.append((entry_id, entry, props))

    stranded = await _strand_check(user, previews)

    for preview_id, preview, props in previews:
        # 1=queued/rendering, 2=suspended, 6=pending on a dependency
        if preview.get("Stat", 0) not in {1, 2, 6}:
            continue

        if preview_id in stranded:
            await _rescue_stranded_preview(
                user, preview_id, props, reason="no worker may run it"
            )
            continue

        if _job_chunk_count(preview, "Errs") >= _PREVIEW_ERROR_LIMIT:
            await _rescue_failing_preview(user, preview_id, preview, props)
            continue

        _, _, _, target_user_id, _, source_job_id = _extract_preview_context(
            props, user.telegram_user_id
        )
        if not source_job_id or target_user_id != user.telegram_user_id:
            continue

        source = jobs_by_id.get(source_job_id)
        source_stat = source.get("Stat", 0) if source is not None else None

        reason: Optional[str] = None
        if source is None:
            reason = "source render was deleted"
        elif source_stat == 4:
            reason = "source render failed"
        elif source_stat == 2:
            # Paused render: keep the preview for a while (the user may just be
            # fixing something), then release it so it does not sit in the queue
            # forever holding its upload token.
            paused_since = _suspended_source_since.setdefault(
                source_job_id, time.monotonic()
            )
            if (time.monotonic() - paused_since) >= _SUSPENDED_SOURCE_GRACE_SECONDS:
                reason = "source render stayed paused"
        else:
            _suspended_source_since.pop(source_job_id, None)

        if reason is None:
            # Deadline holds the preview Pending until the render completes and
            # the farm event plugin releases it immediately.
            continue

        try:
            deleted = await delete_job(user.login, user.password, preview_id)
        except Exception as exc:
            logger.warning("Watcher: failed to delete stale preview %s: %s", preview_id, exc)
            continue
        if deleted:
            logger.info("Watcher: removed preview %s because its %s", preview_id, reason)
            _suspended_source_since.pop(source_job_id, None)
            # Forget the dedupe records so resuming the render queues a new
            # preview instead of silently skipping it.
            auto_preview_jobs.remove((source_job_id, target_user_id))
            await _unregister_auto_preview_history(target_user_id, source_job_id)
            stored_message = pop_preview_message(preview_id)
            if stored_message:
                with contextlib.suppress(Exception):
                    await bot.edit_message_text(
                        f"⏹️ Auto preview cancelled: {reason}.",
                        chat_id=stored_message[0],
                        message_id=stored_message[1],
                    )


async def _scan_auto_preview_candidates(users: list[_WatcherUser]) -> int:
    """Find newly completed non-preview jobs and enqueue auto-preview generation."""
    from app.services.deadline import get_jobs_by_credentials

    auto_users = [user for user in users if user.auto_preview_enabled]
    if not auto_users:
        return 0

    await _maybe_cleanup_auto_preview_history()

    max_parallel = min(4, max(1, len(auto_users)))
    semaphore = asyncio.Semaphore(max_parallel)

    async def _scan_user(user: _WatcherUser) -> int:
        async with semaphore:
            try:
                jobs = await get_jobs_by_credentials(
                    user.login,
                    user.password,
                    use_cache=True,
                )
            except Exception as exc:
                logger.error(
                    "Watcher: failed to load jobs for auto-preview user %s: %s",
                    user.telegram_user_id,
                    exc,
                )
                return 0

            scheduled = 0
            live_previews = _sources_with_live_previews(jobs, user.telegram_user_id)
            for job in jobs:
                if not isinstance(job, dict):
                    continue

                job_id_raw = job.get("_id")
                job_id = str(job_id_raw).strip() if job_id_raw else ""
                if not job_id:
                    continue

                props = job.get("Props") or {}
                if not isinstance(props, dict):
                    props = {}

                if _is_preview_job(props):
                    continue

                if not job.get("OutDir"):
                    # Utility jobs (no render output) cannot be previewed;
                    # skip silently instead of messaging the user with an error.
                    continue

                job_stat = job.get("Stat", 0)
                if job_stat != 3:
                    if (
                        settings.preview_presubmit_enabled
                        and job_stat == 1
                        and _presubmit_ready(job, props)
                        and _job_matches_scope(user.auto_scope, user.login, props, job)
                    ):
                        if job_id in live_previews:
                            # This render already has a preview on the farm.
                            # Leave the record untouched: once that preview is
                            # done and gone, a run still waiting for one is
                            # picked up on a later pass.
                            continue
                        auto_key = (job_id, user.telegram_user_id)
                        claim = await _claim_new_run(user.telegram_user_id, job_id)
                        if claim == _CLAIM_KNOWN:
                            continue
                        if claim == _CLAIM_UNKNOWN and auto_key in auto_preview_jobs:
                            continue
                        auto_preview_jobs.add(auto_key)
                        job_name = str(props.get("Name") or "").split("/")[-1] or job_id
                        asyncio.create_task(
                            _run_auto_preview_presubmit(
                                user.telegram_user_id,
                                job_id,
                                job_name,
                                user.preview_worker,
                            )
                        )
                        scheduled += 1
                    continue

                if not _is_recently_completed(job, props):
                    continue

                if not _job_matches_scope(user.auto_scope, user.login, props, job):
                    continue

                auto_key = (job_id, user.telegram_user_id)
                claim = await _claim_completed_run(
                    user.telegram_user_id,
                    job_id,
                    str(job.get("DateComp") or props.get("DateComp") or ""),
                )
                if claim == _CLAIM_KNOWN:
                    continue
                if claim == _CLAIM_UNKNOWN and auto_key in auto_preview_jobs:
                    continue

                auto_preview_jobs.add(auto_key)
                job_name = str(props.get("Name") or "").split("/")[-1] or job_id
                asyncio.create_task(
                    _run_auto_preview_for_job(
                        user.telegram_user_id,
                        job_id,
                        job_name,
                        user.preview_worker,
                    )
                )
                scheduled += 1

            try:
                await _reconcile_presubmitted_previews(user, jobs)
            except Exception as exc:
                logger.warning(
                    "Watcher: preview reconcile failed for user %s: %s",
                    user.telegram_user_id,
                    exc,
                )

            return scheduled

    tasks = [asyncio.create_task(_scan_user(user)) for user in auto_users]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_scheduled = 0
    for user, result in zip(auto_users, results):
        if isinstance(result, Exception):
            logger.error(
                "Watcher: auto-preview scan failed for user %s: %s",
                user.telegram_user_id,
                result,
            )
            continue
        total_scheduled += int(result)

    return total_scheduled


def _may_probe(job: dict, props: dict, user: "_WatcherUser") -> bool:
    """Whether this account is allowed to hold back this job's tasks.

    Probing is a write on somebody's render - most of its tasks go to Suspended
    for a while - so it is opt-in per account:

        off   never probe (the account only reads the farm)
        own   probe the jobs this login submitted (the default)
        all   probe anyone's job, for an account with the Deadline rights to
              suspend foreign tasks

    "all" exists because the submitter is often not the one who can probe: they
    may not be logged into the bot, may submit under a different account name
    than their bot login, or may lack the rights to suspend their own tasks. In
    all those cases nobody probes the job and its ETA stays a guess for hours.
    """
    scope = _normalize_probe_scope(user.probe_scope)
    if scope == "off":
        return False
    return _job_matches_scope(scope, user.login, props, job)


def _probe_scan_order(users: list[_WatcherUser]) -> list[_WatcherUser]:
    """Owners first, farm-wide accounts second.

    Both may be eligible for the same job, and whoever gets there first claims
    it for the whole probe phase. The submitter is the better holder: their
    rights to their own tasks are not in question, and a release under their own
    login is what an artist watching Monitor expects to see.
    """
    eligible = [u for u in users if _normalize_probe_scope(u.probe_scope) != "off"]
    return sorted(eligible, key=lambda u: _normalize_probe_scope(u.probe_scope) == "all")


async def _scan_render_probes(users: list[_WatcherUser]) -> None:
    """Drive the probe phase of active renders, so the ETA has a cost curve.

    Cheap by design: it only touches Active render jobs in the account's probe
    scope, and only fetches a task list for those.
    """
    from app.services import probe_scheduler
    from app.services.deadline import get_job_tasks_by_user_id, get_jobs_by_credentials

    # Backstop first - this must run even if every user below fails.
    try:
        await probe_scheduler.release_stale_probes()
    except Exception as exc:
        logger.warning("Watcher: stale probe sweep failed: %s", exc)

    for user in _probe_scan_order(users):
        try:
            jobs = await get_jobs_by_credentials(user.login, user.password, use_cache=True)
        except Exception as exc:
            logger.warning(
                "Watcher: failed to load jobs for probing (user %s): %s",
                user.telegram_user_id,
                exc,
            )
            continue

        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("_id") or "").strip()
            if not job_id or job.get("Stat") != 1 or not job.get("OutDir"):
                continue
            props = job.get("Props") or {}
            if not isinstance(props, dict):
                props = {}
            if _is_preview_job(props) or not _may_probe(job, props, user):
                continue

            try:
                state = await probe_state.get_probe_state(job_id)
                if state is not None and state.released_at is not None:
                    continue
                tasks = await get_job_tasks_by_user_id(user.telegram_user_id, job_id)
                if not tasks:
                    continue
                if state is None:
                    await probe_scheduler.start_probing(
                        user.telegram_user_id, job_id, tasks
                    )
                else:
                    await probe_scheduler.release_if_ready(job_id, tasks)
            except Exception as exc:
                logger.warning("Watcher: probe handling failed for %s: %s", job_id, exc)


async def _filter_suspended_auth_users(users: list[_WatcherUser]) -> list[_WatcherUser]:
    """Drop users whose stored Deadline credentials are rejected (401 loop).

    The first time a login gets suspended, its user is told to /login again.
    """
    from app.services.deadline import is_auth_suspended, pop_auth_failure_notification
    from app.storage.user_settings import claim_auth_failure_notice

    active: list[_WatcherUser] = []
    for user in users:
        if not is_auth_suspended(user.login):
            active.append(user)
            continue
        # The in-memory flag fires once per process; the DB claim keeps restarts
        # from repeating the warning (at most one reminder per day).
        if pop_auth_failure_notification(user.login) and await claim_auth_failure_notice(
            user.telegram_user_id
        ):
            try:
                await bot.send_message(
                    user.telegram_user_id,
                    "⚠️ Deadline rejected your stored credentials (401 Unauthorized).\n"
                    "Farm monitoring for your account is paused. "
                    "Please /login again to resume.\n\n"
                    "You will not be reminded again until you log in.",
                )
            except Exception as exc:
                logger.warning(
                    "Could not notify user %s about invalid credentials: %s",
                    user.telegram_user_id,
                    exc,
                )
    return active


async def job_progress_watcher(bot) -> None:
    """Monitor active preview jobs and auto-preview candidates."""
    del bot  # Runtime preview helpers use shared bot instance from bot_core.

    next_interval = settings.job_watcher_interval_normal
    next_auto_scan_at = 0.0
    next_error_scan_at = 0.0
    next_probe_scan_at = 0.0
    try:
        while True:
            loop_started = time.monotonic()
            users = await _load_watcher_users()
            users = await _filter_suspended_auth_users(users)

            active_targets = await _collect_active_preview_targets(users)
            if active_targets:
                logger.debug(
                    "Watcher: processing %d active preview jobs",
                    len(active_targets),
                )
                await _process_active_previews_parallel(active_targets)

            auto_enabled_users = [user for user in users if user.auto_preview_enabled]
            scheduled_count = 0
            auto_scan_interval = min(
                settings.job_watcher_interval_normal,
                _AUTO_PREVIEW_SCAN_INTERVAL_SECONDS,
            )
            if auto_enabled_users and loop_started >= next_auto_scan_at:
                scheduled_count = await _scan_auto_preview_candidates(auto_enabled_users)
                next_auto_scan_at = loop_started + auto_scan_interval
                if scheduled_count:
                    logger.info(
                        "Watcher: scheduled %d auto-preview job(s)",
                        scheduled_count,
                    )
            elif not auto_enabled_users:
                next_auto_scan_at = 0.0

            if users and loop_started >= next_probe_scan_at:
                await _scan_render_probes(users)
                next_probe_scan_at = loop_started + _PROBE_SCAN_INTERVAL_SECONDS
            elif not users:
                next_probe_scan_at = 0.0

            notification_users = [user for user in users if user.notifications_enabled]
            if notification_users and loop_started >= next_error_scan_at:
                alerts_sent = await _scan_error_reports_candidates(notification_users)
                next_error_scan_at = loop_started + _ERROR_REPORT_SCAN_INTERVAL_SECONDS
                if alerts_sent:
                    logger.info("Watcher: sent %d report alert(s)", alerts_sent)
            elif not notification_users:
                next_error_scan_at = 0.0

            if active_targets:
                next_interval = settings.job_watcher_interval_preview
            else:
                next_interval = settings.job_watcher_interval_normal
                if auto_enabled_users:
                    next_interval = min(next_interval, auto_scan_interval)
                if notification_users:
                    next_interval = min(next_interval, _ERROR_REPORT_SCAN_INTERVAL_SECONDS)

            from app.core.farm_events import wait_for_wake

            if await wait_for_wake(next_interval):
                # A farm push event arrived: run every scan immediately instead
                # of waiting for the per-scan schedule to come around.
                logger.info("Watcher woken by farm event; scanning immediately")
                next_auto_scan_at = 0.0
                next_error_scan_at = 0.0
                next_probe_scan_at = 0.0
    except asyncio.CancelledError:
        logger.info("Job progress watcher cancelled")
