# Contributing

Use Python 3.11 or newer and an isolated virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/check_release.py
```

Routine tests use synthetic mail and temporary databases. The optional native integration
checks require a locally installed Codex executable; see the README. They never use your
mailbox or a real model. Do not run the installer against a working personal installation
to test a change; use its dry run and isolated installer tests.

Use `feat/mail-subject` or `fix/bridge-subject` branches and `type(scope): subject`
commit and pull request titles. Follow the pull request template, explain behavior changes,
and identify generated files or database changes. A compatibility change to Codex startup
arguments needs a regression test that also preserves unrelated CLI invocations.

Privacy checks apply to the entire Git history and release archive, not just the latest diff.
Keep reports of authorization or credential issues out of public issues; see SECURITY.md.
