# Contributor instructions

Use Conventional Commits: `type(scope): subject`. Working branches use `type/scope-subject`.
Follow the issue and pull request templates. Include relevant validation in pull requests.

Never commit mailbox addresses, credentials, auth files, local database contents, logs,
real message bodies, personal absolute paths, or production task identifiers.
Tests use synthetic fixtures and temporary state; do not contact a real mailbox or model.
Never weaken reply authentication, replay an uncertain execution, or approve native requests automatically.
Run `python3 -m unittest discover -s tests` and `python3 scripts/check_release.py` before a release.
