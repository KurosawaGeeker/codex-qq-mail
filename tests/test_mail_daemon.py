"""Offline daemon crash-recovery tests: real SQLite, fake mail and execution."""

from __future__ import annotations

import copy
from contextlib import closing
import fcntl
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest



import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_mail_daemon import MailDaemon
from qq_mail_service import DeliveryFailure, MailService
from qq_reply_service import ReplyService
from test_reply_queue import EVENT_A, THREAD_A, WIRE_A, FakeMailbox, FakeTransport, user_reply


class SimulatedProcessDeath(BaseException):
    """Represent an abrupt process death, not a recoverable executor error."""


class RecordingReplyService:
    def __init__(self, service):
        self.service = service
        self.mail = service.mail
        self.calls = []

    def handle(self, operation, args):
        self.calls.append((operation, dict(args)))
        return self.service.handle(operation, args)


class MailDaemonRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="qq-mail-daemon-test-")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.rollout_path = self.codex_home / "test-rollout.jsonl"
        self.rollout_path.write_text("", encoding="utf-8")
        self.transport = FakeTransport()
        self.mail = MailService(self.state_dir, self.transport)
        self.mail.handle("configure_email_thread", {
            "thread_id": THREAD_A, "subject": "Daemon recovery test", "notification_mode": "long_tasks",
        })
        sent = self.mail.handle("send_completion_email", {
            "thread_id": THREAD_A, "event_id": EVENT_A,
            "body": "Initial notification.", "elapsed_seconds": 601,
        })
        self.assertEqual(sent["status"], "accepted")
        self.mailbox = FakeMailbox()
        notification = copy.deepcopy(self.transport.messages[0])
        notification.replace_header("Message-ID", WIRE_A)
        self.mailbox.add(10, notification)
        self.service = ReplyService(self.state_dir, lambda: self.mailbox, self.mail)
        self.service.handle("enable_email_replies", {"thread_id": THREAD_A})
        self.recording_service = RecordingReplyService(self.service)
        self.execution_calls = []
        self.available = True
        self.execution_result = {"status": "completed", "body": "Email loop completed."}
        self.executor_error = None
        self.daemon = self.new_daemon()

    def new_daemon(self):
        return MailDaemon(
            state_dir=self.state_dir,
            codex_home=self.codex_home,
            cli_path=Path("/bin/false"),
            reply_service=self.recording_service,
            executor=self.execute,
            thread_reader=lambda thread_id: {
                "cwd": str(self.workspace), "rollout_path": str(self.rollout_path),
            },
            availability=lambda thread_id: self.available,
        )

    def execute(self, claim, metadata, run_dir):
        # An abrupt death immediately after this point must never cause a rerun.
        record = self.journal(claim["reply_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record["state"], "launching")
        self.assertEqual(record["claim_token"], claim["claim_token"])
        self.execution_calls.append((dict(claim), dict(metadata), str(run_dir)))
        if self.executor_error:
            raise self.executor_error
        return dict(self.execution_result)

    def operation(self, operation, **args):
        return self.service.handle(operation, {"thread_id": THREAD_A, **args})

    def enqueue(self, uid=11, message_id="<daemon-reply@example.test>"):
        self.mailbox.add(uid, user_reply(WIRE_A, message_id=message_id, body="Run the offline test."))
        return self.operation("poll_email_replies")

    def claim(self):
        result = self.operation("claim_email_reply")
        self.assertEqual(result["status"], "claimed")
        return result

    def reply(self, reply_id=None):
        rows = self.operation("get_email_reply_status")["replies"]
        if reply_id:
            return next(row for row in rows if row["reply_id"] == reply_id)
        return rows[0] if rows else None

    def journal(self, reply_id):
        with closing(sqlite3.connect(self.state_dir / "daemon.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM executions WHERE reply_id=?", (reply_id,)).fetchone()
        return dict(row) if row else None

    def seed_journal(self, claim, state, result_body=None):
        values = {
            "reply_id": claim["reply_id"], "thread_id": THREAD_A,
            "claim_token": claim["claim_token"], "state": state,
            "result_body": result_body, "error_code": None,
        }
        with closing(sqlite3.connect(self.state_dir / "daemon.sqlite3")) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(executions)")}
            if "run_dir" in columns:
                run_dir = self.state_dir / "runs" / claim["reply_id"]
                run_dir.mkdir(parents=True, exist_ok=True)
                values["run_dir"] = str(run_dir)
            names = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            db.execute(f"INSERT INTO executions ({names}) VALUES ({placeholders})", tuple(values.values()))
            db.commit()

    def assert_no_claim(self):
        self.assertFalse(any(operation == "claim_email_reply" for operation, _ in self.recording_service.calls))

    def assert_result_mail(self, text):
        self.assertEqual(self.transport.messages[-1].get_content().strip(), text)

    def test_empty_queue_never_invokes_executor(self):
        for _ in range(3):
            result = self.daemon.process_thread(THREAD_A)
            self.assertIn("status", result)
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(len(self.transport.messages), 1)

    def test_unmatched_mail_never_invokes_executor(self):
        self.mailbox.add(11, user_reply("<unregistered-notification@example.test>"))
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.execution_calls, [])
        self.assertIsNone(self.reply())
        self.assertEqual(len(self.transport.messages), 1)

    def test_busy_app_keeps_reply_queued_without_claiming(self):
        self.enqueue()
        self.available = False
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.reply()["state"], "queued")
        self.assert_no_claim()
        self.assertEqual(self.execution_calls, [])

    def test_daemon_lock_contention_does_not_claim_or_execute(self):
        self.enqueue()
        with (self.state_dir / "dispatch.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.daemon.process_thread(THREAD_A)
        self.assert_no_claim()
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(self.reply()["state"], "queued")

    def test_duplicate_mail_and_restart_execute_and_send_once(self):
        self.enqueue()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.reply()["state"], "completed")
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 2)
        self.assert_result_mail(self.execution_result["body"])
        self.enqueue(uid=12)  # Same Message-ID appears at a new mailbox UID.
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 2)

    def test_prepared_restart_can_launch_once(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, "prepared")
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(self.reply()["state"], "completed")
        self.assertEqual(len(self.transport.messages), 2)

    def test_launching_restart_without_result_never_reexecutes(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, "launching")
        for _ in range(2):
            self.daemon = self.new_daemon()
            self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(self.reply()["state"], "claimed")
        self.assertEqual(len(self.transport.messages), 1)

    def test_death_after_launch_commit_never_reexecutes(self):
        self.enqueue()
        self.executor_error = SimulatedProcessDeath()
        with self.assertRaises(SimulatedProcessDeath):
            self.daemon.process_thread(THREAD_A)
        claim = self.execution_calls[0][0]
        self.assertEqual(self.journal(claim["reply_id"])["state"], "launching")
        self.executor_error = None
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 1)

    def test_claim_without_journal_is_not_assumed_to_belong_to_daemon(self):
        self.enqueue()
        claim = self.claim()
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(self.reply(claim["reply_id"])["state"], "claimed")
        self.assertEqual(len(self.transport.messages), 1)

    def test_result_ready_restart_only_delivers_saved_result(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, "result_ready", "Recovered final answer.")
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(self.reply()["state"], "completed")
        self.assertEqual(len(self.transport.messages), 2)
        self.assert_result_mail("Recovered final answer.")

    def test_result_delivery_does_not_require_an_available_app(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, "result_ready", "Answer already computed.")
        self.available = False
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.reply()["state"], "completed")
        self.assertEqual(self.execution_calls, [])
        self.assert_result_mail("Answer already computed.")

    def test_failed_smtp_restart_retries_same_result_without_execution(self):
        self.enqueue()
        self.transport.failure = DeliveryFailure("failed", "smtp_connection_failed")
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.reply()["state"], "result_pending")
        failed_message = self.transport.messages[-1]
        self.transport.failure = None
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(self.reply()["state"], "completed")
        self.assertEqual(len(self.transport.messages), 3)
        delivered_message = self.transport.messages[-1]
        self.assertEqual(str(failed_message["Message-ID"]), str(delivered_message["Message-ID"]))
        self.assertEqual(str(failed_message["X-Codex-Event-ID"]), str(delivered_message["X-Codex-Event-ID"]))
        self.assertEqual(failed_message.get_content(), delivered_message.get_content())

    def test_unknown_smtp_never_reexecutes_or_automatically_resends(self):
        self.enqueue()
        self.transport.failure = DeliveryFailure("unknown", "smtp_reply_lost")
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.reply()["state"], "result_pending")
        self.transport.failure = None
        for _ in range(3):
            self.daemon = self.new_daemon()
            self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.reply()["state"], "result_pending")

    def test_completed_reply_wins_over_stale_result_ready_journal(self):
        self.enqueue()
        claim = self.claim()
        body = "Already delivered before daemon crashed."
        self.seed_journal(claim, "result_ready", body)
        result = self.operation("complete_email_reply", reply_id=claim["reply_id"],
                                claim_token=claim["claim_token"], result_body=body)
        self.assertEqual(result["status"], "completed")
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.reply()["state"], "completed")

    def test_uncertain_executor_outcome_blocks_later_mail(self):
        self.enqueue()
        self.enqueue(uid=12, message_id="<second-daemon-reply@example.test>")
        self.execution_result = {"status": "attention", "code": "cli_outcome_unknown"}
        self.daemon.process_thread(THREAD_A)
        self.daemon = self.new_daemon()
        self.daemon.process_thread(THREAD_A)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 1)
        states = [row["state"] for row in self.operation("get_email_reply_status")["replies"]]
        self.assertEqual(states, ["claimed", "queued"])

    def test_native_recovery_delivers_without_resubmitting(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, "launching")
        calls = []
        def recover(original, metadata, run_dir):
            calls.append(original['reply_id'])
            return {'status': 'completed', 'body': 'Recovered shared native turn.'}
        self.daemon.recovery = recover
        result = self.daemon.process_thread(THREAD_A)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls, [claim['reply_id']])
        self.assertEqual(self.execution_calls, [])
        self.assert_result_mail('Recovered shared native turn.')

    def test_native_recovery_running_keeps_claim_without_model(self):
        self.enqueue()
        claim = self.claim()
        self.seed_journal(claim, 'launching')
        self.daemon.recovery = lambda *args: {'status': 'running'}
        for _ in range(2):
            self.assertEqual(self.daemon.process_thread(THREAD_A)['status'], 'waiting_for_result')
        self.assertEqual(self.execution_calls, [])
        self.assertEqual(len(self.transport.messages), 1)
        self.assertEqual(self.reply()['state'], 'claimed')

    def test_attention_sends_one_notice_and_preserves_claim(self):
        self.enqueue()
        self.execution_result = {'status': 'attention', 'code': 'connection_lost'}
        for _ in range(3):
            result = self.daemon.process_thread(THREAD_A)
            self.daemon.notify_attention(THREAD_A, result)
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.reply()['state'], 'claimed')

    def test_failed_attention_notice_recovers_before_final_result(self):
        self.enqueue()
        self.execution_result = {'status': 'attention', 'code': 'connection_lost'}
        result = self.daemon.process_thread(THREAD_A)
        self.transport.failure = DeliveryFailure('failed', 'smtp_connection_failed')
        self.daemon.notify_attention(THREAD_A, result)
        self.transport.failure = None
        self.daemon.recovery = lambda *args: {'status': 'completed', 'body': 'Recovered answer.'}
        result = self.daemon.process_thread(THREAD_A)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(self.execution_calls), 1)
        self.assertEqual(self.reply()['state'], 'completed')
        self.assert_result_mail('Recovered answer.')


if __name__ == "__main__":
    unittest.main()
