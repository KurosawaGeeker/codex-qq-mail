#!/usr/bin/env python3
"""Remove this installer-owned integration, preserving mail state and Keychain."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from install import ENV_LABEL, LISTENER_LABEL, RECEIPT, run, stop_agent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', type=Path, default=Path.home() / '.agents/skills/qq-mail')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        root = args.install_root.expanduser().absolute()
        receipt = json.loads((root / RECEIPT).read_text())
        if receipt.get('format') != 1 or receipt.get('install_root') != str(root):
            raise ValueError('Installation receipt does not match this directory')
        if args.dry_run:
            print(json.dumps({'dry_run': True, 'remove': [str(root), receipt['launcher'], *receipt['plists']],
                              'preserve': [receipt['state_dir'], 'macOS Keychain credential'],
                              'remove_mcp': receipt['mcp_registered']}, indent=2))
            return 0
        if sys.platform != 'darwin':
            raise ValueError('Uninstallation requires macOS')
        metadata = Path(receipt['state_dir']) / 'shared-app.json'
        if metadata.exists():
            record = json.loads(metadata.read_text())
            for prefix in ('parent_app', 'supervisor', 'backend'):
                if not record.get(prefix + '_pid'):
                    continue
                running = subprocess.run(['/bin/ps', '-p', str(record[prefix + '_pid']), '-o', 'lstart='],
                                         capture_output=True, text=True)
                observed_start = running.stdout.strip()
                if observed_start and observed_start == record.get(prefix + '_start'):
                    raise ValueError('Quit Codex App and let its shared backend finish before uninstalling')
        launcher = Path(receipt['launcher'])
        if launcher.exists() and hashlib.sha256(launcher.read_bytes()).hexdigest() != receipt['launcher_sha256']:
            raise ValueError('Launcher was changed after installation; it will not be removed')
        for filename in receipt['plists']:
            path = Path(filename)
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('plist_sha256', {}).get(filename):
                raise ValueError('LaunchAgent was changed after installation; it will not be removed')
        if receipt['mcp_registered']:
            result = subprocess.run([receipt['cli_path'], 'mcp', 'get', 'qq-mail', '--json'],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                entry = json.loads(result.stdout)
                transport = entry.get('transport', entry)
                expected = str(root / 'scripts/qq_mail_mcp.py')
                if transport.get('command') != receipt['python'] or transport.get('args') != [expected]:
                    raise ValueError('qq-mail MCP registration changed; it will not be removed')
        stop_agent(LISTENER_LABEL)
        stop_agent(ENV_LABEL)
        if run(['/bin/launchctl', 'getenv', 'CODEX_CLI_PATH']).stdout.strip() == str(launcher):
            run(['/bin/launchctl', 'unsetenv', 'CODEX_CLI_PATH'])
        if receipt['mcp_registered']:
            run([receipt['cli_path'], 'mcp', 'remove', 'qq-mail'])
        for filename in [*receipt['plists'], str(launcher)]:
            Path(filename).unlink(missing_ok=True)
        shutil.rmtree(root)
        print(json.dumps({'uninstalled': True, 'preserved_state': receipt['state_dir'],
                          'keychain_preserved': True}, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Uninstallation stopped: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
