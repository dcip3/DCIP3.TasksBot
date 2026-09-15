"""Alerting on a worker whose Redshift has no licence.

The reports below are the two wordings NodeB produced for SHC_0170_ID_v022 on
2026-09-15: one licence-server timeout, then the key prompt on every task after
it. Each report used to be its own alert, so the job's owner got nine identical
messages in half an hour for one machine that needed signing in once.
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

JOB_ID = "000000000000000000000007"
STACK_FRAME = "   at Deadline.Plugins.PluginWrapper.RenderTasks(Task task, String& outMessage, AbortLevel& abortLevel)"

TIMEOUT_REPORT = {
    "_id": "000000000000000000000008",
    "Title": (
        'Dialog popup detected: Title "Redshift activation v3.2026.0627140608 error", '
        'Message "HTTP send failure: (12002) "\n' + STACK_FRAME
    ),
    "Slave": "NodeB",
    "JobUser": "nodea",
    "JobName": "SHC_0170_ID_v022 - /obj/ropnet1/SHC_0172_v002",
    "Plugin": "Houdini",
    "Task": "1",
    "Date": "2026-09-15T04:16:00.000+00:00",
    "LogErr": "",
}

KEY_PROMPT_REPORT = dict(
    TIMEOUT_REPORT,
    _id="000000000000000000000009",
    Title=(
        'Dialog popup detected: Title "Redshift activation v3.2026.0627140608", '
        'Message "Enter activation key"\n' + STACK_FRAME
    ),
)


class RedshiftActivationDeliveryTests(unittest.TestCase):
    def test_both_wordings_match_the_rule(self) -> None:
        for report in (TIMEOUT_REPORT, KEY_PROMPT_REPORT):
            rule = job_watcher._match_error_alert_rule(report)
            self.assertIsNotNone(rule)
            self.assertEqual(rule.key, "redshift_activation")

    def test_one_alert_per_machine_per_job(self) -> None:
        """Nine reports from NodeB on one job must be one message, not nine."""
        reports = [TIMEOUT_REPORT] + [
            dict(KEY_PROMPT_REPORT, _id=f"report{i}", Task=str(i)) for i in range(8)
        ]
        ids = {
            job_watcher._report_dedupe_id(JOB_ID, report, "redshift_activation")
            for report in reports
        }
        self.assertEqual(len(ids), 1)

    def test_a_second_machine_gets_its_own_alert(self) -> None:
        first = job_watcher._report_dedupe_id(JOB_ID, KEY_PROMPT_REPORT, "redshift_activation")
        second = job_watcher._report_dedupe_id(
            JOB_ID, dict(KEY_PROMPT_REPORT, Slave="NodeC"), "redshift_activation"
        )
        self.assertNotEqual(first, second)

    def test_another_job_on_the_same_machine_alerts_again(self) -> None:
        """Someone else's job hitting the same machine is news to that someone."""
        first = job_watcher._report_dedupe_id(JOB_ID, KEY_PROMPT_REPORT, "redshift_activation")
        second = job_watcher._report_dedupe_id("otherjob", KEY_PROMPT_REPORT, "redshift_activation")
        self.assertNotEqual(first, second)

    def test_message_explains_both_causes_and_drops_the_stack_frame(self) -> None:
        rule = job_watcher._match_error_alert_rule(TIMEOUT_REPORT)
        text = job_watcher._build_error_alert_text(TIMEOUT_REPORT, rule)
        self.assertIn("NodeB", text)
        self.assertIn("Resubmitting will not help", text)
        self.assertIn("12002", text)
        self.assertIn("Enter activation key", text)
        self.assertIn("Maxon App", text)
        self.assertNotIn("PluginWrapper.RenderTasks", text)
        self.assertIn("Critical", text)


class StackFrameStrippingTests(unittest.TestCase):
    def test_a_message_that_is_only_a_frame_is_kept(self) -> None:
        """Better an ugly line than an empty Message field."""
        report = {"Title": STACK_FRAME.strip(), "Slave": "NodeC"}
        rule = job_watcher._ErrorAlertRule(
            key="other",
            label="Other",
            matcher=lambda _report: True,
            recipient_mode="job_user",
            severity="warning",
        )
        text = job_watcher._build_error_alert_text(report, rule)
        self.assertIn("PluginWrapper.RenderTasks", text)


if __name__ == "__main__":
    unittest.main()
