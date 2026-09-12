"""Offline reply-queue behavior tests; no QQ mailbox or Codex execution."""

from __future__ import annotations

import copy
from contextlib import closing
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch



import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_mail_service import ACCOUNT, DeliveryFailure, MailService
from qq_reply_service import ReplyService
from qq_reply_parser import ReplyRejected


THREAD_A = "00000000-0000-4000-8000-000000000001"
THREAD_B = "00000000-0000-4000-8000-000000000002"
EVENT_A = "00000000-0000-4000-8000-000000000003"
EVENT_B = "00000000-0000-4000-8000-000000000004"
WIRE_A = "<provider-notification-a@qq.com>"


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []
        self.failure: DeliveryFailure | None = None

    def __call__(self, message: EmailMessage) -> None:
        self.messages.append(copy.deepcopy(message))
        if self.failure:
            raise self.failure


class FakeMailbox:
    """The trusted sent-folder boundary; parser identity tests live separately."""

    def __init__(self) -> None:
        self.uidvalidity = "test-mailbox-generation-1"
        self.messages: list[tuple[int, bytes]] = []
        self.closed_count = 0

    def snapshot(self) -> dict:
        return {
            "uidvalidity": self.uidvalidity,
            "last_uid": max((uid for uid, _ in self.messages), default=0),
        }

    def find_notifications(self, mail_thread_id: str) -> list[tuple[int, bytes]]:
        matching = []
        for uid, raw in self.messages:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            if str(message.get("X-Codex-Mail-Thread-ID", "")) == mail_thread_id:
                matching.append((uid, raw))
        return matching

    def fetch_since(self, last_uid: int) -> list[tuple[int, bytes]]:
        return [(uid, raw) for uid, raw in self.messages if uid > int(last_uid)]

    def fetch_message(self, uid: int) -> bytes:
        return next((raw for message_uid, raw in self.messages if message_uid == int(uid)), b"")

    def close(self) -> None:
        self.closed_count += 1

    def add(self, uid: int, message: EmailMessage) -> None:
        self.messages.append((uid, message.as_bytes()))


def user_reply(
    parent_id: str,
    *,
    message_id: str = "<user-reply-a@qq.com>",
    body: str = "请继续运行测试。",
    references: list[str] | None = None,
) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = ACCOUNT
    message["To"] = ACCOUNT
    message["Subject"] = "Re: Codex 任务"
    message["Message-ID"] = message_id
    message["In-Reply-To"] = parent_id
    message["References"] = " ".join(references or [parent_id])
    message.set_content(body)
    return message


class ReplyQueueBehaviorTests(unittest.TestCase):
    def test_explicit_local_alias_recovers_rejected_reply_and_clears_status(self):
        self.enable()
        reply = user_reply(WIRE_A, body='你是谁')
        reply.replace_header('From', 'confirmed-alias@qq.com')
        self.mailbox.add(11, reply)
        self.assertEqual(self.operation('poll_email_replies')['rejected_counts'], {'wrong_account': 1})
        self.restart()
        self.assertEqual(self.operation('get_email_reply_status')['rejected_replies'], [{'uid': 11, 'code': 'wrong_account'}])
        (self.state_dir / 'account-aliases.json').write_text('{"sender_aliases": ["confirmed-alias@qq.com"]}')
        self.assertEqual(self.operation('retry_email_reply', uid=11)['new_replies'], 1)
        self.assertEqual(self.operation('get_email_reply_status')['rejected_replies'], [])
        self.assertEqual(self.operation('claim_email_reply')['body'], '你是谁')

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="qq-reply-queue-test-")
        self.addCleanup(self.temp_dir.cleanup)
        self.state_dir = Path(self.temp_dir.name)
        self.transport = FakeTransport()
        self.mail = MailService(self.state_dir, self.transport)
        self.mail.handle(
            "configure_email_thread",
            {"thread_id": THREAD_A, "subject": "当前任务", "notification_mode": "long_tasks"},
        )
        self.initial = self.mail.handle(
            "send_completion_email",
            {
                "thread_id": THREAD_A,
                "event_id": EVENT_A,
                "body": "第一轮已经完成。",
                "elapsed_seconds": 601,
            },
        )
        self.assertEqual(self.initial["status"], "accepted")
        self.mailbox = FakeMailbox()
        notification = copy.deepcopy(self.transport.messages[0])
        notification.replace_header("Message-ID", WIRE_A)
        self.mailbox.add(10, notification)
        self.restart()

    def restart(self) -> None:
        self.mail = MailService(self.state_dir, self.transport)
        self.service = ReplyService(self.state_dir, lambda: self.mailbox, self.mail)

    def operation(self, name: str, *, thread_id: str = THREAD_A, **args) -> dict:
        return self.service.handle(name, {"thread_id": thread_id, **args})

    def enable(self) -> dict:
        return self.operation("enable_email_replies")

    def enqueue_and_claim(self, *, message_id: str = "<user-reply-a@qq.com>") -> dict:
        self.enable()
        self.mailbox.add(11, user_reply(WIRE_A, message_id=message_id))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        claim = self.operation("claim_email_reply")
        self.assertEqual(claim["status"], "claimed")
        return claim

    def complete(self, claim: dict, result_body: str = "已按回信完成；测试全部通过。") -> dict:
        return self.operation(
            "complete_email_reply",
            reply_id=claim["reply_id"],
            claim_token=claim["claim_token"],
            result_body=result_body,
        )

    def create_other_task_notification(self) -> dict:
        self.mail.handle(
            "configure_email_thread",
            {"thread_id": THREAD_B, "subject": "另一个任务", "notification_mode": "long_tasks"},
        )
        return self.mail.handle(
            "send_completion_email",
            {
                "thread_id": THREAD_B,
                "event_id": EVENT_B,
                "body": "另一个任务已经完成。",
                "elapsed_seconds": 601,
            },
        )

    def test_enabling_registers_baseline_and_ignores_earlier_replies(self) -> None:
        self.mailbox.add(9, user_reply(WIRE_A, message_id="<old-reply@qq.com>"))
        enabled = self.enable()
        self.assertEqual(enabled["status"], "enabled")
        self.assertEqual(enabled["baseline_uid"], 10)
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 0)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")
        self.mailbox.add(11, user_reply(WIRE_A))
        polled = self.operation("poll_email_replies")
        self.assertEqual(polled["new_replies"], 1)
        self.assertEqual(self.operation("claim_email_reply")["body"], "请继续运行测试。")

    def test_enabling_existing_subscription_does_not_skip_new_reply(self) -> None:
        self.enable()
        self.mailbox.add(11, user_reply(WIRE_A))
        self.restart()
        self.assertEqual(self.enable()["status"], "enabled")
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)

    def test_provider_notification_alias_is_synced_before_reply_parsing(self) -> None:
        self.enable()
        state = self.mail.handle("get_email_thread", {"thread_id": THREAD_A})
        self.assertEqual(state["messages"][0]["provider_message_id"], WIRE_A)
        self.mailbox.add(11, user_reply(WIRE_A))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        claim = self.operation("claim_email_reply")
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["message_id"], "<user-reply-a@qq.com>")

    def test_later_proactive_mail_uses_provider_parent_without_rewriting_local_identity(self) -> None:
        self.enable()
        synchronized = self.mail.handle("get_email_thread", {"thread_id": THREAD_A})
        original = synchronized["messages"][0]
        self.assertEqual(original["event_id"], EVENT_A)
        self.assertEqual(original["message_id"], self.initial["message_id"])
        self.assertEqual(original["provider_message_id"], WIRE_A)
        later = self.mail.handle(
            "send_completion_email",
            {
                "thread_id": THREAD_A,
                "event_id": EVENT_B,
                "body": "主动发来的第二轮结果。",
                "elapsed_seconds": 601,
            },
        )
        self.assertEqual(later["status"], "accepted")
        self.assertEqual(later["event_id"], EVENT_B)
        self.assertNotEqual(later["message_id"], self.initial["message_id"])
        wire_message = self.transport.messages[-1]
        self.assertEqual(str(wire_message["In-Reply-To"]), WIRE_A)
        self.assertEqual(str(wire_message["References"]).split(), [WIRE_A])
        self.assertEqual(str(wire_message["Message-ID"]), later["message_id"])
        self.assertEqual(str(wire_message["X-Codex-Event-ID"]), EVENT_B)

        with self.assertRaises(ValueError):
            self.mail.handle(
                "record_delivery_id",
                {"thread_id": THREAD_A, "event_id": EVENT_B, "provider_message_id": WIRE_A},
            )
        state = self.mail.handle("get_email_thread", {"thread_id": THREAD_A})
        records = {row["event_id"]: row for row in state["messages"]}
        self.assertEqual(records[EVENT_A]["message_id"], self.initial["message_id"])
        self.assertEqual(records[EVENT_A]["provider_message_id"], WIRE_A)
        self.assertEqual(records[EVENT_B]["message_id"], later["message_id"])
        self.assertIsNone(records[EVENT_B]["provider_message_id"])

    def test_duplicate_uid_or_message_id_never_queues_a_second_instruction(self) -> None:
        self.enable()
        reply = user_reply(WIRE_A)
        self.mailbox.add(11, reply)
        self.mailbox.add(11, reply)
        self.mailbox.add(12, reply)
        result = self.operation("poll_email_replies")
        self.assertEqual(result["new_replies"], 1)
        self.assertEqual(result["queued_count"], 1)
        self.restart()
        self.mailbox.add(13, reply)
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 0)
        state = self.operation("get_email_reply_status")
        self.assertEqual(len(state["replies"]), 1)

    def test_service_notifications_and_known_aliases_are_not_instructions(self) -> None:
        self.enable()
        notification = copy.deepcopy(self.transport.messages[0])
        notification.replace_header("Message-ID", "<provider-notification-copy@qq.com>")
        self.mailbox.add(11, notification)
        forged_reply_shape = user_reply(WIRE_A, message_id=WIRE_A)
        self.mailbox.add(12, forged_reply_shape)
        automatic = user_reply(WIRE_A, message_id="<automatic-reply@qq.com>")
        automatic["Auto-Submitted"] = "auto-replied"
        self.mailbox.add(13, automatic)
        polled = self.operation("poll_email_replies")
        self.assertEqual(polled["new_replies"], 0)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

    def test_unrelated_and_wrong_account_messages_cannot_enter_the_queue(self) -> None:
        self.enable()
        unrelated = user_reply("<unregistered-parent@qq.com>")
        self.mailbox.add(11, unrelated)
        foreign = user_reply(WIRE_A, message_id="<foreign-sender@qq.com>")
        foreign.replace_header("From", "other@qq.com")
        self.mailbox.add(12, foreign)
        wrong_recipient = user_reply(WIRE_A, message_id="<wrong-recipient@qq.com>")
        wrong_recipient.replace_header("To", "other@qq.com")
        self.mailbox.add(13, wrong_recipient)
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 0)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

    def test_mixed_known_task_references_cannot_enter_either_task_queue(self) -> None:
        other = self.create_other_task_notification()
        self.enable()
        self.operation("enable_email_replies", thread_id=THREAD_B)
        self.mailbox.add(
            11,
            user_reply(
                other["message_id"],
                message_id="<ambiguous-task-reply@qq.com>",
                references=[WIRE_A, other["message_id"]],
            ),
        )
        for thread_id in (THREAD_A, THREAD_B):
            with self.subTest(thread_id=thread_id):
                self.assertEqual(self.operation("poll_email_replies", thread_id=thread_id)["new_replies"], 0)
                self.assertEqual(self.operation("get_email_reply_status", thread_id=thread_id)["replies"], [])
                self.assertEqual(self.operation("claim_email_reply", thread_id=thread_id)["status"], "empty")

    def test_reply_for_another_task_is_not_consumed_or_marked_seen_by_wrong_poll(self) -> None:
        other = self.create_other_task_notification()
        self.enable()
        self.operation("enable_email_replies", thread_id=THREAD_B)
        self.mailbox.add(
            11,
            user_reply(other["message_id"], message_id="<other-task-reply@qq.com>", body="只继续另一个任务。"),
        )
        self.assertEqual(self.operation("poll_email_replies", thread_id=THREAD_A)["new_replies"], 0)
        self.assertEqual(self.operation("get_email_reply_status", thread_id=THREAD_A)["replies"], [])
        self.assertEqual(self.operation("poll_email_replies", thread_id=THREAD_B)["new_replies"], 1)
        claim = self.operation("claim_email_reply", thread_id=THREAD_B)
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["thread_id"], THREAD_B)
        self.assertEqual(claim["body"], "只继续另一个任务。")

    def test_claim_is_persistent_exclusive_and_blocks_later_queued_instructions(self) -> None:
        self.enable()
        self.mailbox.add(11, user_reply(WIRE_A, message_id="<first-reply@qq.com>", body="第一条指令"))
        self.mailbox.add(12, user_reply(WIRE_A, message_id="<second-reply@qq.com>", body="第二条指令"))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 2)
        first = self.operation("claim_email_reply")
        self.assertEqual(first["status"], "claimed")
        self.assertEqual(first["body"], "第一条指令")
        self.restart()
        repeated = self.operation("claim_email_reply")
        self.assertEqual(repeated["status"], "needs_attention")
        self.assertEqual(repeated["reply"]["reply_id"], first["reply_id"])
        self.assertEqual(repeated["reply"]["claim_token"], first["claim_token"])
        self.assertEqual(self.complete(first)["status"], "completed")
        second = self.operation("claim_email_reply")
        self.assertEqual(second["status"], "claimed")
        self.assertEqual(second["body"], "第二条指令")
        self.assertNotEqual(second["reply_id"], first["reply_id"])
        self.assertNotEqual(second["claim_token"], first["claim_token"])

    def test_successful_completion_replies_to_user_mail_and_keeps_task_identity(self) -> None:
        claim = self.enqueue_and_claim()
        # A reply-triggered completion must be sent even when proactive mail is off.
        self.mail.handle(
            "configure_email_thread",
            {"thread_id": THREAD_A, "notification_mode": "off"},
        )
        completed = self.complete(claim)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(self.transport.messages), 2)
        output = self.transport.messages[-1]
        self.assertEqual(str(output["Message-ID"]), completed["message_id"])
        self.assertEqual(str(output["In-Reply-To"]), claim["message_id"])
        self.assertEqual(str(output["References"]).split(), [WIRE_A, claim["message_id"]])
        self.assertEqual(str(output["Subject"]), str(self.transport.messages[0]["Subject"]))
        self.assertEqual(str(output["X-Codex-Thread-ID"]), THREAD_A)
        self.assertEqual(str(output["X-Codex-Mail-Thread-ID"]), self.initial["mail_thread_id"])
        state = self.operation("get_email_reply_status")
        self.assertEqual(state["replies"][0]["state"], "completed")
        self.assertEqual(state["replies"][0]["output_message_id"], completed["message_id"])
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

        provider_output = copy.deepcopy(output)
        provider_output.replace_header("Message-ID", "<provider-result@qq.com>")
        self.mailbox.add(12, provider_output)
        self.mailbox.add(
            13,
            user_reply(
                "<provider-result@qq.com>",
                message_id="<user-followup@qq.com>",
                body="现在继续下一步。",
                references=[WIRE_A, claim["message_id"], "<provider-result@qq.com>"],
            ),
        )
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        followup = self.operation("claim_email_reply")
        self.assertEqual(followup["body"], "现在继续下一步。")
        self.assertEqual(followup["thread_id"], THREAD_A)

    def test_completion_retry_after_success_does_not_send_another_email(self) -> None:
        claim = self.enqueue_and_claim()
        completed = self.complete(claim)
        self.restart()
        duplicate = self.complete(claim)
        self.assertEqual(duplicate["status"], "completed")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["message_id"], completed["message_id"])
        self.assertEqual(len(self.transport.messages), 2)
        with self.assertRaises(ValueError):
            self.complete(claim, "改变结果正文不能复用同一次完成提交")

    def test_pending_completion_can_retry_saved_result_without_body_after_restart(self) -> None:
        claim = self.enqueue_and_claim()
        result_body = "保留原始结果的第一段。\n\n第二段含中文和空行。\n"
        self.transport.failure = DeliveryFailure("failed", "smtp_authentication_failed")
        first = self.complete(claim, result_body)
        self.assertEqual(first["status"], "result_pending")
        attempted_mail = self.transport.messages[-1].as_bytes()

        self.transport.failure = None
        self.restart()
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")
        retried = self.operation(
            "complete_email_reply",
            reply_id=claim["reply_id"],
            claim_token=claim["claim_token"],
        )
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["message_id"], first["submission"]["message_id"])
        self.assertEqual(self.transport.messages[-1].as_bytes(), attempted_mail)
        self.assertEqual(len(self.transport.messages), 3)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

    def test_first_completion_requires_result_body_and_keeps_the_claim_on_rejection(self) -> None:
        claim = self.enqueue_and_claim()
        with self.assertRaises(ValueError):
            self.operation(
                "complete_email_reply",
                reply_id=claim["reply_id"],
                claim_token=claim["claim_token"],
            )
        self.assertEqual(len(self.transport.messages), 1)
        state = self.operation("get_email_reply_status")
        self.assertEqual(state["replies"][0]["state"], "claimed")
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")

    def test_completed_reply_clears_bodies_but_retains_conflict_detection_and_idempotency(self) -> None:
        claim = self.enqueue_and_claim()
        result_body = "这次工作已完成，验证通过。"
        first = self.complete(claim, result_body)
        self.assertEqual(first["status"], "completed")
        self.restart()
        duplicate = self.operation(
            "complete_email_reply",
            reply_id=claim["reply_id"],
            claim_token=claim["claim_token"],
        )
        self.assertEqual(duplicate["status"], "completed")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["message_id"], first["message_id"])
        with self.assertRaises(ValueError):
            self.complete(claim, "与已经发送的结果不相同")
        self.assertEqual(self.complete(claim, result_body)["status"], "completed")
        self.assertEqual(len(self.transport.messages), 2)
        with closing(sqlite3.connect(self.state_dir / "replies.sqlite3")) as db:
            saved = db.execute(
                "SELECT body,result_body,result_digest FROM replies WHERE reply_id=?",
                (claim["reply_id"],),
            ).fetchone()
        self.assertIsNone(saved[0])
        self.assertIsNone(saved[1])
        self.assertEqual(saved[2], hashlib.sha256(result_body.encode()).hexdigest())

    def test_failed_result_delivery_retries_only_email_with_the_same_identity(self) -> None:
        claim = self.enqueue_and_claim()
        self.transport.failure = DeliveryFailure("failed", "smtp_authentication_failed")
        first = self.complete(claim)
        self.assertEqual(first["status"], "result_pending")
        self.assertEqual(first["submission"]["status"], "failed")
        attempted = self.transport.messages[-1].as_bytes()
        self.restart()
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")
        with self.assertRaises(ValueError):
            self.complete(claim, "更换原始结果")
        self.transport.failure = None
        retried = self.complete(claim)
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["message_id"], first["submission"]["message_id"])
        self.assertEqual(self.transport.messages[-1].as_bytes(), attempted)
        self.assertEqual(len(self.transport.messages), 3)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

    def test_unknown_result_delivery_blocks_reexecution_and_automatic_resend(self) -> None:
        claim = self.enqueue_and_claim()
        self.transport.failure = DeliveryFailure("unknown", "smtp_reply_lost")
        first = self.complete(claim)
        self.assertEqual(first["status"], "result_pending")
        self.assertEqual(first["submission"]["status"], "unknown")
        self.transport.failure = None
        self.restart()
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")
        second = self.complete(claim)
        self.assertEqual(second["status"], "result_pending")
        self.assertEqual(second["submission"]["status"], "unknown")
        self.assertEqual(len(self.transport.messages), 2)
        state = self.operation("get_email_reply_status")
        output_event_id = state["replies"][0]["output_event_id"]
        self.mail.handle(
            "resolve_email_submission",
            {"thread_id": THREAD_A, "event_id": output_event_id, "resolution": "received"},
        )
        self.assertEqual(self.complete(claim)["status"], "completed")
        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.operation("claim_email_reply")["status"], "empty")

    def test_wrong_claim_or_task_cannot_complete_someone_elses_reply(self) -> None:
        claim = self.enqueue_and_claim()
        self.mail.handle(
            "configure_email_thread",
            {"thread_id": THREAD_B, "subject": "另一个任务", "notification_mode": "long_tasks"},
        )
        for thread_id, reply_id, token in (
            (THREAD_B, claim["reply_id"], claim["claim_token"]),
            (THREAD_A, EVENT_B, claim["claim_token"]),
            (THREAD_A, claim["reply_id"], EVENT_B),
        ):
            with self.subTest(thread_id=thread_id, reply_id=reply_id, token=token):
                with self.assertRaises(ValueError):
                    self.operation(
                        "complete_email_reply",
                        thread_id=thread_id,
                        reply_id=reply_id,
                        claim_token=token,
                        result_body="不能发送错误任务的结果",
                    )
        self.assertEqual(len(self.transport.messages), 1)
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")

    def test_mailbox_uidvalidity_change_disables_intake_until_new_baseline(self) -> None:
        self.enable()
        self.mailbox.uidvalidity = "test-mailbox-generation-2"
        self.mailbox.add(11, user_reply(WIRE_A))
        reset = self.operation("poll_email_replies")
        self.assertEqual(reset["status"], "mailbox_reset")
        self.assertTrue(reset["requires_reenable"])
        self.assertEqual(self.operation("get_email_reply_status")["status"], "disabled")
        self.assertEqual(self.operation("claim_email_reply")["status"], "disabled")
        self.assertEqual(self.enable()["baseline_uid"], 11)
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 0)
        self.mailbox.add(12, user_reply(WIRE_A, message_id="<after-reset@qq.com>"))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)

    def test_retry_recovers_an_exact_scanned_reply_after_parser_rejection_is_fixed(self) -> None:
        self.enable()
        self.mailbox.add(11, user_reply(WIRE_A, body="恢复这条此前被误拒的测试指令。"))
        with patch(
            "qq_reply_service.parse_reply",
            side_effect=ReplyRejected("test_parser_rejection", "Simulated older parser rejection"),
        ):
            rejected = self.operation("poll_email_replies")
        self.assertEqual(rejected["new_replies"], 0)
        self.assertEqual(rejected["rejected_counts"]["test_parser_rejection"], 1)
        self.restart()
        scanned = self.operation("get_email_reply_status")["subscription"]
        self.assertEqual(scanned["baseline_uid"], 10)
        self.assertEqual(scanned["last_uid"], 11)
        recovered = self.operation("retry_email_reply", uid=11)
        self.assertEqual(recovered["status"], "retried")
        self.assertEqual(recovered["new_replies"], 1)
        self.assertEqual(recovered["rejected_counts"], {})
        claim = self.operation("claim_email_reply")
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["message_id"], "<user-reply-a@qq.com>")
        self.assertEqual(claim["body"], "恢复这条此前被误拒的测试指令。")

    def test_retry_cannot_read_at_or_before_baseline_or_unscanned_future(self) -> None:
        self.mailbox.add(9, user_reply(WIRE_A, message_id="<before-baseline@qq.com>"))
        self.enable()
        self.mailbox.add(11, user_reply("<unregistered-parent@qq.com>", message_id="<rejected-scanned@qq.com>"))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 0)
        self.mailbox.add(12, user_reply(WIRE_A, message_id="<not-scanned-yet@qq.com>"))
        self.restart()
        for uid in (9, 10, 12):
            with self.subTest(uid=uid):
                with self.assertRaises(ValueError):
                    self.operation("retry_email_reply", uid=uid)
        state = self.operation("get_email_reply_status")
        self.assertEqual(state["subscription"]["baseline_uid"], 10)
        self.assertEqual(state["subscription"]["last_uid"], 11)
        self.assertEqual(state["replies"], [])
        # The future reply is still eligible for the ordinary next poll.
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        self.assertEqual(self.operation("get_email_reply_status")["subscription"]["baseline_uid"], 10)

    def test_retry_is_idempotent_and_stops_if_the_mailbox_generation_changes(self) -> None:
        self.enable()
        self.mailbox.add(11, user_reply(WIRE_A))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        self.restart()
        retried = self.operation("retry_email_reply", uid=11)
        self.assertEqual(retried["status"], "retried")
        self.assertEqual(retried["new_replies"], 0)
        self.assertEqual(len(self.operation("get_email_reply_status")["replies"]), 1)
        claim = self.operation("claim_email_reply")
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(self.operation("retry_email_reply", uid=11)["new_replies"], 0)
        self.assertEqual(self.operation("claim_email_reply")["status"], "needs_attention")

        self.mailbox.uidvalidity = "replacement-mailbox-generation"
        stopped = self.operation("retry_email_reply", uid=11)
        self.assertEqual(stopped["status"], "mailbox_reset")
        state = self.operation("get_email_reply_status")
        self.assertEqual(state["status"], "disabled")
        self.assertEqual(len(state["replies"]), 1)

    def test_legacy_subscription_migration_does_not_authorize_unrecorded_history(self) -> None:
        # Before baseline_uid existed, only last_uid survived a worker restart.
        self.seed_legacy_subscription(last_uid=11)
        self.mailbox.add(11, user_reply(WIRE_A, message_id="<legacy-scanned-reply@qq.com>"))
        self.restart()
        migrated = self.operation("get_email_reply_status")
        self.assertEqual(migrated["subscription"]["baseline_uid"], 11)
        self.assertEqual(migrated["subscription"]["last_uid"], 11)
        with self.assertRaises(ValueError):
            self.operation("retry_email_reply", uid=11)
        self.assertEqual(self.operation("get_email_reply_status")["replies"], [])
        self.mailbox.add(12, user_reply(self.initial["message_id"], message_id="<new-after-migration@qq.com>"))
        self.assertEqual(self.operation("poll_email_replies")["new_replies"], 1)
        self.assertEqual(self.operation("get_email_reply_status")["subscription"]["baseline_uid"], 11)

    def test_interrupted_legacy_migration_cannot_leave_an_authorizing_zero_baseline(self) -> None:
        self.seed_legacy_subscription(last_uid=11)
        real_connect = sqlite3.connect

        class InterruptedMigrationConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.strip().startswith("UPDATE subscriptions SET baseline_uid=last_uid"):
                    raise sqlite3.OperationalError("simulated interruption while copying the baseline")
                return super().execute(sql, parameters)

        def interrupted_connect(database, *args, **kwargs):
            if Path(database) == self.state_dir / "replies.sqlite3":
                kwargs["factory"] = InterruptedMigrationConnection
            return real_connect(database, *args, **kwargs)

        with patch("qq_reply_service.sqlite3.connect", side_effect=interrupted_connect):
            with self.assertRaises(sqlite3.OperationalError):
                self.operation("get_email_reply_status")
        self.restart()
        recovered = self.operation("get_email_reply_status")
        self.assertEqual(recovered["subscription"]["baseline_uid"], 11)
        with self.assertRaises(ValueError):
            self.operation("retry_email_reply", uid=11)

    def seed_legacy_subscription(self, *, last_uid: int) -> None:
        with closing(sqlite3.connect(self.state_dir / "replies.sqlite3")) as db:
            db.execute(
                "CREATE TABLE subscriptions (thread_id TEXT PRIMARY KEY, mail_thread_id TEXT NOT NULL, "
                "uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL, enabled INTEGER NOT NULL)"
            )
            db.execute(
                "INSERT INTO subscriptions VALUES (?,?,?,?,?)",
                (THREAD_A, self.initial["mail_thread_id"], self.mailbox.uidvalidity, last_uid, 1),
            )
            db.commit()


if __name__ == "__main__":
    unittest.main()
