import asyncio
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_app_bridge import (checked_metadata, configuration_key, is_app_backend_launch, is_server_launch,
                           server_arguments, write_private)


class AppBridgeTests(unittest.TestCase):
    def test_current_app_plugin_override_is_shared(self):
        key = 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true'
        actual = ['-c', 'features.code_mode_host=true', 'app-server',
                  '--analytics-default-enabled', '-c', key]
        self.assertTrue(is_app_backend_launch(actual))
        self.assertEqual(server_arguments(actual, Path('/tmp/shared.sock')),
                         actual + ['--listen', 'unix:///tmp/shared.sock'])
        for form in (['--config', key], ['--config=' + key], ['-c' + key]):
            self.assertTrue(is_app_backend_launch(['app-server', *form]))

    def test_tool_internal_app_servers_keep_native_transport(self):
        self.assertFalse(is_app_backend_launch(['app-server', '--listen', 'stdio://']))
        self.assertFalse(is_app_backend_launch(['-c', 'features.code_mode_host=true', 'app-server']))
        self.assertTrue(is_app_backend_launch(['app-server', '-c',
                                              'mcp_servers.codex_app={command="fixture"}']))

    def test_only_normal_server_launch_is_intercepted(self):
        self.assertTrue(is_server_launch(['-c', 'features.code_mode_host=true',
                                         'app-server', '--analytics-default-enabled',
                                         '-c', 'mcp_servers.codex_app={command="fixture"}']))
        for args in (['--version'], ['exec', 'resume', 'task'], ['app-server', 'daemon', 'version'],
                     ['app-server', 'proxy', '--sock', '/tmp/test'], ['app-server', '--help'],
                     ['app-server', 'generate-json-schema', '--out', '/tmp/test'],
                     ['app-server', '--listen']):
            self.assertFalse(is_server_launch(args), args)

    def test_preserves_native_app_configuration_exactly(self):
        original = ['-c', 'features.code_mode_host=true', 'app-server',
                    '--analytics-default-enabled', '-c',
                    'mcp_servers.codex_app={command="/Applications/App space/server"}',
                    '--listen', 'stdio://', '--enable', 'some_feature']
        expected = original[:6] + original[8:] + ['--listen', 'unix:///tmp/shared.sock']
        self.assertEqual(server_arguments(original, Path('/tmp/shared.sock')), expected)

    def test_transport_like_configuration_value_is_not_removed(self):
        self.assertEqual(server_arguments(['-c', '--listen', 'app-server', '--stdio'], Path('/tmp/x')),
                         ['-c', '--listen', 'app-server', '--listen', 'unix:///tmp/x'])

    def test_configuration_fingerprint_does_not_store_values(self):
        key = configuration_key(['app-server'], {'OPENAI_API_KEY': 'private-value'})
        self.assertEqual(len(key), 64)
        self.assertNotIn('private-value', key)
        self.assertNotEqual(key, configuration_key(['app-server'], {'OPENAI_API_KEY': 'other'}))

    def test_metadata_permissions_and_owner_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            metadata = {'supervisor_pid': 2, 'supervisor_start': 's', 'backend_pid': 3,
                        'backend_start': 'b', 'parent_app_pid': 1, 'parent_app_start': 'a',
                        'configuration_sha256': 'key', 'status': 'ready',
                        'socket': str(state / 'shared-app.sock')}
            write_private(state / 'shared-app.json', metadata)
            self.assertEqual((state / 'shared-app.json').stat().st_mode & 0o777, 0o600)
            with patch('qq_app_bridge.process_identity', side_effect=lambda pid: {2: 's', 3: 'b'}.get(pid)):
                self.assertEqual(checked_metadata(state, 1, 'a', 'key'), metadata)
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    checked_metadata(state, 1, 'a', 'changed')
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    checked_metadata(state, 4, 'different', 'key')
            with patch('qq_app_bridge.process_identity', return_value=None):
                self.assertIsNone(checked_metadata(state, 1, 'a', 'key'))

    def test_orphaned_live_backend_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            write_private(state / 'shared-app.json', {
                'supervisor_pid': 2, 'supervisor_start': 's', 'backend_pid': 3,
                'backend_start': 'b', 'status': 'ready'})
            with patch('qq_app_bridge.process_identity', side_effect=lambda pid: {3: 'b'}.get(pid)):
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    checked_metadata(state, 1, 'a', 'key')


class AppBridgeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_app_json_line_survives_stdio_and_websocket(self):
        from websockets.asyncio.server import unix_serve
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='qq-bridge-') as tmp:
            socket = Path(tmp) / 'test.sock'
            async def echo(ws):
                async for message in ws:
                    await ws.send(message)
            async with unix_serve(echo, str(socket), compression=None):
                child = await asyncio.create_subprocess_exec(
                    sys.executable, '-c',
                    'import asyncio,sys;from qq_app_bridge import bridge;asyncio.run(bridge(sys.argv[1]))',
                    str(socket), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=1024 * 1024,
                    cwd=bootstrap.SOURCE_DIR)
                try:
                    line = json.dumps({'id': 1, 'method': 'turn/start', 'text': 'x' * 200000}).encode() + b'\n'
                    child.stdin.write(line)
                    await child.stdin.drain()
                    received = await asyncio.wait_for(child.stdout.readline(), 10)
                    self.assertEqual(received, line)
                    child.stdin.close()
                    self.assertEqual(await asyncio.wait_for(child.wait(), 10), 0)
                finally:
                    if child.returncode is None:
                        child.terminate()
                        await child.wait()


if __name__ == '__main__':
    unittest.main()
