"""Alerting on a worker that cannot start its plugin sandbox.

The reports below have the shape Deadline returns when that happens: every
task that lands on the machine fails, while the rest of the farm renders the
same job without trouble. The list endpoint gives no log body at all - the
title is everything the matcher gets - which is why this is tested against that
shape rather than against the full error text.
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

from app.services import job_watcher

JOB_ID = "000000000000000000000003"

# The usual wording; the log body arrives empty from the list endpoint.
SHORT_REPORT = {
    "_id": "000000000000000000000004",
    "Title": "Failed to load the plugin because: Could not initialize the plugin sandbox",
    "Slave": "NodeC",
    "JobUser": "nodeb",
    "JobName": "SHA_0070_DS_v019 - /obj/ropnet1/SHA_0070_v06",
    "Plugin": "Houdini",
    "Frames": "45-49",
    "Task": "9",
    "Date": "2026-08-16T15:53:08.329+00:00",
    "LogErr": "",
}

# Now and then: same machine, same cause, different wording.
SANDBOX_EXIT_REPORT = dict(
    SHORT_REPORT,
    _id="000000000000000000000005",
    Title="Sandbox process exited unexpectedly while waiting for response from Plugin.",
)

# What the full report carries once the watcher fetches the contents.
FULL_REPORT = dict(
    SHORT_REPORT,
    _id="000000000000000000000006",
    ErrorContents=(
        "0: Loading Job's Plugin timeout is Disabled\n"
        "0: SandboxedPlugin: Render Job As User disabled, running as current user 'NodeC'\n"
        "0: System.Net.Sockets.SocketException (10013): An attempt was made to access "
        "a socket in a way forbidden by its access permissions. [::1]:29293\n"
        "0:    at Deadline.Slaves.CommandListener..ctor(Int32 commandPort, Boolean isRunningAsService)"
    ),
)


class SandboxMatcherTests(unittest.TestCase):
    def test_matches_the_short_report(self) -> None:
        """No log body is available at match time, so the title has to carry it."""
        rule = job_watcher._match_error_alert_rule(SHORT_REPORT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "plugin_sandbox_error")

    def test_matches_the_sandbox_exit_wording(self) -> None:
        rule = job_watcher._match_error_alert_rule(SANDBOX_EXIT_REPORT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "plugin_sandbox_error")

    def test_matches_the_full_report_too(self) -> None:
        rule = job_watcher._match_error_alert_rule(FULL_REPORT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "plugin_sandbox_error")

    def test_is_critical(self) -> None:
        """The machine keeps taking tasks and failing them until someone acts."""
        rule = job_watcher._match_error_alert_rule(SHORT_REPORT)
        self.assertEqual(rule.severity, job_watcher._ERROR_ALERT_SEVERITY_CRITICAL)

    def test_leaves_unrelated_reports_alone(self) -> None:
        for title in (
            "Error: Redshift cannot find the scene file",
            "Houdini: unable to open file: Y:/shots/scene.hip",
            "Job timed out",
        ):
            with self.subTest(title=title):
                report = dict(SHORT_REPORT, Title=title)
                rule = job_watcher._match_error_alert_rule(report)
                self.assertNotEqual(
                    getattr(rule, "key", None), "plugin_sandbox_error"
                )

    def test_a_c_drive_report_still_matches_its_own_rule(self) -> None:
        """The new rule must not shadow the existing ones."""
        report = {
            "_id": "x",
            "Title": "Unable to open file: C:/local/scene.hip",
            "Slave": "NodeC",
            "JobUser": "nodeb",
        }
        rule = job_watcher._match_error_alert_rule(report)
        self.assertEqual(rule.key, "local_c_drive_open_error")


class SandboxAlertDeliveryTests(unittest.TestCase):
    def test_one_alert_per_machine_per_job(self) -> None:
        """A machine failing every task must not send a message per report."""
        ids = {
            job_watcher._report_dedupe_id(
                JOB_ID, dict(SHORT_REPORT, _id=f"report{i}", Task=str(i)), "plugin_sandbox_error"
            )
            for i in range(100)
        }
        self.assertEqual(len(ids), 1)

    def test_a_second_machine_gets_its_own_alert(self) -> None:
        first = job_watcher._report_dedupe_id(JOB_ID, SHORT_REPORT, "plugin_sandbox_error")
        second = job_watcher._report_dedupe_id(
            JOB_ID, dict(SHORT_REPORT, Slave="NodeD"), "plugin_sandbox_error"
        )
        self.assertNotEqual(first, second)

    def test_another_job_on_the_same_machine_alerts_again(self) -> None:
        first = job_watcher._report_dedupe_id(JOB_ID, SHORT_REPORT, "plugin_sandbox_error")
        second = job_watcher._report_dedupe_id("otherjob", SHORT_REPORT, "plugin_sandbox_error")
        self.assertNotEqual(first, second)

    def test_reaches_both_the_machine_owner_and_the_job_owner(self) -> None:
        """The machine is also a bot login here, so whoever can fix it hears."""
        identities = {"nodec": {100000003}, "nodeb": {100000002}}
        rule = job_watcher._match_error_alert_rule(SHORT_REPORT)
        recipients = job_watcher._resolve_error_alert_recipient_ids(
            SHORT_REPORT, rule.recipient_mode, identities
        )
        self.assertEqual(recipients, {100000003, 100000002})

    def test_message_names_the_worker_and_says_not_to_resubmit(self) -> None:
        rule = job_watcher._match_error_alert_rule(SHORT_REPORT)
        text = job_watcher._build_error_alert_text(SHORT_REPORT, rule)
        self.assertIn("NodeC", text)
        self.assertIn("SHA_0070_DS_v019", text)
        self.assertIn("Resubmitting will not help", text)
        self.assertIn("excludedportrange", text)
        self.assertIn("Critical", text)


if __name__ == "__main__":
    unittest.main()
