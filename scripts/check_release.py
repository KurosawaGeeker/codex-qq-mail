#!/usr/bin/env python3
"""Reject accidentally tracked runtime data and obvious private material.

This is a small release guard, not a replacement for full Git-history secret
scanning and a human review. Findings print paths and rule names, never values.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {'.eml', '.mbox', '.log', '.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.pfx', '.zip'}
FORBIDDEN_NAMES = {'auth.json', 'config.json', 'account-aliases.json', 'account-binding.json',
                   'daemon-status.json', 'shared-app.json', 'shared-receipt.json', '.env'}
PATTERNS = {
    'private_key': re.compile(r'-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'provider_api_key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}\b'),
    'personal_home_path': re.compile(r'/(?:Users|home)/[A-Za-z][A-Za-z0-9_.-]{1,63}/'),
    'numeric_qq_address': re.compile(r'\b[1-9][0-9]{4,12}@qq\.com\b'),
}


def release_files():
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True)
    if tracked.returncode == 0 and tracked.stdout:
        return [ROOT / name.decode() for name in tracked.stdout.split(b'\0') if name]
    excluded = {'.git', '.venv', '__pycache__', '.pytest_cache'}
    return [p for p in ROOT.rglob('*') if p.is_file() and not excluded.intersection(p.relative_to(ROOT).parts)]


def main():
    problems = []
    files = release_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            problems.append((relative, 'symlink_review_required'))
            continue
        if (path.suffix in FORBIDDEN_SUFFIXES or path.name in FORBIDDEN_NAMES
                or '.sqlite' in path.name or path.name.startswith('.env.')
                or {'work', 'outputs', 'runs', 'runtime'}.intersection(relative.parts)):
            problems.append((relative, 'runtime_or_private_file'))
        try:
            content = path.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            problems.append((relative, 'non_text_release_file'))
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(content):
                problems.append((relative, name))
    for path, rule in problems:
        print(f'{path}: {rule}', file=sys.stderr)
    print(f'Reviewed {len(files)} release files; {len(problems)} findings.')
    return bool(problems)


if __name__ == '__main__':
    sys.exit(main())
