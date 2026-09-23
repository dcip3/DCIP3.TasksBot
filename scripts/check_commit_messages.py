"""Validate English Conventional Commit messages using the standard library."""

import argparse
from pathlib import Path
import re
import subprocess


SUBJECT = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([a-z0-9][a-z0-9._/-]*\))?!?: [a-z][ -~]*$"
)
# GitHub's Revert button titles its pull request 'Revert "<subject>"'.
GITHUB_REVERT = re.compile(r'^Revert ".+"(?: \(#\d+\))?$')


def validate(message: str, comment: str | None = "#") -> list[str]:
    lines = []
    for line in message.splitlines():
        if comment is not None:
            # `git commit -v` puts the staged diff below a scissors line.
            if re.fullmatch(re.escape(comment) + r" -+ >8 -+", line):
                break
            if line.startswith(comment):
                continue
        lines.append(line)
    message = "\n".join(lines).strip()
    if not message:
        return ["Commit message is empty."]
    lines = message.splitlines()
    errors = []
    if not GITHUB_REVERT.fullmatch(lines[0]):
        if not SUBJECT.fullmatch(lines[0]):
            errors.append(
                "Use a Conventional Commit, e.g. fix: keep the preview caption short."
            )
        if len(lines[0]) > 72:
            errors.append("Keep the subject within 72 characters.")
    if any(ord(char) > 126 or (ord(char) < 32 and char not in "\n\t") for char in message):
        errors.append("Use English text and ASCII punctuation.")
    if len(lines) > 1 and lines[1].strip():
        errors.append("Separate the subject and body with a blank line.")
    return errors


def comment_char() -> str:
    configured = subprocess.run(
        ["git", "config", "core.commentChar"], capture_output=True, text=True
    ).stdout.strip()
    return configured if len(configured) == 1 else "#"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--rev-range", help="Git revision or range to validate")
    args = parser.parse_args()
    if args.file:
        messages = [(str(args.file), args.file.read_text(encoding="utf-8"))]
        comment = comment_char()
    else:
        # Merge commits carry GitHub's own subjects; the commits they merge are checked.
        revisions = subprocess.check_output(
            ["git", "rev-list", "--no-merges", args.rev_range], text=True
        ).splitlines()
        messages = [
            (revision[:12], subprocess.check_output(
                ["git", "show", "-s", "--format=%B", revision], encoding="utf-8"
            ))
            for revision in revisions
        ]
        comment = None
    failed = False
    for label, message in messages:
        for error in validate(message, comment):
            print(f"{label}: {error}")
            failed = True
    if not failed:
        print(f"Validated {len(messages)} commit message(s).")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
