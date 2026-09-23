# Contributing

Use Python 3.13 (see `.python-version`) and install `requirements.txt` and
`requirements-dev.txt` in a virtual environment.
Install [Gitleaks](https://github.com/gitleaks/gitleaks#installing) and enable hooks:

```bash
git config --local core.hooksPath .githooks
```

If Gitleaks is not on PATH, set its executable with
`git config --local gitleaks.path /absolute/path/to/gitleaks`.
The pre-commit hook scans staged changes for secrets with the rules in
`.gitleaks.toml`. The commit-msg hook checks the message format. CI also scans
the complete Git history.

## Commit messages

Use English [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
with ASCII punctuation, a lowercase description, and a subject of at most 72
characters. Separate an optional body from the subject with a blank line.

```text
feat: add a refresh button to the workers list
fix: keep the preview caption within the Telegram limit
docs: explain the worker setup
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`. An optional scope and breaking-change `!` are supported.
Credit actual human contributions accurately; generated tool signatures are not
required. Pull requests are squash-merged, so give them a title in the same
format; GitHub's own `Revert "..."` titles are accepted too.

## Local checks

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
python -m unittest discover -s tests -v
python -m compileall -q app main.py scripts tests
python -m ruff check --select F401,F811,F821,F822,F823,F841 app main.py scripts tests
python scripts/check_commit_messages.py --rev-range HEAD
git diff --check 4b825dc642cb6eb9a060e54bf8d69288fbee4904 HEAD
gitleaks git --log-opts="--all --full-history" --redact
python -m pip_audit -r requirements.txt
```

The tests run offline: they use temporary databases and never contact Telegram
or Deadline. With `ffmpeg` and `ffprobe` on PATH, the video tests run too;
otherwise they are skipped.

Keep `.env`, bot tokens, Deadline credentials, databases, and real farm data
(job, machine and user names, storage paths) out of commits and issue reports.
No license has been selected yet; public visibility alone does not grant
permission to reuse the project.
