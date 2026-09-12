"""Reply execution through the same public App-server as the desktop client.

Never launches a CLI writer, creates a task, steers an active turn, or approves a
server request. Native queued submissions provide the busy/start atomic guard.
"""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid


class RpcError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get('message', 'app_server_error'))


def private_write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


class RpcClient:
    def __init__(self, socket_path, timeout=20, audit=None):
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.audit = audit
        self.pending = {}
        self.events = asyncio.Queue()
        self.serial = 0
        self.ws = None
        self.reader = None

    async def __aenter__(self):
        from websockets.asyncio.client import unix_connect
        info = self.socket_path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('shared_socket_invalid_owner_or_type')
        self.ws = await unix_connect(str(self.socket_path), uri='ws://localhost/rpc',
                                     compression=None, open_timeout=self.timeout,
                                     max_size=16 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request('initialize', {
                'clientInfo': {'name': 'qq-mail-shared', 'version': '5.0.0'},
                'capabilities': {'experimentalApi': True},
            })
            await self.ws.send(json.dumps({'method': 'initialized'}))
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *args):
        if self.ws:
            await self.ws.close()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader

    def record(self, direction, message):
        if self.audit:
            self.audit({'time': time.time(), 'direction': direction, 'message': message})

    async def _read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                self.record('received', message)
                # Server request IDs and client response IDs occupy separate
                # namespaces. A method-bearing request must never resolve ours.
                if 'method' not in message and message.get('id') in self.pending:
                    future = self.pending.pop(message['id'])
                    if not future.done():
                        future.set_result(message)
                else:
                    await self.events.put(message)
        except Exception as exc:
            await self.events.put({'connection_error': type(exc).__name__})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('shared_connection_closed'))
            self.pending.clear()
            await self.events.put({'connection_error': 'shared_connection_closed'})

    async def respond(self, request_id, result):
        message = {'id': request_id, 'result': result}
        self.record('sent', message)
        await self.ws.send(json.dumps(message))

    async def request(self, method, params):
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        message = {'id': ident, 'method': method, 'params': params}
        self.record('sent', message)
        try:
            await self.ws.send(json.dumps(message))
            response = await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(ident, None)
        if 'error' in response:
            raise RpcError(response['error'])
        return response['result']


def user_message_matches(item, reply_id):
    if item.get('type') != 'userMessage':
        return False
    if item.get('clientId') == reply_id:
        return True
    marker = 'QQ_MAIL_REPLY_ID: ' + reply_id
    return any(part.get('type') == 'text' and marker in part.get('text', '')
               for part in item.get('content', []))


def final_text(items):
    messages = [item for item in items if item.get('type') == 'agentMessage'
                and isinstance(item.get('text'), str) and item['text'].strip()]
    finals = [item for item in messages if item.get('phase') == 'final_answer']
    unknown = [item for item in messages if item.get('phase') is None]
    chosen = finals or unknown[-1:]
    return '\n\n'.join(item['text'] for item in chosen)


class SharedExecutor:
    def __init__(self, socket_path, rpc_timeout=20, completion_timeout=86400,
                 client_factory=RpcClient, metadata_path=None):
        self.socket_path = Path(socket_path)
        self.rpc_timeout = rpc_timeout
        self.completion_timeout = completion_timeout
        self.client_factory = client_factory
        self.metadata_path = Path(metadata_path) if metadata_path else None
        self.last_availability = {'status': 'unknown', 'code': 'not_checked'}

    def ready(self):
        if self.metadata_path is None:
            return True
        try:
            metadata = json.loads(self.metadata_path.read_text())
            if metadata.get('status') != 'ready' or metadata.get('socket') != str(self.socket_path):
                return False
            for prefix in ['parent_app', 'supervisor', 'backend']:
                pid = metadata.get(prefix + '_pid')
                expected = metadata.get(prefix + '_start')
                if not isinstance(pid, int) or pid <= 0 or not isinstance(expected, str) or not expected:
                    return False
                identity = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                          text=True, capture_output=True, timeout=2)
                if identity.returncode != 0 or identity.stdout.strip() != expected:
                    return False
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def client(self, audit=None):
        return self.client_factory(self.socket_path, self.rpc_timeout, audit)

    def availability(self, thread_id):
        try:
            result = asyncio.run(self._availability(thread_id))
        except (OSError, ConnectionError, TimeoutError, ImportError) as exc:
            result = {'status': 'deferred', 'code': 'shared_server_unavailable',
                      'detail': type(exc).__name__}
        except Exception as exc:
            result = {'status': 'attention', 'code': 'shared_server_check_failed',
                      'detail': type(exc).__name__}
        self.last_availability = result
        return result.get('status') == 'available'

    async def _availability(self, thread_id):
        if not self.ready():
            return {'status': 'deferred', 'code': 'shared_app_not_ready'}
        async with self.client() as client:
            response = await client.request('thread/read', {'threadId': thread_id})
            thread = self.exact_thread(response, thread_id)
            state = thread.get('status', {}).get('type')
            if state not in {'idle', 'notLoaded'}:
                return {'status': 'deferred', 'code': 'shared_task_busy' if state == 'active' else 'shared_task_unavailable'}
            if thread.get('canAcceptDirectInput') is False:
                return {'status': 'attention', 'code': 'shared_task_rejects_direct_input'}
            return {'status': 'available', 'code': 'shared_task_idle'}

    @staticmethod
    def exact_thread(response, thread_id):
        thread = response.get('thread', {})
        if thread.get('id') != thread_id:
            raise ValueError('shared_thread_id_mismatch')
        return thread

    def __call__(self, claim, metadata, run_dir):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir.chmod(0o700)
        receipt_path = run_dir / 'shared-receipt.json'
        if receipt_path.exists():
            # A durable receipt means queue/add might already have executed.
            return self.recover(claim, metadata, run_dir)
        try:
            return asyncio.run(self._execute(claim, run_dir))
        except Exception as exc:
            if receipt_path.exists():
                return {'status': 'running', 'code': 'shared_execution_outcome_pending',
                        'detail': type(exc).__name__}
            return {'status': 'deferred', 'code': 'shared_server_unavailable',
                    'detail': type(exc).__name__}

    def audit(self, run_dir):
        path = Path(run_dir) / 'events.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        path.chmod(0o600)
        def append(record):
            with path.open('a') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return append

    @staticmethod
    def prompt(claim):
        return ('The following is a verified QQ email reply from the user for this exact existing task. '
                'Continue this task once, following applicable policies. '
                'The local mail worker owns delivery of the final answer to this same email conversation. '
                'Do not send a separate completion email for this invocation. '
                'Do not change the mail daemon or create a polling automation unless explicitly requested.\n'
                'QQ_MAIL_REPLY_ID: ' + claim['reply_id'] + '\n\n' + claim['body'])

    async def _execute(self, claim, run_dir):
        thread_id = str(uuid.UUID(claim['thread_id']))
        reply_id = str(uuid.UUID(claim['reply_id']))
        if not self.ready():
            return {'status': 'deferred', 'code': 'shared_app_not_ready'}
        async with self.client(self.audit(run_dir)) as client:
            before = self.exact_thread(await client.request('thread/read', {'threadId': thread_id}), thread_id)
            if before.get('status', {}).get('type') not in {'idle', 'notLoaded'}:
                return {'status': 'deferred', 'code': 'shared_task_busy'}
            resumed = self.exact_thread(await client.request('thread/resume', {
                'threadId': thread_id, 'excludeTurns': True}), thread_id)
            if resumed.get('status', {}).get('type') != 'idle':
                return {'status': 'deferred', 'code': 'shared_task_busy'}
            if resumed.get('canAcceptDirectInput') is False:
                return {'status': 'attention', 'code': 'shared_task_rejects_direct_input'}
            queue = await client.request('thread/queue/list', {'threadId': thread_id, 'limit': 100})
            if queue.get('data'):
                return {'status': 'deferred', 'code': 'shared_task_has_queued_input'}
            if not self.ready():
                return {'status': 'deferred', 'code': 'shared_app_not_ready'}
            receipt = {'version': 1, 'thread_id': thread_id, 'reply_id': reply_id,
                       'state': 'submitting', 'created_at': time.time()}
            # queue/add can auto-start when idle: persist BEFORE that RPC.
            private_write(run_dir / 'shared-receipt.json', receipt)
            added = await client.request('thread/queue/add', {
                'threadId': thread_id, 'clientUserMessageId': reply_id,
                'input': [{'type': 'text', 'text': self.prompt(claim)}],
            })
            queued = added.get('queuedSubmission', {})
            if queued.get('clientUserMessageId') != reply_id or not queued.get('id'):
                return {'status': 'attention', 'code': 'shared_queue_identity_mismatch'}
            receipt.update(state='queued', queued_submission_id=queued['id'])
            private_write(run_dir / 'shared-receipt.json', receipt)
            try:
                started = await client.request('thread/queue/start', {
                    'threadId': thread_id, 'queuedSubmissionId': queued['id']})
                turn_id = started.get('turn', {}).get('id')
                if not turn_id:
                    raise ValueError('shared_start_missing_turn_id')
                receipt.update(state='running', turn_id=turn_id)
                private_write(run_dir / 'shared-receipt.json', receipt)
            except RpcError as exc:
                message = exc.error.get('message', '')
                permitted = {'thread already has an active or pending turn',
                             'queued submission not found: ' + queued['id']}
                if exc.error.get('code') != -32600 or message not in permitted:
                    raise
                # Native busy guard leaves the queued reply for automatic drain.
                # Not-found means it may have already drained; neither is replayed.
            return await self.wait_for_result(client, claim, receipt, run_dir)

    async def wait_for_result(self, client, claim, receipt, run_dir):
        deadline = asyncio.get_running_loop().time() + self.completion_timeout
        items_by_turn = {}
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {'status': 'running', 'code': 'shared_completion_pending'}
            event = await asyncio.wait_for(client.events.get(), remaining)
            if event.get('connection_error'):
                return {'status': 'running', 'code': 'shared_connection_lost'}
            params = event.get('params', {})
            if 'method' in event and 'id' in event:
                if event['method'] == 'currentTime/read' and params.get('threadId') == claim['thread_id']:
                    await client.respond(event['id'], {'currentTimeAt': int(time.time())})
                    continue
                private_write(run_dir / 'shared-client-request.json', event)
                receipt.update(state='client_request', request_method=event['method'])
                private_write(run_dir / 'shared-receipt.json', receipt)
                # No automatic approval, tool invocation, or permission override.
                return {'status': 'attention', 'code': 'shared_client_request_requires_app'}
            if params.get('threadId') != claim['thread_id']:
                continue
            method = event.get('method')
            if method in {'item/started', 'item/completed'}:
                turn_id = params.get('turnId')
                item = params.get('item', {})
                if user_message_matches(item, claim['reply_id']):
                    if receipt.get('turn_id') and receipt['turn_id'] != turn_id:
                        return {'status': 'attention', 'code': 'shared_reply_turn_mismatch'}
                    receipt.update(state='running', turn_id=turn_id, user_message_verified=True)
                    private_write(run_dir / 'shared-receipt.json', receipt)
                if method == 'item/completed' and item.get('type') == 'agentMessage':
                    items_by_turn.setdefault(turn_id, {})[item.get('id')] = item
            if method == 'turn/completed' and params.get('turn', {}).get('id') == receipt.get('turn_id'):
                turn = params['turn']
                items = {item.get('id'): item for item in turn.get('items', [])}
                items.update(items_by_turn.get(turn['id'], {}))
                if not receipt.get('user_message_verified'):
                    return await self._recover(client, claim, receipt, run_dir)
                return self.finish(turn, list(items.values()), receipt, run_dir)

    def finish(self, turn, items, receipt, run_dir):
        if turn.get('status') == 'inProgress':
            return {'status': 'running', 'code': 'shared_turn_running'}
        if turn.get('status') != 'completed':
            receipt.update(state='failed', turn_status=turn.get('status'), error=turn.get('error'))
            private_write(run_dir / 'shared-receipt.json', receipt)
            return {'status': 'attention', 'code': 'shared_turn_' + str(turn.get('status', 'unknown'))}
        body = final_text(items)
        if not body.strip() or len(body) > 1_000_000:
            return {'status': 'attention', 'code': 'shared_final_answer_missing_or_invalid'}
        receipt.update(state='completed', completed_at=time.time())
        private_write(run_dir / 'shared-receipt.json', receipt)
        return {'status': 'completed', 'body': body, 'turn_id': turn['id']}

    def recover(self, claim, metadata, run_dir):
        try:
            receipt = json.loads((Path(run_dir) / 'shared-receipt.json').read_text())
            if receipt.get('thread_id') != claim['thread_id'] or receipt.get('reply_id') != claim['reply_id']:
                return {'status': 'attention', 'code': 'shared_receipt_identity_mismatch'}
            return asyncio.run(self._recover_connection(claim, receipt, Path(run_dir)))
        except FileNotFoundError:
            return {'status': 'attention', 'code': 'shared_receipt_missing'}
        except (OSError, ConnectionError, TimeoutError):
            return {'status': 'running', 'code': 'shared_recovery_unavailable'}
        except Exception as exc:
            return {'status': 'attention', 'code': 'shared_recovery_failed', 'detail': type(exc).__name__}

    async def _recover_connection(self, claim, receipt, run_dir):
        async with self.client(self.audit(run_dir)) as client:
            return await self._recover(client, claim, receipt, run_dir)

    async def _recover(self, client, claim, receipt, run_dir):
        cursor = None
        for _ in range(20):
            params = {'threadId': claim['thread_id'], 'limit': 100, 'itemsView': 'full'}
            if cursor:
                params['cursor'] = cursor
            response = await client.request('thread/turns/list', params)
            for turn in response.get('data', []):
                matched = any(user_message_matches(item, claim['reply_id']) for item in turn.get('items', []))
                if turn.get('id') == receipt.get('turn_id') or matched:
                    if not matched:
                        return {'status': 'attention', 'code': 'shared_history_reply_mismatch'}
                    if receipt.get('turn_id') and receipt['turn_id'] != turn['id']:
                        return {'status': 'attention', 'code': 'shared_history_turn_mismatch'}
                    receipt.update(turn_id=turn['id'], user_message_verified=True)
                    private_write(run_dir / 'shared-receipt.json', receipt)
                    return self.finish(turn, turn.get('items', []), receipt, run_dir)
            cursor = response.get('nextCursor')
            if not cursor:
                break
        queued = await client.request('thread/queue/list', {'threadId': claim['thread_id'], 'limit': 100})
        if any(item.get('clientUserMessageId') == claim['reply_id'] for item in queued.get('data', [])):
            return {'status': 'running', 'code': 'shared_submission_queued'}
        return {'status': 'attention', 'code': 'shared_execution_outcome_unknown'}
