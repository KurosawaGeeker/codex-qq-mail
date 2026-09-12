"""Read only the account's Sent mailbox and queue verified task replies locally."""

import fcntl
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
from contextlib import closing
from email import policy
from email.parser import BytesParser
from uuid import uuid4

from qq_mail_service import ACCOUNT, STATE_DIR, MailService, keychain_credential, uuid_string
from qq_mail_config import require_account
from qq_reply_parser import OutboundTarget, ReplyRejected, VerifiedIdentity, normalize_message_id, parse_reply

HEADERS = "MESSAGE-ID FROM TO SUBJECT IN-REPLY-TO REFERENCES AUTO-SUBMITTED X-CODEX-MAIL-THREAD-ID X-CODEX-THREAD-ID X-CODEX-EVENT-ID"
MAX_BYTES = 5_000_000


class QQMailbox:
    """Authenticated, read-only QQ Sent mailbox; no inbox command intake."""
    def __init__(self):
        require_account()
        credential = keychain_credential()
        self.client = imaplib.IMAP4_SSL("imap.qq.com", 993, ssl_context=ssl.create_default_context(), timeout=15)
        try:
            self.client.login(ACCOUNT, credential)
            credential = None
            status, _ = self.client.select('"Sent Messages"', readonly=True)
            if status != "OK":
                raise RuntimeError("sent_mailbox_unavailable")
        except Exception:
            self.close()
            raise
        finally:
            credential = None
        self.known_ids = set()
        self.mail_thread_id = None
        self.sizes = {}
        self.rejections = {}

    def set_targets(self, ids, mail_thread_id):
        self.known_ids = set(ids)
        self.mail_thread_id = mail_thread_id

    def snapshot(self):
        # SELECT is read-only and refreshes UIDNEXT/UIDVALIDITY after each poll.
        status, _ = self.client.select('"Sent Messages"', readonly=True)
        if status != "OK":
            raise RuntimeError("sent_mailbox_unavailable")
        validity = self.client.response("UIDVALIDITY")[1][0]
        next_uid = self.client.response("UIDNEXT")[1][0]
        return {"uidvalidity": validity.decode() if isinstance(validity, bytes) else str(validity), "last_uid": int(next_uid) - 1}

    def _fetch(self, uid, full=False):
        if full and self.sizes.get(uid, 0) > MAX_BYTES:
            return None
        section = "BODY.PEEK[]" if full else f"BODY.PEEK[HEADER.FIELDS ({HEADERS})]"
        status, response = self.client.uid("fetch", str(uid), f"(RFC822.SIZE {section})")
        if status != "OK":
            raise RuntimeError("imap_fetch_failed")
        for item in response or []:
            if isinstance(item, tuple):
                size_match = re.search(rb"RFC822.SIZE (\d+)", item[0])
                if size_match:
                    self.sizes[uid] = int(size_match.group(1))
                if len(item[1]) > MAX_BYTES:
                    return None
                return item[1]
        return None

    def find_notifications(self, mail_thread_id):
        status, data = self.client.uid("search", None, "HEADER", "X-Codex-Mail-Thread-ID", '"' + mail_thread_id + '"')
        if status != "OK":
            raise RuntimeError("imap_search_failed")
        uids = data[0].split() if data and data[0] else []
        for uid in uids[-256:]:
            headers = self._fetch(int(uid))
            if headers is not None:
                raw = self._fetch(int(uid), full=True)
                if raw is not None:
                    yield int(uid), raw

    def fetch_message(self, uid):
        headers = self._fetch(uid)
        if headers is None:
            return None
        if self.sizes.get(uid, 0) > MAX_BYTES:
            raise ReplyRejected("message_too_large", "Message exceeds the intake size limit")
        return self._fetch(uid, full=True)

    def fetch_since(self, last_uid):
        status, data = self.client.uid("search", None, "UID", f"{last_uid + 1}:*")
        if status != "OK":
            raise RuntimeError("imap_search_failed")
        uids = [int(x) for x in (data[0].split() if data and data[0] else []) if int(x) > last_uid]
        for uid in sorted(uids)[:100]:
            headers = self._fetch(uid)
            if headers is None:
                yield uid, b""
                continue
            message = BytesParser(policy=policy.default).parsebytes(headers)
            ids = set()
            for value in re.findall(r"<[^<>\s]+>", str(message.get("In-Reply-To", "")) + " " + str(message.get("References", ""))):
                try:
                    ids.add(normalize_message_id(value))
                except ReplyRejected:
                    continue
            is_our_notification = str(message.get("X-Codex-Mail-Thread-ID", "")) == self.mail_thread_id
            if ids & self.known_ids or is_our_notification:
                raw = self._fetch(uid, full=True)
                if raw is None and self.sizes.get(uid, 0) > MAX_BYTES:
                    self.rejections[uid] = "message_too_large"
                yield uid, raw or b""
            else:
                # Advance over unrelated messages without downloading their body.
                yield uid, b""

    def close(self):
        try:
            self.client.logout()
        except (imaplib.IMAP4.error, OSError):
            # No mailbox mutation to commit. Close the underlying socket on logout failure.
            try:
                self.client.shutdown()
            except OSError:
                pass


class ReplyService:
    def __init__(self, state_dir=STATE_DIR, mailbox_factory=QQMailbox, mail_service=None):
        self.state_dir = Path(state_dir)
        require_account(self.state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.mailbox_factory = mailbox_factory
        self.mail = mail_service or MailService(self.state_dir)

    def handle(self, operation, args):
        allowed = {
            "enable_email_replies": {"thread_id"}, "disable_email_replies": {"thread_id"},
            "poll_email_replies": {"thread_id"}, "claim_email_reply": {"thread_id"},
            "get_email_reply_status": {"thread_id"},
            "retry_email_reply": {"thread_id", "uid"},
            "complete_email_reply": {"thread_id", "reply_id", "claim_token", "result_body"},
        }
        if operation not in allowed or not isinstance(args, dict) or set(args) - allowed[operation]:
            raise ValueError("invalid reply operation or arguments")
        thread_id = uuid_string(args.get("thread_id"), "thread_id")
        thread = self.mail.handle("get_email_thread", {"thread_id": thread_id})
        if thread["status"] != "registered":
            return thread
        with (self.state_dir / "replies.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            with closing(sqlite3.connect(self.state_dir / "replies.sqlite3")) as db:
                os.chmod(self.state_dir / "replies.sqlite3", 0o600)
                db.row_factory = sqlite3.Row
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        thread_id TEXT PRIMARY KEY, mail_thread_id TEXT NOT NULL,
                        uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL, enabled INTEGER NOT NULL,
                        baseline_uid INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS replies (
                        reply_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                        message_id TEXT UNIQUE NOT NULL, body TEXT,
                        refs TEXT NOT NULL, state TEXT NOT NULL,
                        claim_token TEXT, output_event_id TEXT NOT NULL,
                        result_body TEXT, output_message_id TEXT,
                        result_digest TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS reply_rejections (
                        thread_id TEXT NOT NULL, uid INTEGER NOT NULL, code TEXT NOT NULL,
                        PRIMARY KEY(thread_id,uid)
                    );
                """)
                if "result_digest" not in {row[1] for row in db.execute("PRAGMA table_info(replies)")}:
                    db.execute("ALTER TABLE replies ADD COLUMN result_digest TEXT")
                    db.commit()
                if "baseline_uid" not in {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("ALTER TABLE subscriptions ADD COLUMN baseline_uid INTEGER NOT NULL DEFAULT 0")
                    # Old databases did not retain the original baseline. Do not
                    # assume any previously scanned mail is authorized for retry.
                    db.execute("UPDATE subscriptions SET baseline_uid=last_uid")
                    db.commit()
                subscription = db.execute("SELECT * FROM subscriptions WHERE thread_id=?", (thread_id,)).fetchone()
                if operation == "get_email_reply_status":
                    return self._status(db, thread_id, subscription)
                if operation == "disable_email_replies":
                    db.execute("UPDATE subscriptions SET enabled=0 WHERE thread_id=?", (thread_id,)); db.commit()
                    return {"status": "disabled", "thread_id": thread_id}
                if operation == "enable_email_replies":
                    if subscription and subscription["enabled"]:
                        return self._status(db, thread_id, subscription)
                    mailbox = self.mailbox_factory()
                    try:
                        snapshot = mailbox.snapshot()
                        for _, raw in mailbox.find_notifications(thread["mail_thread_id"]):
                            self._reconcile(thread, raw)
                    finally:
                        mailbox.close()
                    db.execute("INSERT INTO subscriptions (thread_id,mail_thread_id,uidvalidity,last_uid,enabled,baseline_uid) VALUES (?,?,?,?,1,?) ON CONFLICT(thread_id) DO UPDATE SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid,baseline_uid=excluded.baseline_uid,enabled=1",
                               (thread_id, thread["mail_thread_id"], str(snapshot["uidvalidity"]), snapshot["last_uid"], snapshot["last_uid"]))
                    db.commit()
                    return {"status": "enabled", "thread_id": thread_id, "baseline_uid": snapshot["last_uid"]}
                if operation == "complete_email_reply":
                    return self._complete(db, thread_id, args)
                if subscription is None or not subscription["enabled"]:
                    return {"status": "disabled", "thread_id": thread_id}
                if operation == "claim_email_reply":
                    return self._claim(db, thread_id)
                if operation == "retry_email_reply":
                    return self._retry(db, thread, subscription, args.get("uid"))
                return self._poll(db, thread, subscription)

    def _outbound(self, thread_id):
        with closing(sqlite3.connect(self.state_dir / "state.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            records = [dict(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=?", (thread_id,))]
            aliases = [dict(row) for row in db.execute("SELECT * FROM delivery_aliases WHERE thread_id=?", (thread_id,))]
        return records, aliases

    def _reconcile(self, thread, raw):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if str(message.get("Auto-Submitted", "")).lower() != "auto-generated":
            return False
        if str(message.get("X-Codex-Mail-Thread-ID", "")) != thread["mail_thread_id"] or str(message.get("X-Codex-Thread-ID", "")) != thread["thread_id"]:
            return False
        if str(message.get("From", "")) != ACCOUNT or str(message.get("To", "")) != ACCOUNT:
            return False
        try:
            provider_id = normalize_message_id(str(message.get("Message-ID", "")))
            part = message.get_body(preferencelist=("plain",))
            if part is None:
                return False
            body = part.get_content().replace("\r\n", "\n")
        except (ReplyRejected, UnicodeError, LookupError):
            return False
        possible_digests = {hashlib.sha256(text.encode()).hexdigest() for text in (body, body.rstrip("\n"))}
        records, _ = self._outbound(thread["thread_id"])
        event_id = str(message.get("X-Codex-Event-ID", ""))
        matches = [record for record in records if record["status"] in {"accepted", "unknown"}
                   and record["digest"] in possible_digests and (not event_id or record["event_id"] == event_id)]
        if len(matches) != 1:
            return False
        result = self.mail.handle("record_delivery_id", {"thread_id": thread["thread_id"], "event_id": matches[0]["event_id"], "provider_message_id": provider_id})
        if result["status"] != "recorded":
            raise RuntimeError("delivery_id_sync_busy")
        return True

    def _known_ids(self):
        # All tasks must be visible to the parser so mixed-task references fail closed.
        with closing(sqlite3.connect(self.state_dir / "state.sqlite3")) as db:
            records = db.execute("SELECT m.message_id,m.thread_id,t.mail_thread_id FROM messages m JOIN threads t USING(thread_id) WHERE m.status IN ('accepted','unknown')").fetchall()
            aliases = db.execute("SELECT a.provider_message_id,a.thread_id,t.mail_thread_id FROM delivery_aliases a JOIN threads t USING(thread_id)").fetchall()
        known = {message_id: OutboundTarget(thread_id, mail_thread_id) for message_id, thread_id, mail_thread_id in records + aliases}
        return known

    def _poll(self, db, thread, subscription):
        mailbox = self.mailbox_factory()
        queued = 0
        rejected = {}
        try:
            snapshot = mailbox.snapshot()
            if str(snapshot["uidvalidity"]) != subscription["uidvalidity"]:
                db.execute("UPDATE subscriptions SET enabled=0 WHERE thread_id=?", (thread["thread_id"],)); db.commit()
                return {"status": "mailbox_reset", "requires_reenable": True}
            # Server-rewritten outbound IDs must be known before processing a reply.
            records, _ = self._outbound(thread["thread_id"])
            if any(row["status"] in {"accepted", "unknown"} and not row["provider_message_id"] for row in records):
                for _, raw in mailbox.find_notifications(thread["mail_thread_id"]):
                    self._reconcile(thread, raw)
            known = self._known_ids()
            if hasattr(mailbox, "set_targets"):
                mailbox.set_targets({key for key, target in known.items() if target.codex_thread_id == thread["thread_id"]}, thread["mail_thread_id"])
            seen = {row[0] for row in db.execute("SELECT message_id FROM replies")}
            cursor = subscription["last_uid"]
            for uid, raw in mailbox.fetch_since(subscription["last_uid"]):
                if uid <= cursor:
                    continue
                code = getattr(mailbox, "rejections", {}).get(uid)
                if code:
                    rejected[code] = rejected.get(code, 0) + 1
                    db.execute('INSERT OR REPLACE INTO reply_rejections VALUES (?,?,?)', (thread['thread_id'], uid, code))
                if raw:
                    if self._reconcile(thread, raw):
                        known = self._known_ids()
                        if hasattr(mailbox, "set_targets"):
                            mailbox.set_targets({key for key, target in known.items() if target.codex_thread_id == thread["thread_id"]}, thread["mail_thread_id"])
                    else:
                        try:
                            self._queue_reply(db, thread, raw, known, seen)
                            db.execute('DELETE FROM reply_rejections WHERE thread_id=? AND uid=?', (thread['thread_id'], uid))
                            queued += 1
                        except ReplyRejected as exc:
                            rejected[exc.code] = rejected.get(exc.code, 0) + 1
                            if exc.code != 'duplicate_message':
                                db.execute('INSERT OR REPLACE INTO reply_rejections VALUES (?,?,?)', (thread['thread_id'], uid, exc.code))
                db.execute("UPDATE subscriptions SET last_uid=? WHERE thread_id=?", (uid, thread["thread_id"]))
                db.commit()
                cursor = uid
        finally:
            mailbox.close()
        return {"status": "polled", "thread_id": thread["thread_id"], "new_replies": queued, "rejected_counts": rejected,
                "has_more": cursor < snapshot["last_uid"],
                "queued_count": db.execute("SELECT COUNT(*) FROM replies WHERE thread_id=? AND state='queued'", (thread["thread_id"],)).fetchone()[0]}

    def _queue_reply(self, db, thread, raw, known, seen):
        aliases_path = self.state_dir / 'account-aliases.json'
        aliases = json.loads(aliases_path.read_text()).get('sender_aliases', []) if aliases_path.exists() else []
        if not isinstance(aliases, list) or len(aliases) > 16 or any(
            not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9._+-]+@qq\.com', item) for item in aliases
        ):
            raise ValueError('invalid local QQ sender aliases')
        parsed = parse_reply(raw, known, identity=VerifiedIdentity(
            source="sent_folder", account=ACCOUNT, verified=True,
            message_sha256=hashlib.sha256(raw).hexdigest()), seen_message_ids=seen, sender_aliases=aliases)
        if parsed.codex_thread_id != thread["thread_id"]:
            raise ReplyRejected("other_task", "This reply belongs to another task")
        refs_message = BytesParser(policy=policy.default).parsebytes(raw)
        refs = re.findall(r"<[^<>\s]+>", str(refs_message.get("References", "")))
        if not refs:
            refs = [parsed.matched_outbound_message_id]
        db.execute("INSERT INTO replies (reply_id,thread_id,message_id,body,refs,state,output_event_id) VALUES (?,?,?,?,?,'queued',?)",
                   (str(uuid4()), thread["thread_id"], parsed.message_id, parsed.body, json.dumps(refs), str(uuid4())))
        seen.add(parsed.message_id)

    def _retry(self, db, thread, subscription, uid):
        if isinstance(uid, bool) or not isinstance(uid, int) or not subscription["baseline_uid"] < uid <= subscription["last_uid"]:
            raise ValueError("uid must be a previously scanned message after the enable baseline")
        mailbox = self.mailbox_factory()
        queued = 0
        rejected = {}
        try:
            snapshot = mailbox.snapshot()
            if str(snapshot["uidvalidity"]) != subscription["uidvalidity"]:
                db.execute("UPDATE subscriptions SET enabled=0 WHERE thread_id=?", (thread["thread_id"],))
                db.commit()
                return {"status": "mailbox_reset", "requires_reenable": True}
            try:
                raw = mailbox.fetch_message(uid)
                if not raw:
                    raise ReplyRejected("message_unavailable", "The selected message is unavailable")
                known = self._known_ids()
                seen = {row[0] for row in db.execute("SELECT message_id FROM replies")}
                self._queue_reply(db, thread, raw, known, seen)
                db.execute('DELETE FROM reply_rejections WHERE thread_id=? AND uid=?', (thread['thread_id'], uid))
                db.commit()
                queued = 1
            except ReplyRejected as exc:
                rejected[exc.code] = 1
                if exc.code != 'duplicate_message':
                    db.execute('INSERT OR REPLACE INTO reply_rejections VALUES (?,?,?)', (thread['thread_id'], uid, exc.code))
                    db.commit()
        finally:
            mailbox.close()
        return {"status": "retried", "thread_id": thread["thread_id"], "new_replies": queued, "rejected_counts": rejected,
                "queued_count": db.execute("SELECT COUNT(*) FROM replies WHERE thread_id=? AND state='queued'", (thread["thread_id"],)).fetchone()[0]}

    def _claim(self, db, thread_id):
        active = db.execute("SELECT reply_id,state,claim_token FROM replies WHERE thread_id=? AND state IN ('claimed','result_pending') LIMIT 1", (thread_id,)).fetchone()
        if active:
            return {"status": "needs_attention", "reply": dict(active), "instruction": "Do not execute the instruction again. Inspect the existing run or retry only result delivery."}
        row = db.execute("SELECT * FROM replies WHERE thread_id=? AND state='queued' ORDER BY rowid LIMIT 1", (thread_id,)).fetchone()
        if row is None:
            return {"status": "empty", "thread_id": thread_id}
        token = str(uuid4())
        db.execute("UPDATE replies SET state='claimed',claim_token=? WHERE reply_id=?", (token, row["reply_id"])); db.commit()
        return {"status": "claimed", "thread_id": thread_id, "reply_id": row["reply_id"], "claim_token": token,
                "message_id": row["message_id"], "body": row["body"], "identity_source": "authenticated_qq_sent_mailbox"}

    def _complete(self, db, thread_id, args):
        reply_id = uuid_string(args.get("reply_id"), "reply_id")
        token = uuid_string(args.get("claim_token"), "claim_token")
        result_body = args.get("result_body")
        row = db.execute("SELECT * FROM replies WHERE thread_id=? AND reply_id=? AND claim_token=?", (thread_id, reply_id, token)).fetchone()
        if row is None or row["state"] not in {"claimed", "result_pending", "completed"}:
            raise ValueError("reply is not owned by this claim")
        if row["state"] == "completed" and result_body is None:
            return {"status": "completed", "reply_id": reply_id, "duplicate": True, "message_id": row["output_message_id"]}
        if result_body is None and row["state"] == "result_pending":
            result_body = row["result_body"]
        if not isinstance(result_body, str) or not result_body.strip() or len(result_body) > 1_000_000:
            raise ValueError("result_body is required for first completion and must contain 1-1000000 characters")
        digest = hashlib.sha256(result_body.encode()).hexdigest()
        original_digest = row["result_digest"] or (hashlib.sha256(row["result_body"].encode()).hexdigest() if row["result_body"] is not None else None)
        if original_digest is not None and original_digest != digest:
            raise ValueError("result delivery retry must preserve result_body")
        if row["state"] == "completed":
            return {"status": "completed", "reply_id": reply_id, "duplicate": True, "message_id": row["output_message_id"]}
        db.execute("UPDATE replies SET state='result_pending',result_body=?,result_digest=? WHERE reply_id=?", (result_body, digest, reply_id)); db.commit()
        submission = self.mail.handle("send_completion_email", {
            "thread_id": thread_id, "event_id": row["output_event_id"], "body": result_body,
            "elapsed_seconds": 0, "send_now": True,
            "parent_message_id": row["message_id"], "parent_references": json.loads(row["refs"]),
        })
        if submission["status"] == "accepted":
            db.execute("UPDATE replies SET state='completed',body=NULL,result_body=NULL,output_message_id=? WHERE reply_id=?", (submission["message_id"], reply_id)); db.commit()
            return {"status": "completed", "reply_id": reply_id, "message_id": submission["message_id"], "duplicate": submission["duplicate"]}
        return {"status": "result_pending", "reply_id": reply_id, "submission": submission, "instruction": "Do not re-execute the email instruction. Resolve or retry only result delivery."}

    @staticmethod
    def _status(db, thread_id, subscription):
        replies = [dict(row) for row in db.execute("SELECT reply_id,message_id,state,claim_token,output_event_id,output_message_id,created_at FROM replies WHERE thread_id=? ORDER BY rowid", (thread_id,))]
        return {"status": "enabled" if subscription and subscription["enabled"] else "disabled", "thread_id": thread_id,
                "rejected_replies": [dict(row) for row in db.execute('SELECT uid,code FROM reply_rejections WHERE thread_id=? ORDER BY uid DESC LIMIT 10', (thread_id,))],
                "subscription": dict(subscription) if subscription else None, "replies": replies}
