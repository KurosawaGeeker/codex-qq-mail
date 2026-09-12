"""Offline behavior tests for the local QQ mail process.

Run with: python3 -m unittest discover -s tests -v
The tests use a real temporary SQLite state store and intercept EmailMessage
objects at the transport boundary; they never use network or credentials.
"""

from __future__ import annotations

import copy
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
import smtplib
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid



import bootstrap  # Isolate state and add the source directory before runtime imports.

import qq_mail_service
from qq_mail_service import DeliveryFailure, MailService


THREAD_A = "00000000-0000-4000-8000-000000000001"
THREAD_B = "00000000-0000-4000-8000-000000000002"
EVENT_A = "00000000-0000-4000-8000-000000000003"
EVENT_B = "00000000-0000-4000-8000-000000000004"
EVENT_C = "00000000-0000-4000-8000-000000000005"
SUBJECT = "继续这个 Codex 任务"


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.failure: DeliveryFailure | None = None

    def __call__(self, message: EmailMessage) -> None:
        if not isinstance(message, EmailMessage):
            raise TypeError("The transport boundary must receive EmailMessage")
        self.messages.append(copy.deepcopy(message))
        if self.failure is not None:
            raise self.failure


class MailServiceBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="qq-mail-test-")
        self.addCleanup(self.temp_dir.cleanup)
        self.state_dir = Path(self.temp_dir.name)
        self.transport = FakeTransport()
        self.service = MailService(self.state_dir, self.transport)

    def configure(
        self,
        thread_id: str = THREAD_A,
        notification_mode: str = "long_tasks",
    ) -> dict:
        return self.service.handle(
            "configure_email_thread",
            {
                "thread_id": thread_id,
                "subject": SUBJECT,
                "notification_mode": notification_mode,
            },
        )

    def send(
        self,
        *,
        thread_id: str = THREAD_A,
        event_id: str = EVENT_A,
        body: str = "已完成。验证通过。",
        subject: str = SUBJECT,
        elapsed_seconds: float = 601,
        send_now: bool = False,
    ) -> dict:
        return self.service.handle(
            "send_completion_email",
            {
                "thread_id": thread_id,
                "event_id": event_id,
                "body": body,
                "subject": subject,
                "elapsed_seconds": elapsed_seconds,
                "send_now": send_now,
            },
        )

    def test_mail_thread_uuid_persists_across_process_instances(self) -> None:
        configured = self.configure()
        first = self.send()
        self.assertEqual(first["status"], "accepted")
        mail_thread_id = first["mail_thread_id"]
        self.assertEqual(str(uuid.UUID(mail_thread_id)), mail_thread_id)
        self.assertEqual(configured["mail_thread_id"], mail_thread_id)

        self.service = MailService(self.state_dir, self.transport)
        restored = self.service.handle("get_email_thread", {"thread_id": THREAD_A})
        self.assertEqual(restored["mail_thread_id"], mail_thread_id)
        second = self.send(event_id=EVENT_B)
        self.assertEqual(second["mail_thread_id"], mail_thread_id)
        self.assertEqual(second["status"], "accepted")

    def test_distinct_codex_tasks_have_independent_mail_threads(self) -> None:
        self.configure(THREAD_A)
        self.configure(THREAD_B)
        first = self.send(thread_id=THREAD_A)
        second = self.send(thread_id=THREAD_B)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        self.assertNotEqual(first["mail_thread_id"], second["mail_thread_id"])
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertIsNone(self.transport.messages[1]["In-Reply-To"])
        self.assertIsNone(self.transport.messages[1]["References"])

    def test_message_ids_are_distinct_but_subject_and_reply_chain_are_stable(self) -> None:
        self.configure()
        results = [
            self.send(event_id=EVENT_A),
            self.send(event_id=EVENT_B, subject="不能另开一条邮件流"),
            self.send(event_id=EVENT_C, subject="第三轮的其他标题"),
        ]
        messages = self.transport.messages
        self.assertEqual(len(messages), 3)
        self.assertEqual(len({result["mail_thread_id"] for result in results}), 1)
        message_ids = [str(message["Message-ID"]) for message in messages]
        self.assertEqual(len(set(message_ids)), 3)
        for result, message_id in zip(results, message_ids):
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["message_id"], message_id)
            self.assertTrue(message_id.startswith("<") and message_id.endswith(">"))
        subjects = [str(message["Subject"]) for message in messages]
        self.assertTrue(subjects[0])
        self.assertEqual(subjects, [subjects[0]] * 3)
        self.assertIsNone(messages[0]["In-Reply-To"])
        self.assertIsNone(messages[0]["References"])
        self.assertEqual(str(messages[1]["In-Reply-To"]), message_ids[0])
        self.assertEqual(str(messages[1]["References"]).split(), message_ids[:1])
        self.assertEqual(str(messages[2]["In-Reply-To"]), message_ids[1])
        self.assertEqual(str(messages[2]["References"]).split(), message_ids[:2])

    def test_same_event_is_sent_once_including_after_process_restart(self) -> None:
        self.configure()
        initial = self.send()
        self.assertEqual(initial["status"], "accepted")
        self.assertFalse(initial["duplicate"])
        for restart in (False, True):
            if restart:
                self.service = MailService(self.state_dir, self.transport)
            duplicate = self.send()
            self.assertEqual(duplicate["status"], "accepted")
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["message_id"], initial["message_id"])
            self.assertEqual(duplicate["mail_thread_id"], initial["mail_thread_id"])
        self.assertEqual(len(self.transport.messages), 1)

    def test_reusing_an_event_with_conflicting_content_is_rejected(self) -> None:
        self.configure()
        self.send()
        with self.assertRaises(ValueError):
            self.send(body="同一个事件换了正文，不能再次投递。")
        self.assertEqual(len(self.transport.messages), 1)

    def test_default_threshold_is_strictly_greater_than_ten_minutes(self) -> None:
        self.configure()
        at_threshold = self.send(event_id=EVENT_A, elapsed_seconds=600)
        self.assertEqual(at_threshold["status"], "skipped")
        self.assertEqual(len(self.transport.messages), 0)
        over_threshold = self.send(event_id=EVENT_B, elapsed_seconds=601)
        self.assertEqual(over_threshold["status"], "accepted")
        self.assertEqual(len(self.transport.messages), 1)

    def test_always_mode_persists_and_sends_short_task_results(self) -> None:
        self.configure(notification_mode="always")
        self.service = MailService(self.state_dir, self.transport)
        restored = self.service.handle("get_email_thread", {"thread_id": THREAD_A})
        self.assertEqual(restored["notification_mode"], "always")
        result = self.send(elapsed_seconds=1)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(self.transport.messages), 1)

    def test_explicit_send_now_allows_a_short_task(self) -> None:
        self.configure()
        result = self.send(elapsed_seconds=1, send_now=True)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(self.transport.messages), 1)

    def test_off_mode_skips_even_long_tasks_by_default(self) -> None:
        self.configure(notification_mode="off")
        result = self.send(elapsed_seconds=3600)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(len(self.transport.messages), 0)

    def test_ambiguous_delivery_is_not_retried_automatically(self) -> None:
        self.configure()
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        first = self.send()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(len(self.transport.messages), 1)

        self.transport.failure = None
        self.service = MailService(self.state_dir, self.transport)
        repeated = self.send()
        self.assertEqual(repeated["status"], "unknown")
        self.assertEqual(repeated["message_id"], first["message_id"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len(self.transport.messages), 1)

    def test_ambiguous_delivery_blocks_later_events_in_the_same_task(self) -> None:
        self.configure()
        self.configure(THREAD_B)
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        self.assertEqual(self.send()["status"], "unknown")

        self.transport.failure = None
        self.service = MailService(self.state_dir, self.transport)
        next_event = self.send(event_id=EVENT_B)
        self.assertEqual(next_event["status"], "unknown")
        self.assertEqual(len(self.transport.messages), 1)
        other_task = self.send(thread_id=THREAD_B, event_id=EVENT_C)
        self.assertEqual(other_task["status"], "accepted")
        self.assertEqual(len(self.transport.messages), 2)

    def test_definite_failure_retry_preserves_message_id_date_and_content(self) -> None:
        self.configure()
        self.transport.failure = DeliveryFailure(status="failed", code="smtp_authentication_failed")
        with patch(
            "qq_mail_service.formatdate",
            side_effect=[
                "Sat, 12 Sep 2026 09:00:00 +0800",
                "Sat, 12 Sep 2026 09:01:00 +0800",
            ],
        ):
            first = self.send()
            self.assertEqual(first["status"], "failed")
            first_message = self.transport.messages[0]
            self.transport.failure = None
            self.service = MailService(self.state_dir, self.transport)
            retried = self.send()
        self.assertEqual(retried["status"], "accepted")
        self.assertEqual(retried["message_id"], first["message_id"])
        self.assertEqual(len(self.transport.messages), 2)
        second_message = self.transport.messages[1]
        self.assertEqual(str(second_message["Date"]), str(first_message["Date"]))
        self.assertEqual(second_message.as_bytes(), first_message.as_bytes())

    def test_process_interruption_during_submission_is_recovered_as_unknown(self) -> None:
        self.configure()
        self.transport.failure = DeliveryFailure(status="failed", code="test_preparation")
        first = self.send()
        self._set_event_status(THREAD_A, EVENT_A, "sending")
        self.transport.failure = None
        self.service = MailService(self.state_dir, self.transport)
        recovered = self.send()
        self.assertEqual(recovered["status"], "unknown")
        self.assertEqual(recovered["message_id"], first["message_id"])
        self.assertEqual(len(self.transport.messages), 1)
        later = self.send(event_id=EVENT_B)
        self.assertEqual(later["status"], "unknown")
        self.assertEqual(len(self.transport.messages), 1)

    def test_failed_or_prepared_submission_must_be_retried_before_a_new_event(self) -> None:
        for thread_id, status in ((THREAD_A, "failed"), (THREAD_B, "prepared")):
            with self.subTest(status=status):
                self.configure(thread_id)
                self.transport.failure = DeliveryFailure(status="failed", code="test_preparation")
                first = self.send(thread_id=thread_id)
                self.assertEqual(first["status"], "failed")
                if status == "prepared":
                    self._set_event_status(thread_id, EVENT_A, "prepared")
                self.transport.failure = None
                self.service = MailService(self.state_dir, self.transport)
                submission_count = len(self.transport.messages)
                blocked = self.send(thread_id=thread_id, event_id=EVENT_B)
                self.assertEqual(blocked["status"], status)
                self.assertEqual(blocked["code"], "previous_submission_needs_retry")
                self.assertEqual(blocked["message_id"], first["message_id"])
                self.assertEqual(len(self.transport.messages), submission_count)
                retried = self.send(thread_id=thread_id)
                self.assertEqual(retried["status"], "accepted")
                next_event = self.send(thread_id=thread_id, event_id=EVENT_B)
                self.assertEqual(next_event["status"], "accepted")
                self.assertEqual(len(self.transport.messages), submission_count + 2)

    def test_confirming_unknown_as_received_continues_the_original_reply_chain(self) -> None:
        self.configure()
        first = self.send(event_id=EVENT_A)
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        uncertain = self.send(event_id=EVENT_B)
        self.assertEqual(uncertain["status"], "unknown")
        self.transport.failure = None
        resolved = self.service.handle(
            "resolve_email_submission",
            {"thread_id": THREAD_A, "event_id": EVENT_B, "resolution": "received"},
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["submission_status"], "accepted")
        self.assertEqual(len(self.transport.messages), 2)

        self.service = MailService(self.state_dir, self.transport)
        duplicate = self.send(event_id=EVENT_B)
        self.assertEqual(duplicate["status"], "accepted")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["message_id"], uncertain["message_id"])
        self.assertEqual(len(self.transport.messages), 2)
        next_event = self.send(event_id=EVENT_C)
        self.assertEqual(next_event["status"], "accepted")
        self.assertEqual(len(self.transport.messages), 3)
        message = self.transport.messages[-1]
        self.assertEqual(str(message["In-Reply-To"]), uncertain["message_id"])
        self.assertEqual(
            str(message["References"]).split(),
            [first["message_id"], uncertain["message_id"]],
        )
        with closing(sqlite3.connect(self.state_dir / "state.sqlite3")) as db:
            stored_body = db.execute(
                "SELECT raw_message FROM messages WHERE thread_id=? AND event_id=?",
                (THREAD_A, EVENT_B),
            ).fetchone()[0]
        self.assertIsNone(stored_body)

    def test_authorized_retry_only_prepares_and_later_sends_identical_original_mail(self) -> None:
        self.configure()
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        uncertain = self.send()
        self.assertEqual(uncertain["status"], "unknown")
        original_mail = self.transport.messages[0].as_bytes()
        self.transport.failure = None
        resolved = self.service.handle(
            "resolve_email_submission",
            {"thread_id": THREAD_A, "event_id": EVENT_A, "resolution": "retry_authorized"},
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["submission_status"], "prepared")
        self.assertEqual(len(self.transport.messages), 1)

        self.service = MailService(self.state_dir, self.transport)
        blocked = self.send(event_id=EVENT_B)
        self.assertEqual(blocked["status"], "prepared")
        self.assertEqual(len(self.transport.messages), 1)
        retried = self.send()
        self.assertEqual(retried["status"], "accepted")
        self.assertEqual(retried["message_id"], uncertain["message_id"])
        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.transport.messages[1].as_bytes(), original_mail)

    def test_resolution_cannot_release_a_different_task_or_event(self) -> None:
        self.configure(THREAD_A)
        self.configure(THREAD_B)
        self.send(thread_id=THREAD_B, event_id=EVENT_B)
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        uncertain = self.send(thread_id=THREAD_A, event_id=EVENT_A)
        self.assertEqual(uncertain["status"], "unknown")
        self.transport.failure = None

        for wrong_thread, wrong_event in (
            (THREAD_B, EVENT_A),
            (THREAD_A, EVENT_B),
            (THREAD_B, EVENT_B),
        ):
            with self.subTest(thread_id=wrong_thread, event_id=wrong_event):
                with self.assertRaises(ValueError):
                    self.service.handle(
                        "resolve_email_submission",
                        {
                            "thread_id": wrong_thread,
                            "event_id": wrong_event,
                            "resolution": "received",
                        },
                    )
                still_blocked = self.send(thread_id=THREAD_A, event_id=EVENT_C)
                self.assertEqual(still_blocked["status"], "unknown")
                self.assertEqual(still_blocked["message_id"], uncertain["message_id"])
                self.assertEqual(len(self.transport.messages), 2)

    def test_resolution_rejects_invalid_choice_without_releasing_unknown(self) -> None:
        self.configure()
        self.transport.failure = DeliveryFailure(status="unknown", code="smtp_reply_lost")
        self.send()
        self.transport.failure = None
        with self.assertRaises(ValueError):
            self.service.handle(
                "resolve_email_submission",
                {"thread_id": THREAD_A, "event_id": EVENT_A, "resolution": "retry_automatically"},
            )
        self.assertEqual(self.send(event_id=EVENT_B)["status"], "unknown")
        self.assertEqual(len(self.transport.messages), 1)

    def _set_event_status(self, thread_id: str, event_id: str, status: str) -> None:
        """Model a persisted state left behind when the worker process stops."""
        with closing(sqlite3.connect(self.state_dir / "state.sqlite3")) as db:
            changed = db.execute(
                "UPDATE messages SET status=?, error_code=NULL WHERE thread_id=? AND event_id=?",
                (status, thread_id, event_id),
            ).rowcount
            self.assertEqual(changed, 1)
            db.commit()


class SmtpTransportBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="qq-smtp-test-")
        self.addCleanup(self.temp_dir.cleanup)
        self.smtp = MagicMock()
        self.smtp.send_message.return_value = {}
        self.keychain_patch = patch(
            "qq_mail_service.subprocess.run",
            return_value=SimpleNamespace(stdout="unit-test-only-credential\n"),
        )
        self.smtp_patch = patch("qq_mail_service.smtplib.SMTP_SSL", return_value=self.smtp)
        self.keychain = self.keychain_patch.start()
        self.addCleanup(self.keychain_patch.stop)
        self.smtp_factory = self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)
        self.service = MailService(Path(self.temp_dir.name), qq_mail_service.smtp_transport)

    def send(self) -> dict:
        return self.service.handle(
            "send_completion_email",
            {
                "thread_id": THREAD_A,
                "event_id": EVENT_A,
                "subject": SUBJECT,
                "body": "测试 SMTP 的结果状态，无外部邮件。",
                "elapsed_seconds": 601,
            },
        )

    def test_accepted_data_remains_accepted_if_socket_cleanup_fails(self) -> None:
        self.smtp.close.side_effect = OSError("test socket close failed")
        result = self.send()
        self.assertEqual(result["status"], "accepted")
        self.smtp.send_message.assert_called_once()
        self.smtp.close.assert_called_once()
        self.assertEqual(self.keychain.call_count, 1)
        self.assertEqual(self.smtp_factory.call_count, 1)

    def test_connection_loss_during_data_is_unknown_and_does_not_resubmit(self) -> None:
        self.smtp.send_message.side_effect = smtplib.SMTPServerDisconnected(
            "test disconnect during DATA"
        )
        first = self.send()
        self.assertEqual(first["status"], "unknown")
        self.smtp.send_message.side_effect = None
        duplicate = self.send()
        self.assertEqual(duplicate["status"], "unknown")
        self.assertEqual(duplicate["message_id"], first["message_id"])
        self.smtp.send_message.assert_called_once()
        self.smtp.close.assert_called_once()
        self.assertEqual(self.keychain.call_count, 1)

    def test_authentication_rejection_is_failed_before_data_submission(self) -> None:
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"test login rejected")
        result = self.send()
        self.assertEqual(result["status"], "failed")
        self.smtp.send_message.assert_not_called()
        self.smtp.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
