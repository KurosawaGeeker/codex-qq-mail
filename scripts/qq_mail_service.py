#!/usr/bin/env python3
"""One-request local mail worker. Only this process accesses Keychain and SMTP."""

import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import smtplib
import sqlite3
import ssl
import subprocess
import sys
from contextlib import closing
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate
from uuid import UUID, uuid4

from qq_mail_config import ACCOUNT, KEYCHAIN_SERVICE, STATE_DIR, require_account
MAX_BODY_CHARS = 1_000_000
MODES = {"long_tasks", "always", "off"}


class DeliveryFailure(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def keychain_credential():
    require_account()
    try:
        credential = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", ACCOUNT,
             "-s", KEYCHAIN_SERVICE, "-w"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.rstrip("\r\n")
        if not credential:
            raise DeliveryFailure("failed", "keychain_unavailable")
    except (subprocess.SubprocessError, OSError):
        raise DeliveryFailure("failed", "keychain_unavailable") from None
    return credential


def smtp_transport(message):
    """A successful DATA response is accepted even if connection cleanup fails."""
    credential = keychain_credential()

    smtp = None
    submitting = False
    try:
        smtp = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20,
                               context=ssl.create_default_context())
        smtp.login(ACCOUNT, credential)
        credential = None
        submitting = True
        refused = smtp.send_message(message)
        if refused:
            raise DeliveryFailure("failed", "recipient_rejected")
    except smtplib.SMTPAuthenticationError:
        raise DeliveryFailure("failed", "smtp_authentication_failed") from None
    except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
            smtplib.SMTPDataError, smtplib.SMTPNotSupportedError):
        raise DeliveryFailure("failed", "smtp_rejected") from None
    except (smtplib.SMTPException, OSError):
        raise DeliveryFailure("unknown" if submitting else "failed",
                              "smtp_result_unknown" if submitting else "smtp_connection_failed") from None
    finally:
        credential = None
        if smtp is not None:
            # Do not use the context manager: QUIT failure cannot undo DATA acceptance.
            try:
                smtp.close()
            except OSError:
                pass  # Socket cleanup does not alter the submission result.


def uuid_string(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UUID")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError(f"{name} must be a UUID") from None


def subject_string(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("subject must contain 1-160 characters")
    if "\r" in value or "\n" in value:
        raise ValueError("subject cannot contain line breaks")
    return value.strip()


class MailService:
    def __init__(self, state_dir=STATE_DIR, transport=smtp_transport):
        self.state_dir = Path(state_dir)
        require_account(self.state_dir)
        self.transport = transport
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)

    def handle(self, operation, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        allowed = {
            "configure_email_thread": {"thread_id", "subject", "notification_mode"},
            "get_email_thread": {"thread_id"},
            "resolve_email_submission": {"thread_id", "event_id", "resolution"},
            "record_delivery_id": {"thread_id", "event_id", "provider_message_id"},
            "send_completion_email": {"thread_id", "event_id", "body", "subject",
                                      "elapsed_seconds", "send_now", "parent_message_id", "parent_references"},
        }
        if operation not in allowed:
            raise ValueError("unknown operation")
        if set(arguments) - allowed[operation]:
            raise ValueError("unsupported arguments")
        thread_id = uuid_string(arguments.get("thread_id"), "thread_id")
        with (self.state_dir / "service.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "code": "retry_same_event_id"}
            db_path = self.state_dir / "state.sqlite3"
            with closing(sqlite3.connect(db_path, timeout=5)) as db:
                os.chmod(db_path, 0o600)
                db.row_factory = sqlite3.Row
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS threads (
                        thread_id TEXT PRIMARY KEY,
                        mail_thread_id TEXT UNIQUE NOT NULL,
                        subject TEXT NOT NULL,
                        notification_mode TEXT NOT NULL,
                        last_message_id TEXT
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        thread_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        message_id TEXT UNIQUE NOT NULL,
                        digest TEXT NOT NULL,
                        raw_message BLOB,
                        status TEXT NOT NULL,
                        error_code TEXT,
                        refs TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (thread_id, event_id)
                    );
                    CREATE TABLE IF NOT EXISTS delivery_aliases (
                        provider_message_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        event_id TEXT NOT NULL
                    );
                """)
                columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
                if "provider_message_id" not in columns:
                    db.execute("ALTER TABLE messages ADD COLUMN provider_message_id TEXT")
                # The process lock means no live sender owns a leftover sending row.
                db.execute("UPDATE messages SET status='unknown', error_code='interrupted_submission' WHERE status='sending'")
                db.commit()
                thread = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
                if operation == "get_email_thread":
                    return self._thread_result(db, thread) if thread else {"status": "not_registered", "thread_id": thread_id}
                if operation == "resolve_email_submission":
                    return self._resolve(db, thread_id, arguments)
                if operation == "record_delivery_id":
                    event_id = uuid_string(arguments.get("event_id"), "event_id")
                    provider_id = self._header_id(arguments.get("provider_message_id"))
                    row = db.execute("SELECT * FROM messages WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
                    if row is None or row["status"] not in {"accepted", "unknown"}:
                        raise ValueError("delivery must reference a submitted message")
                    alias = db.execute("SELECT thread_id,event_id FROM delivery_aliases WHERE provider_message_id=?", (provider_id,)).fetchone()
                    if alias and (alias["thread_id"], alias["event_id"]) != (thread_id, event_id):
                        raise ValueError("provider message ID is already bound to another event")
                    db.execute("INSERT OR IGNORE INTO delivery_aliases VALUES (?,?,?)", (provider_id, thread_id, event_id))
                    db.execute("UPDATE messages SET provider_message_id=COALESCE(provider_message_id,?) WHERE thread_id=? AND event_id=?", (provider_id, thread_id, event_id))
                    db.commit()
                    return {"status": "recorded", "provider_message_id": provider_id, "thread_id": thread_id, "event_id": event_id}
                if operation == "configure_email_thread":
                    mode = arguments.get("notification_mode")
                    if mode not in MODES:
                        raise ValueError("notification_mode must be long_tasks, always, or off")
                    if thread is None:
                        thread = self._create_thread(db, thread_id, arguments.get("subject"), mode)
                    else:
                        if "subject" in arguments:
                            subject_string(arguments["subject"])
                        db.execute("UPDATE threads SET notification_mode=? WHERE thread_id=?", (mode, thread_id))
                        db.commit()
                        thread = db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
                    return self._thread_result(db, thread)
                return self._send(db, thread_id, thread, arguments)

    def _resolve(self, db, thread_id, args):
        event_id = uuid_string(args.get("event_id"), "event_id")
        resolution = args.get("resolution")
        if resolution not in {"received", "retry_authorized"}:
            raise ValueError("resolution must be received or retry_authorized")
        row = db.execute("SELECT * FROM messages WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
        if row is None or row["status"] != "unknown":
            raise ValueError("only an unknown submission can be resolved")
        status = "accepted" if resolution == "received" else "prepared"
        if resolution == "received":
            db.execute("UPDATE messages SET status='accepted',error_code='user_confirmed_received',raw_message=NULL WHERE thread_id=? AND event_id=?", (thread_id, event_id))
            db.execute("UPDATE threads SET last_message_id=? WHERE thread_id=?", (row["message_id"], thread_id))
        else:
            db.execute("UPDATE messages SET status='prepared',error_code='user_authorized_retry' WHERE thread_id=? AND event_id=?", (thread_id, event_id))
        db.commit()
        return {"status": "resolved", "submission_status": status, "thread_id": thread_id,
                "event_id": event_id, "message_id": row["message_id"]}

    def _create_thread(self, db, thread_id, subject, mode="long_tasks"):
        title = subject_string(subject)
        mail_thread_id = str(uuid4())
        stable_subject = f"[Codex {mail_thread_id[:8]}] {title}"
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, NULL)",
                   (thread_id, mail_thread_id, stable_subject, mode))
        db.commit()
        return db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()

    def _thread_result(self, db, thread):
        result = dict(thread)
        result["status"] = "registered"
        result["messages"] = [dict(row) for row in db.execute(
            "SELECT event_id, message_id, provider_message_id, status, error_code, created_at FROM messages WHERE thread_id=? ORDER BY rowid",
            (thread["thread_id"],))]
        return result

    @staticmethod
    def _header_id(value):
        if not isinstance(value, str) or len(value) > 998 or not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", value):
            raise ValueError("invalid parent or provider Message-ID")
        return value

    @staticmethod
    def _wire_id(db, message_id):
        row = db.execute("SELECT provider_message_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return row["provider_message_id"] if row and row["provider_message_id"] else message_id

    def _send(self, db, thread_id, thread, args):
        event_id = uuid_string(args.get("event_id"), "event_id")
        body = args.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
            raise ValueError("body must contain 1-1000000 characters")
        elapsed = args.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_seconds must be a finite nonnegative number")
        send_now = args.get("send_now", False)
        if not isinstance(send_now, bool):
            raise ValueError("send_now must be a boolean")
        if "subject" in args:
            subject_string(args["subject"])
        parent_id = args.get("parent_message_id")
        supplied_refs = args.get("parent_references", [])
        if parent_id is not None:
            self._header_id(parent_id)
        if not isinstance(supplied_refs, list) or len(supplied_refs) > 1000:
            raise ValueError("invalid parent_references")
        for reference in supplied_refs:
            self._header_id(reference)
        if supplied_refs and parent_id is None:
            raise ValueError("parent_references requires parent_message_id")
        if thread is None:
            thread = self._create_thread(db, thread_id, args.get("subject"))
        # The first subject is canonical; future outcome text belongs in the body.
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        existing = db.execute("SELECT * FROM messages WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
        if existing is not None:
            if existing["digest"] != digest:
                raise ValueError("event_id was already used with different content")
            if existing["status"] in {"accepted", "unknown"}:
                return self._message_result(thread, existing, duplicate=True)
        uncertain = db.execute("SELECT * FROM messages WHERE thread_id=? AND status='unknown' LIMIT 1", (thread_id,)).fetchone()
        if uncertain is not None:
            result = self._message_result(thread, uncertain)
            result["code"] = "previous_submission_needs_verification"
            return result
        pending = db.execute("SELECT * FROM messages WHERE thread_id=? AND status IN ('failed','prepared') AND event_id<>? LIMIT 1", (thread_id, event_id)).fetchone()
        if pending is not None:
            result = self._message_result(thread, pending)
            result["code"] = "previous_submission_needs_retry"
            return result
        mode = thread["notification_mode"]
        if not (send_now or mode == "always" or (mode == "long_tasks" and elapsed > 600)):
            return {"status": "skipped", "thread_id": thread_id, "mail_thread_id": thread["mail_thread_id"],
                    "notification_mode": mode, "reason": "notifications_off" if mode == "off" else "under_threshold"}
        if existing is None:
            message_id = f"<{uuid4()}@codex-mail.local>"
            message = EmailMessage(policy=policy.SMTP)
            message["From"] = ACCOUNT
            message["To"] = ACCOUNT
            message["Subject"] = thread["subject"]
            message["Date"] = formatdate(localtime=True)
            message["Message-ID"] = message_id
            message["X-Codex-Mail-Thread-ID"] = thread["mail_thread_id"]
            message["X-Codex-Thread-ID"] = thread_id
            message["X-Codex-Event-ID"] = event_id
            message["Auto-Submitted"] = "auto-generated"
            refs = []
            if parent_id:
                refs = list(dict.fromkeys(supplied_refs + [parent_id]))
                message["In-Reply-To"] = parent_id
                message["References"] = " ".join(refs)
            elif thread["last_message_id"]:
                parent = db.execute("SELECT refs FROM messages WHERE message_id=?", (thread["last_message_id"],)).fetchone()
                refs = [self._wire_id(db, ref) for ref in json.loads(parent["refs"])] + [self._wire_id(db, thread["last_message_id"])]
                message["In-Reply-To"] = self._wire_id(db, thread["last_message_id"])
                message["References"] = " ".join(refs)
            message.set_content(body)
            db.execute("INSERT INTO messages (thread_id,event_id,message_id,digest,raw_message,status,refs) VALUES (?,?,?,?,?,'prepared',?)",
                       (thread_id, event_id, message_id, digest, message.as_bytes(), json.dumps(refs)))
            db.commit()
        row = db.execute("SELECT * FROM messages WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
        message = BytesParser(policy=policy.SMTP).parsebytes(row["raw_message"])
        db.execute("UPDATE messages SET status='sending',error_code=NULL WHERE thread_id=? AND event_id=?", (thread_id, event_id))
        db.commit()
        try:
            self.transport(message)
        except DeliveryFailure as exc:
            status = exc.status if exc.status in {"failed", "unknown"} else "unknown"
            db.execute("UPDATE messages SET status=?,error_code=? WHERE thread_id=? AND event_id=?",
                       (status, exc.code, thread_id, event_id))
            db.commit()
        except Exception:
            # An unexpected transport exception may occur after DATA acceptance.
            db.execute("UPDATE messages SET status='unknown',error_code='unexpected_transport_error' WHERE thread_id=? AND event_id=?", (thread_id, event_id))
            db.commit()
        else:
            db.execute("UPDATE messages SET status='accepted',error_code=NULL,raw_message=NULL WHERE thread_id=? AND event_id=?", (thread_id, event_id))
            db.execute("UPDATE threads SET last_message_id=? WHERE thread_id=?", (row["message_id"], thread_id))
            db.commit()
        row = db.execute("SELECT * FROM messages WHERE thread_id=? AND event_id=?", (thread_id, event_id)).fetchone()
        return self._message_result(thread, row, duplicate=existing is not None)

    @staticmethod
    def _message_result(thread, row, duplicate=False):
        return {"status": row["status"], "thread_id": thread["thread_id"],
                "mail_thread_id": thread["mail_thread_id"], "message_id": row["message_id"],
                "event_id": row["event_id"], "duplicate": duplicate, "code": row["error_code"]}


def main():
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.read(8_100_000))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = request.get("operation")
        if operation in {"configure_email_listener", "get_email_listener_status"}:
            from qq_listener_control import handle
            result = handle(operation, request.get("arguments", {}))
        elif operation in {"enable_email_replies", "disable_email_replies", "poll_email_replies", "retry_email_reply", "claim_email_reply", "complete_email_reply", "get_email_reply_status"}:
            from qq_reply_service import ReplyService
            result = ReplyService().handle(operation, request.get("arguments", {}))
        else:
            result = MailService().handle(operation, request.get("arguments", {}))
    except (ValueError, TypeError) as exc:
        result = {"status": "invalid_request", "message": str(exc)}
    except Exception as exc:
        result = {"status": "service_error", "code": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
