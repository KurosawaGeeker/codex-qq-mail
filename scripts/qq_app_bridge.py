#!/usr/bin/env python3
"""CODEX_CLI_PATH adapter: preserve App launch options on a shared native server.

Only the transport of a normal `app-server` launch changes. This program never
talks to the App's private IPC pipe; native Codex owns all configured MCP tools.
"""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket as socket_module
import subprocess
import sys
import time

from qq_mail_config import STATE_DIR, discover_codex

NATIVE = discover_codex()
STATE = STATE_DIR
VALUE_OPTIONS = {'-c', '--config', '--enable', '--disable', '--code-mode-host',
                 '--listen', '--ws-auth', '--ws-token-file', '--ws-token-sha256',
                 '--ws-shared-secret-file', '--ws-issuer', '--ws-audience',
                 '--ws-max-clock-skew-seconds'}


def is_server_launch(args):
    if any(arg in {'-h', '--help', '-V', '--version'} for arg in args):
        return False
    positional = []
    consume = False
    for arg in args:
        if consume:
            consume = False
        elif arg in VALUE_OPTIONS:
            consume = True
        elif not arg.startswith('-'):
            positional.append(arg)
    return positional == ['app-server'] and not consume


def is_app_backend_launch(args):
    # The App also passes CODEX_CLI_PATH to node_repl and computer-use runtimes.
    # Their independent app-server children must retain their ordinary transport.
    # App releases have used both a full server definition and a plugin-enable
    # override. Keep their arguments untouched while recognizing both forms.
    app_keys = {'mcp_servers.codex_app',
                'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled'}
    overrides = []
    for index, arg in enumerate(args):
        if arg in {'-c', '--config'} and index + 1 < len(args):
            overrides.append(args[index + 1])
        elif arg.startswith('--config='):
            overrides.append(arg[len('--config='):])
        elif arg.startswith('-c') and arg != '-c':
            overrides.append(arg[2:])
    has_app_override = any(value.partition('=')[0].strip() in app_keys for value in overrides)
    return is_server_launch(args) and has_app_override


def server_arguments(args, socket):
    result = []
    consume = False
    preserve_next = False
    for arg in args:
        if consume:
            consume = False
        elif preserve_next:
            result.append(arg)
            preserve_next = False
        elif arg == '--listen':
            consume = True
        elif arg == '--stdio' or arg.startswith('--listen='):
            continue
        else:
            result.append(arg)
            preserve_next = arg in VALUE_OPTIONS
    return [*result, '--listen', 'unix://' + str(socket)]


def process_identity(pid):
    try:
        # Start time prevents a recycled PID from inheriting ownership.
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def write_private(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.write('\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def configuration_key(args, env):
    # Hash options and relevant inherited configuration, never serialize secrets.
    relevant = {key: value for key, value in env.items()
                if key.startswith(('CODEX_', 'OPENAI_')) or key in {'PATH', 'HOME'}}
    return hashlib.sha256(json.dumps([args, relevant, os.getcwd()], sort_keys=True).encode()).hexdigest()


def checked_metadata(state, parent_pid, parent_start, key):
    path = state / 'shared-app.json'
    if not path.exists():
        return None
    metadata = json.loads(path.read_text())
    if not metadata.get('supervisor_start') or not metadata.get('backend_start'):
        raise RuntimeError('shared_backend_identity_missing')
    alive = process_identity(metadata['supervisor_pid']) == metadata.get('supervisor_start')
    backend_alive = process_identity(metadata['backend_pid']) == metadata.get('backend_start')
    if not alive and not backend_alive:
        return None
    if (not alive or not backend_alive or metadata.get('status') != 'ready'
            or metadata.get('parent_app_pid') != parent_pid
            or metadata.get('parent_app_start') != parent_start
            or metadata.get('configuration_sha256') != key):
        raise RuntimeError('shared_backend_owner_or_configuration_mismatch')
    if metadata.get('socket') != str(state / 'shared-app.sock'):
        raise RuntimeError('shared_backend_socket_mismatch')
    return metadata


def ensure_backend(args, state=STATE, native=NATIVE):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    parent_pid = os.getppid()
    parent_start = process_identity(parent_pid)
    if parent_start is None:
        raise RuntimeError('app_parent_unavailable')
    key = configuration_key(args, os.environ)
    with (state / 'shared-app-start.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        for attempt in range(7):
            try:
                existing = checked_metadata(state, parent_pid, parent_start, key)
                break
            except RuntimeError:
                metadata_path = state / 'shared-app.json'
                previous = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
                previous_parent = previous.get('parent_app_pid')
                old_parent_gone = (previous_parent is not None and previous_parent != parent_pid
                                   and process_identity(previous_parent) != previous.get('parent_app_start'))
                if not old_parent_gone or attempt == 6:
                    raise
                time.sleep(1)
        if existing is not None:
            return existing
        socket = state / 'shared-app.sock'
        if socket.exists():
            # A stale path is removable only when our previous owner is gone.
            metadata_path = state / 'shared-app.json'
            if not metadata_path.exists():
                raise RuntimeError('unowned_shared_backend_socket')
            with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as probe:
                probe.settimeout(2)
                if probe.connect_ex(str(socket)) == 0:
                    raise RuntimeError('unowned_shared_backend_listener')
            socket.unlink()
        log_fd = os.open(state / 'shared-app-supervisor.log',
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(log_fd, 'ab') as log:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--bridge-supervise'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                start_new_session=True, env=os.environ.copy())
        request = {'args': args, 'state': str(state), 'native': str(native),
                   'parent_app_pid': parent_pid, 'parent_app_start': parent_start,
                   'configuration_sha256': key}
        child.stdin.write(json.dumps(request).encode() + b'\n')
        child.stdin.close()
        if not select.select([child.stdout], [], [], 20)[0]:
            raise RuntimeError('shared_backend_start_timeout')
        response = json.loads(child.stdout.readline())
        child.stdout.close()
        if response.get('status') != 'ready':
            raise RuntimeError('shared_backend_start_failed')
        return response


async def server_is_idle(socket):
    from websockets.asyncio.client import unix_connect
    async with unix_connect(str(socket), uri='ws://localhost/rpc', compression=None,
                            open_timeout=3, close_timeout=2, max_size=32 * 1024 * 1024) as ws:
        sequence = 0
        async def request(method, params):
            nonlocal sequence
            sequence += 1
            await ws.send(json.dumps({'id': sequence, 'method': method, 'params': params}))
            while True:
                result = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if 'method' not in result and result.get('id') == sequence:
                    if 'error' in result:
                        raise RuntimeError('shared_backend_status_unavailable')
                    return result['result']
        await request('initialize', {'clientInfo': {'name': 'qq-app-lifecycle', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
        await ws.send('{"method":"initialized"}')
        cursor = None
        while True:
            loaded = await request('thread/loaded/list', {'cursor': cursor, 'limit': 100})
            for thread_id in loaded.get('data', []):
                thread = (await request('thread/read', {'threadId': thread_id, 'includeTurns': False}))['thread']
                if thread.get('status', {}).get('type') not in {'idle', 'notLoaded'}:
                    return False
                queued = await request('thread/queue/list', {'threadId': thread_id, 'limit': 1})
                if queued.get('data') or queued.get('nextCursor'):
                    return False
            cursor = loaded.get('nextCursor')
            if cursor is None:
                break
        return True


def supervise(request):
    state = Path(request['state'])
    socket = state / 'shared-app.sock'
    log_fd = os.open(state / 'shared-app-native.log',
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(log_fd, 'ab') as log:
        native = subprocess.Popen([request['native'], *server_arguments(request['args'], socket)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=log, env=os.environ.copy())
    metadata = {key: request[key] for key in ('parent_app_pid', 'parent_app_start',
                                              'configuration_sha256')}
    metadata.update({'version': 1, 'supervisor_pid': os.getpid(),
                     'supervisor_start': process_identity(os.getpid()),
                     'backend_pid': native.pid, 'backend_start': process_identity(native.pid),
                     'socket': str(socket), 'created_at': time.time(), 'status': 'starting'})
    write_private(state / 'shared-app.json', metadata)
    for _ in range(300):
        if native.poll() is not None:
            metadata['status'] = 'failed'
            break
        if socket.exists():
            socket.chmod(0o600)
            metadata['status'] = 'ready'
            break
        time.sleep(0.05)
    if metadata['status'] == 'starting':
        metadata['status'] = 'attention_start_timeout'
    write_private(state / 'shared-app.json', metadata)
    print(json.dumps(metadata), flush=True)
    sys.stdout.close()
    # No model calls: process liveness while App runs; read-only RPC while draining.
    while native.poll() is None:
        if process_identity(request['parent_app_pid']) != request['parent_app_start']:
            metadata['status'] = 'draining'
            write_private(state / 'shared-app.json', metadata)
            # The mail daemon holds this lock through claim, execution, and delivery.
            # It must observe status=ready before starting a new turn.
            with (state / 'dispatch.lock').open('a') as dispatch:
                try:
                    fcntl.flock(dispatch, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(2)
                    continue
                try:
                    idle = asyncio.run(server_is_idle(socket))
                except Exception:
                    idle = False
                if idle:
                    native.terminate()
                    try:
                        native.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        # Never force-kill an execution when its outcome is uncertain.
                        metadata['status'] = 'attention_shutdown_timeout'
                        write_private(state / 'shared-app.json', metadata)
                        return
                    break
        time.sleep(2)
    metadata['status'] = 'stopped'
    metadata['exit_code'] = native.poll()
    write_private(state / 'shared-app.json', metadata)


async def bridge(socket):
    from websockets.asyncio.client import unix_connect
    reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        async with unix_connect(str(socket), uri='ws://localhost/rpc', compression=None,
                                open_timeout=10, close_timeout=3,
                                max_size=32 * 1024 * 1024) as ws:
            async def to_server():
                while line := await reader.readline():
                    await ws.send(line.decode().rstrip('\r\n'))
            async def to_app():
                async for message in ws:
                    data = message.encode() if isinstance(message, str) else message
                    sys.stdout.buffer.write(data + b'\n')
                    sys.stdout.buffer.flush()
            pending = {asyncio.create_task(to_server()), asyncio.create_task(to_app())}
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
    finally:
        transport.close()


def main():
    args = sys.argv[1:]
    if args == ['--bridge-supervise']:
        supervise(json.loads(sys.stdin.readline()))
        return
    if not is_app_backend_launch(args):
        os.execv(str(NATIVE), [str(NATIVE), *args])
    metadata = ensure_backend(args)
    asyncio.run(bridge(metadata['socket']))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # CLI overrides can contain credentials, so never echo their values.
        print('QQ App bridge failed: ' + type(error).__name__ + ': ' +
              (str(error) if isinstance(error, RuntimeError) else 'see private service logs'),
              file=sys.stderr)
        sys.exit(1)
