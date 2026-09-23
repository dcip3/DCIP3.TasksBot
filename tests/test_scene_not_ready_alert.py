"""Alerting on a scene the farm cannot open.

A render can start while its .hip is still syncing to the farm storage, and
then Houdini cannot load it. The first few of these are ordinary - the file
lands and the render carries on - so the alert only fires once a job has
collected enough of them to mean the file is not arriving at all.

Note what Deadline puts in the short report title: "Caught exception: The
attempted operation failed." The line that names the file only exists in the
full report contents, which the watcher fetches when nothing matched.
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

JOB_ID = "000000000000000000000001"
SCENE = "Y:/projects/proj_a/scenes/SHA_0110/SHA_0110_v018.hip"

SHORT_REPORT = {
    "_id": "000000000000000000000002",
    "Title": (
        "Error: Caught exception: The attempted operation failed.\r\n"
        "   at Deadline.Plugins.PluginWrapper.RenderTasks(Task task, "
        "String& outMessage, AbortLevel& abortLevel)"
    ),
    "Slave": "NodeA",
    "JobUser": "nodeb",
    "JobName": "SHA_0110_v018 - /obj/ropnet1/SHA_0081_main_v05",
    "Plugin": "Houdini",
    "Frames": "11-15",
    "Date": "2026-08-13T07:39:26.000+00:00",
    "LogErr": "",
}

FULL_REPORT = dict(
    SHORT_REPORT,
    ErrorContents=(
        "0: STDOUT: Input File: " + SCENE + "\n"
        "0: STDOUT: Error: Caught exception: The attempted operation failed.\n"
        "0: STDOUT: Unable to open file: " + SCENE + "\n"
        "0: STDOUT: hou.OperationFailed: The attempted operation failed.\n"
    ),
)


class SceneNotReadyMatcherTests(unittest.TestCase):
    def test_short_report_alone_matches_nothing(self) -> None:
        """The title never names the file, so the watcher must fetch the log."""
        self.assertIsNone(job_watcher._match_error_alert_rule(SHORT_REPORT))

    def test_full_report_matches(self) -> None:
        rule = job_watcher._match_error_alert_rule(FULL_REPORT)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "scene_not_ready")

    def test_a_local_c_path_belongs_to_the_other_rule(self) -> None:
        report = dict(
            SHORT_REPORT,
            ErrorContents="Unable to open file: C:/Users/artist/scene.hip",
        )
        rule = job_watcher._match_error_alert_rule(report)
        self.assertEqual(rule.key, "local_c_drive_open_error")

    def test_an_unrelated_failure_does_not_match(self) -> None:
        report = dict(
            SHORT_REPORT,
            ErrorContents="Error: Renderer returned non-zero error code, 150.",
        )
        self.assertIsNone(job_watcher._match_error_alert_rule(report))

    def test_the_scene_path_is_extracted(self) -> None:
        self.assertEqual(job_watcher._extract_report_path(FULL_REPORT), SCENE)

    def test_the_other_wording_matches_too(self) -> None:
        """Deadline says "Error loading:" when it gives up outside hipFile.load.

        Reports of the very same problem can carry that wording instead, and
        matching only "Unable to open file:" would miss every one of them.
        """
        other = "Y:/projects/proj_b/sandbox/SHB_city_main_v048.hip"
        report = dict(
            SHORT_REPORT,
            ErrorContents=(
                "0: STDOUT: Error: Caught exception: The attempted operation failed.\n"
                f"0: STDOUT: Error loading: {other}\n"
            ),
        )
        rule = job_watcher._match_error_alert_rule(report)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.key, "scene_not_ready")
        self.assertEqual(job_watcher._extract_report_path(report), other)

    def test_a_local_c_path_in_the_other_wording_is_still_the_c_rule(self) -> None:
        report = dict(
            SHORT_REPORT,
            ErrorContents="Error loading: C:/Users/artist/scene.hip",
        )
        rule = job_watcher._match_error_alert_rule(report)
        self.assertEqual(rule.key, "local_c_drive_open_error")


class SceneNotReadyThresholdTests(unittest.TestCase):
    def test_a_few_failures_are_business_as_usual(self) -> None:
        """The sync catching up mid-render must not page anybody."""
        rule = job_watcher._match_error_alert_rule(FULL_REPORT)
        self.assertGreaterEqual(rule.min_occurrences, 5)

    def test_the_other_rules_still_alert_on_the_first_report(self) -> None:
        for report in (
            {"Title": "Redshift activation error", "Slave": "NodeC"},
            {"Title": "Could not initialize the plugin sandbox", "Slave": "NodeC"},
            {"Title": "Unable to open file: C:/local/scene.hip", "Slave": "NodeC"},
        ):
            with self.subTest(title=report["Title"]):
                rule = job_watcher._match_error_alert_rule(report)
                self.assertEqual(rule.min_occurrences, 1)

    def test_all_failures_of_a_job_collapse_into_one_message(self) -> None:
        ids = {
            job_watcher._report_dedupe_id(
                JOB_ID,
                dict(FULL_REPORT, _id=f"report{i}", Slave="NodeC" if i % 2 else "NodeA"),
                "scene_not_ready",
            )
            for i in range(101)
        }
        self.assertEqual(len(ids), 1)

    def test_another_job_alerts_separately(self) -> None:
        self.assertNotEqual(
            job_watcher._report_dedupe_id(JOB_ID, FULL_REPORT, "scene_not_ready"),
            job_watcher._report_dedupe_id("otherjob", FULL_REPORT, "scene_not_ready"),
        )


class SceneNotReadyMessageTests(unittest.TestCase):
    def test_message_names_the_file_and_both_sides_to_check(self) -> None:
        rule = job_watcher._match_error_alert_rule(FULL_REPORT)
        text = job_watcher._build_error_alert_text(FULL_REPORT, rule, occurrences=37)

        self.assertIn(SCENE, text)
        self.assertIn("37", text)
        self.assertIn("NodeA", text)  # the machine that could not read it
        self.assertIn("SHA_0110_v018", text)
        self.assertIn("normal", text)  # says a few of these are expected
        self.assertIn("finished syncing to the farm storage", text)

    def test_reaches_the_submitter_and_the_machine_owner(self) -> None:
        identities = {"nodea": {100000001}, "nodeb": {100000002}}
        rule = job_watcher._match_error_alert_rule(FULL_REPORT)
        recipients = job_watcher._resolve_error_alert_recipient_ids(
            FULL_REPORT, rule.recipient_mode, identities
        )
        self.assertEqual(recipients, {100000001, 100000002})


if __name__ == "__main__":
    unittest.main()
