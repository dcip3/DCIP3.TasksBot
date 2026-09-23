import unittest

from scripts.check_commit_messages import validate


class CommitMessageTests(unittest.TestCase):
    def test_accepts_conventional_messages(self):
        for message in (
            "fix: keep the preview caption short",
            "feat(preview)!: require an upload URL\n\nBREAKING CHANGE: setup changed.",
            "docs: explain worker setup\n\nAdd the ffmpeg note.\n# Git comment",
            'Revert "fix: keep the preview caption short"',
            'Revert "feat: add a long enough subject to pass the usual limit" (#12)',
        ):
            with self.subTest(message=message):
                self.assertEqual(validate(message), [])

    def test_rejects_invalid_messages(self):
        for message in (
            "", "# Only a comment", "Update files", "fix: update dates — again",
            "fix: " + "a" * 68, "fix: update dates\nMissing blank line",
        ):
            with self.subTest(message=message):
                self.assertTrue(validate(message))

    def test_ignores_the_diff_below_the_scissors_line(self):
        message = (
            "fix: keep the preview caption short\n\n"
            "# ------------------------ >8 ------------------------\n"
            "+    text = \"✅ Preview ready\"\n"
        )
        self.assertEqual(validate(message), [])

    def test_honours_a_custom_comment_character(self):
        message = "fix: keep the preview caption short\n; Please enter the message\n"
        self.assertEqual(validate(message, ";"), [])

    def test_stored_messages_keep_lines_that_start_with_hash(self):
        self.assertEqual(validate("fix: count tasks\n\n#12 was off by one.", None), [])


if __name__ == "__main__":
    unittest.main()
