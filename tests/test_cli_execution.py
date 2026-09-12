"""Exercise the real argv/stdin/file boundary with a local, non-model fake CLI."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_mail_daemon import MailDaemon


TASK_ID = "01234567-89ab-4cde-8fab-0123456789ab"
OTHER_TASK_ID = "01234567-89ab-4cde-8fab-0123456789ac"
REPLY_ID = "11234567-89ab-4cde-8fab-0123456789ab"


class CliExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "workspace with spaces"
        self.cwd.mkdir()
        self.run_dir = self.root / "private run"
        self.run_dir.mkdir(mode=0o700)
        self.cli = self.root / "fake-codex"
        self.daemon = MailDaemon(
            state_dir=self.root / "state",
            codex_home=self.root / "codex home",
            cli_path=self.cli,
            reply_service=object(),
        )
        self.claim = {
            "thread_id": TASK_ID,
            "reply_id": REPLY_ID,
            "claim_token": "opaque-test-claim",
            "body": "请只回答测试完成。",
        }
        self.metadata = {"cwd": str(self.cwd)}

    def fake_cli(self, mode):
        source = "#!" + str(Path(sys.executable).resolve()) + "\n" + '''
import json
import os
from pathlib import Path
import sys

mode = MODE
args = sys.argv[1:]
result_file = Path(args[args.index('--output-last-message') + 1])
task_id = args[-2]
lock_error = f'Error: thread/resume: thread/resume failed: thread {task_id} already has an active writer (code -32600)'
capture = {
    'argv': args,
    'stdin': sys.stdin.read(),
    'cwd': os.getcwd(),
    'codex_home': os.environ.get('CODEX_HOME'),
    'thread_env': os.environ.get('CODEX_THREAD_ID'),
    'mail_owner': os.environ.get('QQ_MAIL_DELIVERY_OWNER'),
}
(result_file.parent / 'capture.json').write_text(json.dumps(capture))
if mode in ('lock', 'loose_lock_text', 'nonlock'):
    sys.stderr.write(lock_error if mode == 'lock' else 'already has an active writer' if mode == 'loose_lock_text' else 'authentication unavailable')
    sys.exit(1)
if mode == 'lock_after_turn_event':
    print(json.dumps({'type': 'turn.started'}))
    sys.stderr.write(lock_error)
    sys.exit(1)
if mode not in ('missing_started', 'lock_after_turn_event'):
    if mode == 'mixed_wrong_then_exact':
        print(json.dumps({'type': 'thread.started', 'thread_id': OTHER_TASK_ID}))
    print(json.dumps({'type': 'thread.started', 'thread_id': OTHER_TASK_ID if mode == 'wrong_thread' else task_id}))
if mode == 'lock_after_thread_event':
    sys.stderr.write(lock_error)
    sys.exit(1)
if mode == 'failed':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'local test failure'}}))
    sys.exit(1)
if mode != 'missing_completed':
    print(json.dumps({'type': 'turn.completed'}))
if mode != 'missing_final':
    result_file.write_text('   \\n' if mode == 'empty_final' else '测试完成。')
if mode == 'nonzero_with_final':
    sys.exit(1)
if mode == 'zero_with_lock_text':
    sys.stderr.write('already has an active writer')
'''
        source = source.replace("mode = MODE", "mode = " + repr(mode)).replace("OTHER_TASK_ID", repr(OTHER_TASK_ID))
        self.cli.write_text(source)
        self.cli.chmod(0o700)

    def execute(self, mode):
        self.fake_cli(mode)
        return self.daemon.execute_cli(self.claim, self.metadata, self.run_dir)

    def capture(self):
        return json.loads((self.run_dir / "capture.json").read_text())

    def test_exact_uuid_argv_and_stdin_produce_result(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": OTHER_TASK_ID}):
            result = self.execute("success")
        self.assertEqual(result, {"status": "completed", "body": "测试完成。"})
        capture = self.capture()
        args = capture["argv"]
        self.assertEqual(args[0], "exec")
        self.assertIn("--json", args)
        self.assertIn("resume", args)
        self.assertEqual(args[-2:], [TASK_ID, "-"])
        self.assertEqual(args.count(TASK_ID), 1)
        self.assertNotIn("--last", args)
        self.assertEqual(args[args.index("-C") + 1], str(self.cwd))
        self.assertEqual(capture["cwd"], str(self.cwd.resolve()))
        self.assertEqual(capture["codex_home"], str(self.daemon.codex_home))
        self.assertIsNone(capture["thread_env"])
        self.assertEqual(capture["mail_owner"], "daemon")
        self.assertIn(REPLY_ID, capture["stdin"])
        self.assertTrue(capture["stdin"].endswith(self.claim["body"]))

    def test_prompt_shell_metacharacters_remain_literal(self):
        sentinel = self.root / "must-not-exist"
        body = "literal $(touch '" + str(sentinel) + "') `touch '" + str(sentinel) + "'`; echo injected\n--last"
        self.claim["body"] = body
        self.assertEqual(self.execute("success")["status"], "completed")
        self.assertFalse(sentinel.exists())
        self.assertTrue(self.capture()["stdin"].endswith(body))
        self.assertNotIn(body, self.capture()["argv"])

    def test_wrong_uuid_is_not_success(self):
        self.assertEqual(self.execute("wrong_thread")["status"], "attention")

    def test_any_wrong_started_uuid_is_not_success(self):
        self.assertEqual(self.execute("mixed_wrong_then_exact")["status"], "attention")

    def test_missing_start_or_completion_event_or_final_is_not_success(self):
        for mode in ("missing_started", "missing_completed", "missing_final", "empty_final"):
            with self.subTest(mode=mode):
                (self.run_dir / "result.txt").unlink(missing_ok=True)
                self.assertEqual(self.execute(mode)["status"], "attention")

    def test_failure_and_nonzero_with_final_are_not_success(self):
        for mode in ("failed", "nonzero_with_final"):
            with self.subTest(mode=mode):
                self.assertEqual(self.execute(mode)["status"], "attention")

    def test_native_writer_lock_before_any_events_is_deferred(self):
        result = self.execute("lock")
        self.assertEqual(result["status"], "deferred")

    def test_lock_text_after_thread_or_turn_event_is_not_deferred(self):
        for mode in ("lock_after_thread_event", "lock_after_turn_event"):
            with self.subTest(mode=mode):
                self.assertEqual(self.execute(mode)["status"], "attention")

    def test_nonlock_failure_is_not_deferred(self):
        self.assertEqual(self.execute("nonlock")["status"], "attention")

    def test_incidental_partial_lock_text_is_not_deferred(self):
        self.assertEqual(self.execute("loose_lock_text")["status"], "attention")

    def test_success_with_incidental_lock_text_is_not_deferred(self):
        self.assertEqual(self.execute("zero_with_lock_text")["status"], "completed")

    def test_sensitive_run_files_are_private_despite_open_umask(self):
        original_umask = os.umask(0o022)
        try:
            self.assertEqual(self.execute("success")["status"], "completed")
        finally:
            os.umask(original_umask)
        self.assertEqual(self.run_dir.stat().st_mode & 0o777, 0o700)
        for name in ("events.jsonl", "stderr.txt", "result.txt"):
            with self.subTest(name=name):
                self.assertEqual((self.run_dir / name).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
