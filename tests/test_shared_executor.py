import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_shared_executor import SharedExecutor, RpcClient, RpcError, final_text, private_write

THREAD = '11111111-1111-4111-8111-111111111111'
REPLY = '22222222-2222-4222-8222-222222222222'
TURN = '33333333-3333-4333-8333-333333333333'
CLAIM = {'thread_id': THREAD, 'reply_id': REPLY, 'body': 'Who are you?'}
USER = {'type': 'userMessage', 'id': 'user', 'clientId': REPLY, 'content': []}
ANSWER = {'type': 'agentMessage', 'id': 'answer', 'text': 'Done.', 'phase': 'final_answer'}
DONE = {'id': TURN, 'status': 'completed', 'items': [USER, ANSWER]}


class FakeClient:
    def __init__(self, results=None, events=()):
        self.results = results or {}
        self.calls = []
        self.events = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def respond(self, ident, result):
        self.calls.append(('response', {'id': ident, 'result': result}))

    async def request(self, method, params):
        self.calls.append((method, params))
        result = self.results.get(method)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(params)
        if result is not None:
            return result
        if method in {'thread/read', 'thread/resume'}:
            return {'thread': {'id': THREAD, 'status': {'type': 'idle'}}}
        if method == 'thread/queue/list':
            return {'data': []}
        if method == 'thread/queue/add':
            return {'queuedSubmission': {'id': 'queue', 'clientUserMessageId': REPLY}}
        if method == 'thread/queue/start':
            return {'turn': {'id': TURN}}
        raise AssertionError(method)


def item_event(item, turn=TURN, thread=THREAD, method='item/completed'):
    return {'method': method, 'params': {'threadId': thread, 'turnId': turn, 'item': item}}


def completed_event(turn=DONE, thread=THREAD):
    return {'method': 'turn/completed', 'params': {'threadId': thread, 'turn': turn}}


class SharedExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def executor(self, client):
        return SharedExecutor('/unused', client_factory=lambda *args: client, completion_timeout=.2)

    def receipt(self, **values):
        private_write(self.run / 'shared-receipt.json', {
            'thread_id': THREAD, 'reply_id': REPLY, 'state': 'submitting', **values})

    def test_complete_preserves_task_settings_and_uses_native_queue(self):
        client = FakeClient(events=[item_event(USER), item_event(ANSWER), completed_event()])
        result = self.executor(client)(CLAIM, {}, self.run)
        self.assertEqual(result['body'], 'Done.')
        methods = [method for method, _ in client.calls]
        self.assertNotIn('turn/start', methods)
        self.assertNotIn('thread/start', methods)
        self.assertNotIn('thread/fork', methods)
        resume = next(params for method, params in client.calls if method == 'thread/resume')
        self.assertEqual(resume, {'threadId': THREAD, 'excludeTurns': True})
        self.assertEqual((self.run / 'shared-receipt.json').stat().st_mode & 0o777, 0o600)

    def test_busy_defers_before_any_submission(self):
        client = FakeClient({'thread/read': {'thread': {'id': THREAD, 'status': {'type': 'active'}}}})
        executor = self.executor(client)
        self.assertFalse(executor.availability(THREAD))
        self.assertEqual(executor.last_availability['code'], 'shared_task_busy')
        self.assertEqual(executor(CLAIM, {}, self.run)['status'], 'deferred')
        self.assertTrue(all(method == 'thread/read' for method, _ in client.calls))
        self.assertFalse((self.run / 'shared-receipt.json').exists())

    def test_native_busy_race_keeps_one_queued_submission(self):
        client = FakeClient({'thread/queue/start': RpcError({'code': -32600, 'message': 'thread already has an active or pending turn'})},
                            [item_event(USER), item_event(ANSWER), completed_event()])
        self.assertEqual(self.executor(client)(CLAIM, {}, self.run)['status'], 'completed')
        self.assertEqual(sum(method == 'thread/queue/add' for method, _ in client.calls), 1)

    def test_queue_auto_start_not_found_is_not_replayed(self):
        client = FakeClient({'thread/queue/start': RpcError({'code': -32600, 'message': 'queued submission not found: queue'})},
                            [item_event(USER), item_event(ANSWER), completed_event()])
        self.assertEqual(self.executor(client)(CLAIM, {}, self.run)['status'], 'completed')
        self.assertEqual(sum(method == 'thread/queue/add' for method, _ in client.calls), 1)

    def test_disconnect_after_queue_commit_recovers_read_only(self):
        client = FakeClient({'thread/queue/add': ConnectionError('lost response')})
        executor = self.executor(client)
        self.assertEqual(executor(CLAIM, {}, self.run)['status'], 'running')
        self.assertEqual(json.loads((self.run / 'shared-receipt.json').read_text())['state'], 'submitting')
        recovered = FakeClient({'thread/turns/list': {'data': [DONE]}})
        self.assertEqual(self.executor(recovered)(CLAIM, {}, self.run)['body'], 'Done.')
        self.assertEqual([method for method, _ in recovered.calls], ['thread/turns/list'])

    def test_recovery_active_and_queued_never_submits(self):
        self.receipt(turn_id=TURN)
        active = dict(DONE, status='inProgress')
        client = FakeClient({'thread/turns/list': {'data': [active]}})
        self.assertEqual(self.executor(client).recover(CLAIM, {}, self.run)['status'], 'running')
        self.receipt()
        client = FakeClient({'thread/turns/list': {'data': []}, 'thread/queue/list': {'data': [{'clientUserMessageId': REPLY}]}})
        self.assertEqual(self.executor(client).recover(CLAIM, {}, self.run)['code'], 'shared_submission_queued')
        self.assertTrue(all(method in {'thread/turns/list', 'thread/queue/list'} for method, _ in client.calls))

    def test_unknown_outcome_is_attention_and_never_resubmitted(self):
        self.receipt()
        client = FakeClient({'thread/turns/list': {'data': []}})
        self.assertEqual(self.executor(client)(CLAIM, {}, self.run)['code'], 'shared_execution_outcome_unknown')
        self.assertNotIn('thread/queue/add', [method for method, _ in client.calls])

    def test_approval_is_preserved_without_response(self):
        approval = {'id': 4, 'method': 'item/commandExecution/requestApproval',
                    'params': {'threadId': THREAD, 'turnId': TURN, 'command': 'example'}}
        client = FakeClient(events=[item_event(USER), approval])
        result = self.executor(client)(CLAIM, {}, self.run)
        self.assertEqual(result['code'], 'shared_client_request_requires_app')
        self.assertEqual(json.loads((self.run / 'shared-client-request.json').read_text()), approval)

    def test_other_thread_completion_and_commentary_cannot_supply_result(self):
        events = [item_event(USER), completed_event(thread='other'), item_event(dict(ANSWER, text='Progress', phase='commentary')),
                  completed_event(turn=dict(DONE, items=[USER]))]
        self.assertEqual(self.executor(FakeClient(events=events))(CLAIM, {}, self.run)['code'], 'shared_final_answer_missing_or_invalid')

    def test_history_must_match_both_reply_and_recorded_turn(self):
        self.receipt(turn_id=TURN)
        wrong = dict(DONE, items=[dict(USER, clientId='other'), ANSWER])
        client = FakeClient({'thread/turns/list': {'data': [wrong]}})
        self.assertEqual(self.executor(client).recover(CLAIM, {}, self.run)['code'], 'shared_history_reply_mismatch')

    def test_global_server_request_is_exposed_without_waiting(self):
        request = {'id': 9, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}}
        client = FakeClient(events=[request])
        result = self.executor(client)(CLAIM, {}, self.run)
        self.assertEqual(result['code'], 'shared_client_request_requires_app')
        self.assertEqual(json.loads((self.run / 'shared-client-request.json').read_text()), request)

    def test_clock_request_uses_local_seconds_without_approval(self):
        clock = {'id': 9, 'method': 'currentTime/read', 'params': {'threadId': THREAD}}
        client = FakeClient(events=[clock, item_event(USER), item_event(ANSWER), completed_event()])
        self.assertEqual(self.executor(client)(CLAIM, {}, self.run)['status'], 'completed')
        response = next(params for method, params in client.calls if method == 'response')
        self.assertIsInstance(response['result']['currentTimeAt'], int)

    def test_metadata_gate_requires_ready_and_live_process_identity(self):
        metadata = self.run / 'shared-app.json'
        client = FakeClient()
        executor = SharedExecutor('/unused', client_factory=lambda *args: client, metadata_path=metadata)
        self.assertFalse(executor.availability(THREAD))
        self.assertEqual(client.calls, [])
        data = {'status': 'ready', 'socket': '/unused'}
        for key in ['parent_app', 'supervisor', 'backend']:
            data[key + '_pid'] = 123
            data[key + '_start'] = 'birth-time'
        private_write(metadata, data)
        with patch('qq_shared_executor.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'birth-time\n'
            self.assertTrue(executor.ready())
            run.return_value.stdout = 'reused-pid-different-birth\n'
            self.assertFalse(executor.ready())
        private_write(metadata, dict(data, status='draining'))
        self.assertFalse(executor.ready())

    def test_draining_between_resume_and_submission_defers_without_receipt(self):
        client = FakeClient()
        executor = self.executor(client)
        with patch.object(executor, 'ready', side_effect=[True, False]):
            self.assertEqual(executor(CLAIM, {}, self.run)['code'], 'shared_app_not_ready')
        self.assertFalse((self.run / 'shared-receipt.json').exists())
        self.assertNotIn('thread/queue/add', [method for method, _ in client.calls])

    def test_final_prefers_terminal_message(self):
        self.assertEqual(final_text([dict(ANSWER, phase='commentary', text='working'), ANSWER]), 'Done.')
        self.assertEqual(final_text([dict(ANSWER, phase='commentary')]), '')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_request_id_cannot_resolve_client_response(self):
        class Socket:
            def __init__(self): self.messages = asyncio.Queue()
            def __aiter__(self): return self
            async def __anext__(self): return await self.messages.get()
            async def send(self, value):
                request = json.loads(value)
                await self.messages.put(json.dumps({'id': request['id'], 'method': 'approval', 'params': {}}))
                await self.messages.put(json.dumps({'id': request['id'], 'result': {'ok': True}}))
        client = RpcClient('/unused')
        client.ws = Socket()
        reader = asyncio.create_task(client._read())
        try:
            self.assertEqual(await client.request('test', {}), {'ok': True})
            self.assertEqual((await client.events.get())['method'], 'approval')
        finally:
            reader.cancel()
            try: await reader
            except asyncio.CancelledError: pass


class NativeSharedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.environ.get('QQ_SHARED_NATIVE_TESTS') != '1':
            self.skipTest('Set QQ_SHARED_NATIVE_TESTS=1 for isolated native backend checks')
        native_cli = os.environ.get('CODEX_QQ_MAIL_TEST_CLI', '')
        if not native_cli or not Path(native_cli).is_file() or not os.access(native_cli, os.X_OK):
            self.fail('Native tests require CODEX_QQ_MAIL_TEST_CLI to point to an executable Codex CLI')
        self.requests = []
        requests = self.requests
        owner = self
        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                if getattr(owner, 'delay', 0):
                    time.sleep(owner.delay)
                message = {'type': 'message', 'id': 'message' + str(len(requests)), 'role': 'assistant', 'status': 'completed',
                           'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'NATIVE_SHARED_OK', 'annotations': []}]}
                if getattr(owner, 'use_tool', False) and len(requests) == 2:
                    message = {'type': 'function_call', 'id': 'tool', 'call_id': 'call_probe', 'name': 'exec_command', 'arguments': json.dumps({'cmd': 'printf NATIVE_TOOL_OK', 'max_output_tokens': 100})}
                response = {'id': 'response', 'object': 'response', 'status': 'completed', 'model': 'gpt-probe', 'output': [message],
                            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                events = [{'type': 'response.created', 'response': dict(response, status='in_progress', output=[])},
                          {'type': 'response.output_item.added', 'output_index': 0, 'item': dict(message, status='in_progress', content=[])},
                          {'type': 'response.output_item.done', 'output_index': 0, 'item': message},
                          {'type': 'response.completed', 'response': response}]
                body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.temporary = tempfile.TemporaryDirectory(prefix='qq-executor-', dir='/tmp')
        self.root = Path(self.temporary.name)
        home = self.root / 'home'
        home.mkdir(mode=0o700)
        (home / 'config.toml').write_text(f'''model_provider="probe"\nmodel="gpt-probe"\n[model_providers.probe]\nname="fixture"\nbase_url="http://127.0.0.1:{self.server.server_port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\nrequest_max_retries=0\nstream_max_retries=0\n''')
        self.socket = self.root / 'shared.sock'
        env = {key: value for key, value in os.environ.items() if not key.startswith('CODEX_') and not any(word in key for word in ['TOKEN', 'AUTH', 'API_KEY'])}
        env['CODEX_HOME'] = str(home)
        self.backend = await asyncio.create_subprocess_exec(native_cli, 'app-server',
                         '--listen', 'unix://' + str(self.socket), env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(200):
            if self.socket.exists(): break
            await asyncio.sleep(.05)
        self.app = await RpcClient(self.socket).__aenter__()

    async def asyncTearDown(self):
        if hasattr(self, 'app'): await self.app.__aexit__()
        if hasattr(self, 'backend'):
            self.backend.terminate()
            await self.backend.wait()
        if hasattr(self, 'server'):
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
        if hasattr(self, 'temporary'): self.temporary.cleanup()

    async def app_completed(self, wanted=None):
        while True:
            event = await asyncio.wait_for(self.app.events.get(), 20)
            if event.get('method') == 'turn/completed' and (wanted is None or event['params']['turn']['id'] == wanted):
                return event

    async def test_busy_native_task_defers_without_new_model_request(self):
        self.delay = .5
        response = await self.app.request('thread/start', {'cwd': str(self.root), 'historyMode': 'paginated', 'approvalPolicy': 'never', 'sandbox': 'read-only'})
        thread_id = response['thread']['id']
        await self.app.request('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'STILL_WORKING'}]})
        for _ in range(100):
            if self.requests: break
            await asyncio.sleep(.01)
        executor = SharedExecutor(self.socket, completion_timeout=20)
        self.assertFalse(await asyncio.to_thread(executor.availability, thread_id))
        result = await asyncio.to_thread(executor, dict(CLAIM, thread_id=thread_id), {}, self.root / 'run')
        self.assertEqual(result['status'], 'deferred', result)
        self.assertEqual((await self.app.request('thread/queue/list', {'threadId': thread_id}))['data'], [])
        await self.app_completed()
        self.assertEqual(len(self.requests), 1)

    async def test_native_response_loss_recovers_without_resubmission(self):
        response = await self.app.request('thread/start', {'cwd': str(self.root), 'historyMode': 'paginated', 'approvalPolicy': 'never', 'sandbox': 'read-only'})
        thread_id = response['thread']['id']
        await self.app.request('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'ORIGINAL_APP_CONTEXT'}]})
        await self.app_completed()
        class LoseQueueResponse(RpcClient):
            async def request(self, method, params):
                result = await super().request(method, params)
                if method == 'thread/queue/add':
                    await self.ws.close()
                    raise ConnectionError('simulated lost queue response')
                return result
        run = self.root / 'run'
        claim = dict(CLAIM, thread_id=thread_id)
        executor = SharedExecutor(self.socket, client_factory=LoseQueueResponse, completion_timeout=10)
        result = await asyncio.to_thread(executor, claim, {}, run)
        self.assertEqual(result['status'], 'running', result)
        recovery = SharedExecutor(self.socket)
        for _ in range(30):
            result = await asyncio.to_thread(recovery.recover, claim, {}, run)
            if result['status'] == 'completed': break
            await asyncio.sleep(.1)
        self.assertEqual(result.get('body'), 'NATIVE_SHARED_OK', result)
        self.assertEqual(len(self.requests), 2)
        await self.app_completed(result['turn_id'])

    async def test_same_backend_app_connected_context_and_recovery(self):
        await self.run_shared_reply()

    async def test_native_shell_tool_executes_and_result_returns_to_app(self):
        self.use_tool = True
        await self.run_shared_reply()
        self.assertIn('NATIVE_TOOL_OK', json.dumps(self.requests[-1]))
        self.assertNotIn('Unknown function', json.dumps(self.requests[-1]))

    async def run_shared_reply(self):
        response = await self.app.request('thread/start', {'cwd': str(self.root), 'historyMode': 'paginated', 'approvalPolicy': 'never', 'sandbox': 'read-only'})
        thread_id = response['thread']['id']
        await self.app.request('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'ORIGINAL_APP_CONTEXT'}]})
        await self.app_completed()
        claim = dict(CLAIM, thread_id=thread_id)
        run = self.root / 'run'
        executor = SharedExecutor(self.socket, completion_timeout=20)
        result = await asyncio.to_thread(executor, claim, {}, run)
        self.assertEqual(result.get('body'), 'NATIVE_SHARED_OK', result)
        event = await self.app_completed(result['turn_id'])
        self.assertEqual(event['params']['threadId'], thread_id)
        self.assertIsNone(self.app.ws.close_code)
        expected_requests = 3 if getattr(self, 'use_tool', False) else 2
        self.assertEqual(len(self.requests), expected_requests)
        self.assertIn('ORIGINAL_APP_CONTEXT', json.dumps(self.requests[-1]))
        recovered = await asyncio.to_thread(executor.recover, claim, {}, run)
        self.assertEqual(recovered.get('body'), 'NATIVE_SHARED_OK', recovered)
        self.assertEqual(len(self.requests), expected_requests, 'Recovery must never call the model')


if __name__ == '__main__':
    unittest.main()
