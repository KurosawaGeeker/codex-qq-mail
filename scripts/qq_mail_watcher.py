"""Wait for QQ Sent mailbox changes without downloading mail or calling a model."""

import math
import time

from qq_reply_service import QQMailbox


class MailWatchError(RuntimeError):
    """A disconnected watcher can be retried after the caller's backoff."""


class MailWatcher:
    """Keep one read-only IMAP connection; bound each idle interval to 30 seconds.

    A new connection always reports a change so the caller can catch up using
    its durable intake cursor. Only the reply service decides which messages
    are authorized; this class observes mailbox metadata only.
    """

    def __init__(self, mailbox_factory=QQMailbox, sleep=time.sleep):
        self._mailbox_factory = mailbox_factory
        self._sleep = sleep
        self._mailbox = None
        self._snapshot = None
        self._supports_idle = False

    @property
    def mode(self):
        return 'disconnected' if self._mailbox is None else ('idle' if self._supports_idle else 'poll')

    def wait(self, timeout=30):
        """Return True on connect/mailbox change, or False after quiet waiting.

        Network failures close the connection and raise a sanitized error. A
        subsequent call reconnects and returns True for a catch-up scan. IMAP
        commands retain QQMailbox's finite socket timeout; the IDLE/poll wait
        itself never exceeds 30 seconds.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        duration = min(float(timeout), 30.0)
        try:
            if self._mailbox is None:
                self._mailbox = self._mailbox_factory()
                self._snapshot = self._read_snapshot()
                client = self._mailbox.client
                capabilities = {
                    item.decode("ascii", "replace").upper()
                    if isinstance(item, bytes) else str(item).upper()
                    for item in client.capabilities
                }
                self._supports_idle = "IDLE" in capabilities and callable(getattr(client, "idle", None))
                return True

            client = self._mailbox.client
            if self._supports_idle:
                # Public Python 3.14 API handles IDLE/DONE and socket timeouts.
                with client.idle(duration=duration) as responses:
                    for kind, _ in responses:
                        kind = kind.decode("ascii", "replace") if isinstance(kind, bytes) else kind
                        if kind.upper() == "BYE":
                            raise MailWatchError("imap_watch_disconnected")
                        if kind.upper() in {"EXISTS", "RECENT", "EXPUNGE", "FETCH"}:
                            break
            else:
                self._sleep(duration)
                status, _ = client.noop()
                if status != "OK":
                    raise MailWatchError("imap_watch_disconnected")

            # Refresh UIDNEXT even after an IDLE timeout: a server notification
            # can race with leaving IDLE. Metadata changes are not instructions.
            current = self._read_snapshot()
            changed = current != self._snapshot
            self._snapshot = current
            return changed
        except Exception:
            self.close()
            # Keep transport/authentication details out of daemon logs.
            raise MailWatchError("imap_watch_failed") from None

    def _read_snapshot(self):
        snapshot = self._mailbox.snapshot()
        return str(snapshot["uidvalidity"]), int(snapshot["last_uid"])

    def close(self):
        """Release the selected mailbox/socket; safe to call repeatedly."""
        mailbox = self._mailbox
        self._mailbox = None
        self._snapshot = None
        self._supports_idle = False
        if mailbox is not None:
            try:
                mailbox.close()
            except Exception:
                # QQMailbox normally handles this; injected adapters may not.
                try:
                    mailbox.client.shutdown()
                except Exception:
                    pass
