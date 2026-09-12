#!/usr/bin/env python3
"""Install a fresh, per-user macOS listener. Use --dry-run before applying."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import venv

REPO = Path(__file__).resolve().parents[1]
LISTENER_LABEL = 'io.github.codex-qq-mail.listener'
ENV_LABEL = 'io.github.codex-qq-mail.app-env'
STATE_ENV = 'CODEX_QQ_MAIL_STATE_DIR'
RECEIPT = '.codex-qq-mail-install.json'
KEYCHAIN_SERVICE = 'codex-qq-smtp-auth-code'


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True,
                          capture_output=True, text=True, **kwargs)


def account_value(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9._+-]+@qq\.com', value):
        raise argparse.ArgumentTypeError('Use the QQ login account, for example you@qq.com')
    return value


def native_cli(value=None):
    candidates = [Path(value).expanduser()] if value else [
        Path('/Applications/Codex.app/Contents/Resources/codex'),
        Path('/Applications/ChatGPT.app/Contents/Resources/codex'),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise ValueError('Pass --codex-cli with the native executable bundled in your Codex App')


def build_plan(args, home=None):
    home = home or Path.home()
    install_root = (args.install_root or home / '.agents/skills/qq-mail').expanduser().absolute()
    state_dir = (args.state_dir or Path(os.environ.get(
        STATE_ENV, str(home / 'Library/Application Support/Codex QQ Mail')))).expanduser().absolute()
    launcher = home / '.local/bin/codex-qq-shared'
    launch_agents = home / 'Library/LaunchAgents'
    return {
        'account': args.account, 'cli_path': str(native_cli(args.codex_cli)),
        'install_root': str(install_root), 'state_dir': str(state_dir),
        'launcher': str(launcher), 'python': str(state_dir / 'runtime/bin/python'),
        'plists': [str(launch_agents / (label + '.plist'))
                   for label in (LISTENER_LABEL, ENV_LABEL)],
        'register_mcp': args.register_mcp, 'app_restart_required': True,
        'task_bindings': [],
    }


def file_conflicts(plan):
    targets = [plan['install_root'], plan['state_dir'], plan['launcher'], *plan['plists']]
    return [str(path) for target in targets if (path := Path(target)).exists() or path.is_symlink()]


def launcher_text(plan):
    # shlex.quote protects custom paths with spaces, quotes and shell metacharacters.
    return ('#!/bin/sh\nexport ' + STATE_ENV + '=' + shlex.quote(plan['state_dir']) +
            '\nexec ' + shlex.quote(plan['python']) + ' ' +
            shlex.quote(str(Path(plan['install_root']) / 'scripts/qq_app_bridge.py')) + ' "$@"\n')


def plist_documents(plan):
    state = Path(plan['state_dir'])
    root = Path(plan['install_root'])
    return [
        {'Label': LISTENER_LABEL,
         'ProgramArguments': [plan['python'], str(root / 'scripts/qq_mail_daemon.py'), 'run'],
         'EnvironmentVariables': {STATE_ENV: str(state)},
         'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30, 'ExitTimeOut': 60,
         'StandardOutPath': str(state / 'listener.stdout.log'),
         'StandardErrorPath': str(state / 'listener.stderr.log')},
        {'Label': ENV_LABEL,
         'ProgramArguments': ['/bin/launchctl', 'setenv', 'CODEX_CLI_PATH', plan['launcher']],
         'RunAtLoad': True},
    ]


def private_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)


def preflight(plan):
    if sys.platform != 'darwin':
        raise ValueError('Installation requires macOS; --dry-run is available elsewhere')
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    conflicts = file_conflicts(plan)
    if conflicts:
        raise ValueError('Existing paths are protected; use a fresh installation or back up and uninstall first: ' + ', '.join(conflicts))
    for key in ('CODEX_CLI_PATH', 'CODEX_APP_SERVER_WS_URL', 'CODEX_APP_SERVER_USE_LOCAL_DAEMON'):
        if run(['/bin/launchctl', 'getenv', key]).stdout.strip() or os.environ.get(key):
            raise ValueError('Existing ' + key + ' must be reconciled before installation')
    for key, expected in (('CODEX_QQ_MAIL_ACCOUNT', plan['account']),
                          ('CODEX_QQ_MAIL_CODEX_PATH', plan['cli_path'])):
        if os.environ.get(key) and os.environ[key] != expected:
            raise ValueError('Conflicting ' + key + ' must be reconciled before installation')
    run([plan['cli_path'], '--version'])
    if plan['register_mcp']:
        found = subprocess.run([plan['cli_path'], 'mcp', 'get', 'qq-mail', '--json'],
                               capture_output=True, text=True)
        if found.returncode == 0:
            raise ValueError('An existing qq-mail MCP registration is protected')
        # Parse the current list too: a broken config must not look like an absent entry.
        entries = json.loads(run([plan['cli_path'], 'mcp', 'list', '--json']).stdout)
        if any(entry.get('name') == 'qq-mail' for entry in entries):
            raise ValueError('An existing qq-mail MCP registration is protected')


def stop_agent(label):
    target = 'gui/' + str(os.getuid()) + '/' + label
    if subprocess.run(['/bin/launchctl', 'print', target], capture_output=True).returncode:
        return
    run(['/bin/launchctl', 'bootout', target])
    for _ in range(60):
        if subprocess.run(['/bin/launchctl', 'print', target], capture_output=True).returncode:
            return
        time.sleep(1)
    raise RuntimeError('Listener did not stop; files were preserved')


def install(plan):
    preflight(plan)
    os.umask(0o077)
    root, state = Path(plan['install_root']), Path(plan['state_dir'])
    created = []
    started = []
    mcp_added = False
    environment_set = False
    try:
        for path in (state, root):
            path.mkdir(parents=True, mode=0o700)
            created.append(path)
        (root / 'scripts').mkdir(mode=0o700)
        for source in sorted((REPO / 'scripts').glob('qq_*.py')):
            private_write(root / 'scripts' / source.name, source.read_bytes(), 0o700)
        private_write(root / 'SKILL.md', (REPO / 'skills/qq-mail/SKILL.md').read_bytes())
        private_write(state / 'config.json', json.dumps({
            'account': plan['account'], 'cli_path': plan['cli_path'],
            'keychain_service': KEYCHAIN_SERVICE}, indent=2) + '\n')
        private_write(state / 'daemon-config.json', '{"thread_ids": []}\n')
        venv.EnvBuilder(with_pip=True).create(state / 'runtime')
        run([plan['python'], '-m', 'pip', 'install', '-r', REPO / 'requirements.txt'])
        run([plan['python'], '-c', 'import websockets; assert websockets.__version__ == "16.0"'])
        run([plan['python'], '-c', 'from qq_mail_config import require_account; require_account()'],
            cwd=str(root / 'scripts'), env={**os.environ, STATE_ENV: str(state)})
        launcher = Path(plan['launcher'])
        private_write(launcher, launcher_text(plan), 0o700)
        created.append(launcher)
        run([launcher, '--version'])
        for filename, document in zip(plan['plists'], plist_documents(plan)):
            path = Path(filename)
            private_write(path, plistlib.dumps(document))
            created.append(path)
            run(['/usr/bin/plutil', '-lint', path])
        if plan['register_mcp']:
            run([plan['cli_path'], 'mcp', 'add', 'qq-mail', '--env', STATE_ENV + '=' + str(state),
                 '--', plan['python'], str(root / 'scripts/qq_mail_mcp.py')])
            mcp_added = True
        # Set the GUI environment only after all local validation has passed.
        for filename, label in zip(plan['plists'], (LISTENER_LABEL, ENV_LABEL)):
            run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), filename])
            started.append(label)
        run(['/bin/launchctl', 'setenv', 'CODEX_CLI_PATH', launcher])
        environment_set = True
        receipt = {**plan, 'format': 1, 'mcp_registered': mcp_added,
                   'launcher_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest(),
                   'plist_sha256': {filename: hashlib.sha256(Path(filename).read_bytes()).hexdigest()
                                    for filename in plan['plists']}}
        private_write(root / RECEIPT, json.dumps(receipt, indent=2) + '\n')
    except Exception:
        for label in reversed(started):
            stop_agent(label)
        current = run(['/bin/launchctl', 'getenv', 'CODEX_CLI_PATH']).stdout.strip()
        if current == plan['launcher'] and (environment_set or ENV_LABEL in started):
            run(['/bin/launchctl', 'unsetenv', 'CODEX_CLI_PATH'])
        if mcp_added:
            run([plan['cli_path'], 'mcp', 'remove', 'qq-mail'])
        for path in reversed(created):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        raise


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--account', required=True, type=account_value)
    value.add_argument('--codex-cli', help='Native CLI bundled inside Codex.app or ChatGPT.app')
    value.add_argument('--install-root', type=Path)
    value.add_argument('--state-dir', type=Path)
    value.add_argument('--register-mcp', action='store_true', help='Add the qq-mail stdio MCP server to Codex config')
    value.add_argument('--dry-run', action='store_true', help='Print the plan without writes, network, launchd, or model calls')
    return value


def main():
    args = parser().parse_args()
    try:
        plan = build_plan(args)
        if args.dry_run:
            print(json.dumps({'dry_run': True, **plan, 'existing_path_conflicts': file_conflicts(plan)}, indent=2))
        else:
            install(plan)
            print(json.dumps({'installed': True, 'app_restarted': False,
                              'next_step': 'Restart Codex App once, configure Keychain, then opt in an existing task',
                              'install_root': plan['install_root']}, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Installation stopped: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
