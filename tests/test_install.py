import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install.py'
spec = importlib.util.spec_from_file_location('mail_install', SCRIPT)
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)
with patch.dict(sys.modules, {'install': install}):
    uninstall_spec = importlib.util.spec_from_file_location('mail_uninstall', SCRIPT.with_name('uninstall.py'))
    uninstall = importlib.util.module_from_spec(uninstall_spec)
    uninstall_spec.loader.exec_module(uninstall)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.home = self.base / 'home with spaces'
        self.cli = self.base / 'native-codex'
        self.cli.write_text('#!/bin/sh\nexit 0\n')
        self.cli.chmod(0o700)
        self.args = argparse.Namespace(account='you@qq.com', codex_cli=str(self.cli),
                                       install_root=None, state_dir=None, register_mcp=False)

    def tearDown(self):
        self.directory.cleanup()

    def plan(self):
        with patch.dict(os.environ, {}, clear=True):
            return install.build_plan(self.args, self.home)

    def test_account_rejects_header_injection_and_other_domains(self):
        for value in ['you@qq.com\nBcc:other@example.com', 'you@example.com', '', 'a b@qq.com']:
            with self.assertRaises(argparse.ArgumentTypeError):
                install.account_value(value)
        self.assertEqual(install.account_value('YOU@qq.com'), 'you@qq.com')

    def test_plan_is_read_only_and_does_not_enable_tasks(self):
        plan = self.plan()
        self.assertFalse(self.home.exists())
        self.assertEqual(plan['task_bindings'], [])
        self.assertTrue(plan['app_restart_required'])
        self.assertEqual(install.file_conflicts(plan), [])

    def test_existing_installation_is_reported_and_untouched(self):
        plan = self.plan()
        root = Path(plan['install_root'])
        root.mkdir(parents=True)
        marker = root / 'existing'
        marker.write_text('keep')
        self.assertIn(str(root), install.file_conflicts(plan))
        with patch.object(install.sys, 'platform', 'darwin'), self.assertRaises(ValueError):
            install.preflight(plan)
        self.assertEqual(marker.read_text(), 'keep')

    def test_dangling_symlink_is_a_conflict(self):
        plan = self.plan()
        root = Path(plan['install_root'])
        root.parent.mkdir(parents=True)
        root.symlink_to(self.base / 'missing')
        self.assertIn(str(root), install.file_conflicts(plan))

    def test_launcher_quotes_paths_and_passes_arguments(self):
        plan = self.plan()
        output = self.base / 'capture.json'
        executable = self.base / "python fake '; $(touch NEVER_RUN)"
        executable.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                              'open(os.environ["CAPTURE"],"w").write(json.dumps([sys.argv[1:],os.environ["CODEX_QQ_MAIL_STATE_DIR"]]))\n')
        executable.chmod(0o700)
        plan['python'] = str(executable)
        plan['state_dir'] = str(self.base / "state with ' quotes;$unused")
        launcher = self.base / 'launcher'
        launcher.write_text(install.launcher_text(plan))
        launcher.chmod(0o700)
        subprocess.run([str(launcher), 'app-server', '-c', 'name="a b"'], check=True,
                       env={**os.environ, 'CAPTURE': str(output)}, cwd=self.base)
        captured = json.loads(output.read_text())
        self.assertEqual(captured[0], [str(Path(plan['install_root']) / 'scripts/qq_app_bridge.py'),
                                       'app-server', '-c', 'name="a b"'])
        self.assertEqual(captured[1], plan['state_dir'])
        self.assertFalse((self.base / 'NEVER_RUN').exists())

    def test_plists_preserve_spaced_paths_as_single_arguments(self):
        plan = self.plan()
        listener, environment = [plistlib.loads(plistlib.dumps(doc))
                                 for doc in install.plist_documents(plan)]
        self.assertEqual(listener['ProgramArguments'][0], plan['python'])
        self.assertEqual(listener['EnvironmentVariables'], {install.STATE_ENV: plan['state_dir']})
        self.assertEqual(environment['ProgramArguments'][-1], plan['launcher'])

    def test_existing_launch_environment_fails_before_writes(self):
        plan = self.plan()
        result = subprocess.CompletedProcess([], 0, stdout='/existing/custom/cli\n', stderr='')
        with patch.object(install.sys, 'platform', 'darwin'), patch.object(install, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'CODEX_CLI_PATH'):
                install.preflight(plan)
        self.assertFalse(self.home.exists())

    def test_private_write_never_overwrites(self):
        path = self.base / 'private'
        install.private_write(path, 'first')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            install.private_write(path, 'second')
        self.assertEqual(path.read_text(), 'first')

    def test_install_download_failure_rolls_back_created_directories(self):
        plan = self.plan()
        def fail_download(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ['pip'])
        def mocked_run(args, **kwargs):
            if 'getenv' in args:
                return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
            return fail_download()
        with patch.object(install, 'preflight'), patch.object(install.venv.EnvBuilder, 'create'), \
                patch.object(install, 'run', side_effect=mocked_run):
            with self.assertRaises(subprocess.CalledProcessError):
                install.install(plan)
        self.assertFalse(Path(plan['install_root']).exists())
        self.assertFalse(Path(plan['state_dir']).exists())

    def test_install_and_uninstall_preserve_state_with_mocked_system_services(self):
        plan = self.plan()
        calls = []
        def mocked_run(args, **kwargs):
            calls.append([str(arg) for arg in args])
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        with patch.object(install, 'preflight'), patch.object(install.venv.EnvBuilder, 'create'), \
                patch.object(install, 'run', side_effect=mocked_run):
            install.install(plan)
        root = Path(plan['install_root'])
        state = Path(plan['state_dir'])
        receipt = json.loads((root / install.RECEIPT).read_text())
        self.assertEqual(json.loads((state / 'daemon-config.json').read_text()), {'thread_ids': []})
        self.assertEqual(receipt['account'], 'you@qq.com')
        self.assertFalse(receipt['mcp_registered'])
        self.assertTrue(any('require_account()' in arg for call in calls for arg in call))
        self.assertTrue((root / 'scripts/qq_mail_config.py').is_file())
        self.assertTrue((root / 'SKILL.md').is_file())
        with patch.object(uninstall.sys, 'argv', ['uninstall.py', '--install-root', str(root)]), \
                patch.object(uninstall.sys, 'platform', 'darwin'), \
                patch.object(uninstall, 'run', side_effect=mocked_run), \
                patch.object(uninstall, 'stop_agent'), redirect_stdout(io.StringIO()):
            self.assertEqual(uninstall.main(), 0)
        self.assertTrue((state / 'config.json').exists())
        self.assertFalse(root.exists())
        self.assertFalse(Path(plan['launcher']).exists())

    def test_uninstall_refuses_modified_launcher_before_stopping_services(self):
        plan = self.plan()
        root = Path(plan['install_root'])
        root.mkdir(parents=True)
        launcher = Path(plan['launcher'])
        launcher.parent.mkdir(parents=True)
        launcher.write_text('changed')
        (root / install.RECEIPT).write_text(json.dumps({**plan, 'format': 1, 'launcher_sha256': 'old',
                                                       'mcp_registered': False}))
        with patch.object(uninstall.sys, 'argv', ['uninstall.py', '--install-root', str(root)]), \
                patch.object(uninstall.sys, 'platform', 'darwin'), \
                patch.object(uninstall, 'stop_agent') as stop, redirect_stderr(io.StringIO()):
            self.assertEqual(uninstall.main(), 1)
            stop.assert_not_called()
        self.assertEqual(launcher.read_text(), 'changed')

    def test_uninstall_preserves_draining_backend_after_app_exits(self):
        plan = self.plan()
        root = Path(plan['install_root'])
        state = Path(plan['state_dir'])
        root.mkdir(parents=True)
        state.mkdir(parents=True)
        (root / install.RECEIPT).write_text(json.dumps({**plan, 'format': 1, 'mcp_registered': False}))
        (state / 'shared-app.json').write_text(json.dumps({
            'parent_app_pid': 10001, 'parent_app_start': 'old app start',
            'supervisor_pid': 10002, 'supervisor_start': 'old supervisor start',
            'backend_pid': 10003, 'backend_start': 'live backend start'}))
        def process_result(args, **kwargs):
            return subprocess.CompletedProcess(args, 0,
                stdout='live backend start\n' if args[2] == '10003' else '', stderr='')
        with patch.object(uninstall.sys, 'argv', ['uninstall.py', '--install-root', str(root)]), \
                patch.object(uninstall.sys, 'platform', 'darwin'), \
                patch.object(uninstall.subprocess, 'run', side_effect=process_result), \
                patch.object(uninstall, 'stop_agent') as stop, redirect_stderr(io.StringIO()):
            self.assertEqual(uninstall.main(), 1)
            stop.assert_not_called()
        self.assertTrue(root.exists())


if __name__ == '__main__':
    unittest.main()
