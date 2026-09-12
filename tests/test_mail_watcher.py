import unittest

import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_mail_watcher import MailWatcher, MailWatchError


class FakeIdle:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        if self.client.idle_error:
            raise self.client.idle_error
        if self.client.on_idle:
            self.client.on_idle()
        self.client.idle_entered += 1
        return iter(self.client.events)

    def __exit__(self, *args):
        self.client.idle_exited += 1


class FakeClient:
    def __init__(self, capabilities=("IMAP4rev1", "IDLE")):
        self.capabilities = capabilities
        self.events = []
        self.on_idle = None
        self.idle_error = None
        self.idle_durations = []
        self.idle_entered = 0
        self.idle_exited = 0
        self.noop_calls = 0
        self.noop_status = "OK"
        self.shutdown_calls = 0

    def idle(self, duration):
        self.idle_durations.append(duration)
        return FakeIdle(self)

    def noop(self):
        self.noop_calls += 1
        return self.noop_status, [b"OK"]

    def shutdown(self):
        self.shutdown_calls += 1


class FakeMailbox:
    def __init__(self, **client_args):
        self.client = FakeClient(**client_args)
        self.uidvalidity = "123"
        self.last_uid = 10
        self.snapshot_error = None
        self.close_error = None
        self.snapshot_calls = 0
        self.close_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_error:
            raise self.snapshot_error
        return {"uidvalidity": self.uidvalidity, "last_uid": self.last_uid}

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class MailWatcherTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = FakeMailbox()
        self.sleeps = []
        self.watcher = MailWatcher(lambda: self.mailbox, self.sleeps.append)
        self.addCleanup(self.watcher.close)

    def test_first_connection_requests_catch_up_without_waiting(self):
        self.assertTrue(self.watcher.wait())
        self.assertEqual(self.mailbox.snapshot_calls, 1)
        self.assertEqual(self.mailbox.client.idle_durations, [])
        self.assertEqual(self.sleeps, [])

    def test_idle_timeout_does_not_report_mail(self):
        self.watcher.wait()
        self.assertFalse(self.watcher.wait())
        self.assertEqual(self.mailbox.client.idle_durations, [30])
        self.assertEqual(self.mailbox.client.idle_exited, 1)
        self.assertEqual(self.mailbox.client.noop_calls, 0)

    def test_new_message_reports_change_once(self):
        self.watcher.wait()
        self.mailbox.client.events = [("EXISTS", [b"11"])]
        self.mailbox.client.on_idle = lambda: setattr(self.mailbox, "last_uid", 11)
        self.assertTrue(self.watcher.wait())
        self.assertFalse(self.watcher.wait())

    def test_flags_or_expunge_without_new_uids_do_not_report_new_mail(self):
        self.watcher.wait()
        self.mailbox.client.events = [("FETCH", [b"FLAGS"]), ("EXPUNGE", [b"2"])]
        self.assertFalse(self.watcher.wait())

    def test_uidvalidity_change_requests_revalidation(self):
        self.watcher.wait()
        self.mailbox.uidvalidity = "456"
        self.assertTrue(self.watcher.wait())

    def test_catches_uid_change_without_idle_notification(self):
        self.watcher.wait()
        self.mailbox.last_uid = 11
        self.assertTrue(self.watcher.wait())

    def test_without_idle_uses_sleep_noop_and_metadata(self):
        self.mailbox.client.capabilities = ("IMAP4rev1",)
        self.watcher.wait()
        self.assertFalse(self.watcher.wait())
        self.mailbox.last_uid = 11
        self.assertTrue(self.watcher.wait(5))
        self.assertEqual(self.sleeps, [30, 5])
        self.assertEqual(self.mailbox.client.noop_calls, 2)
        self.assertEqual(self.mailbox.client.idle_durations, [])

    def test_missing_public_idle_method_falls_back(self):
        self.mailbox.client.idle = None
        self.watcher.wait()
        self.assertFalse(self.watcher.wait(2))
        self.assertEqual(self.sleeps, [2])
        self.assertEqual(self.mailbox.client.noop_calls, 1)

    def test_wait_duration_is_capped(self):
        self.watcher.wait()
        self.watcher.wait(300)
        self.assertEqual(self.mailbox.client.idle_durations, [30])

    def test_invalid_duration_never_connects(self):
        for timeout in (0, -1, True, "30", None, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.watcher.wait(timeout)
        self.assertEqual(self.mailbox.snapshot_calls, 0)

    def test_disconnect_closes_and_next_wait_reconnects(self):
        self.watcher.wait()
        self.mailbox.client.idle_error = OSError("private server response")
        with self.assertRaisesRegex(MailWatchError, "^imap_watch_failed$"):
            self.watcher.wait()
        self.assertEqual(self.mailbox.close_calls, 1)
        self.mailbox = FakeMailbox()
        self.assertTrue(self.watcher.wait())
        self.assertEqual(self.mailbox.snapshot_calls, 1)

    def test_server_bye_closes_and_sanitizes(self):
        self.watcher.wait()
        self.mailbox.client.events = [("BYE", [b"private text"])]
        with self.assertRaisesRegex(MailWatchError, "^imap_watch_failed$"):
            self.watcher.wait()
        self.assertEqual(self.mailbox.close_calls, 1)
        self.assertEqual(self.mailbox.client.idle_exited, 1)

    def test_failed_connect_snapshot_closes(self):
        self.mailbox.snapshot_error = OSError("private text")
        with self.assertRaisesRegex(MailWatchError, "^imap_watch_failed$"):
            self.watcher.wait()
        self.assertEqual(self.mailbox.close_calls, 1)

    def test_failed_noop_closes(self):
        self.mailbox.client.capabilities = ()
        self.watcher.wait()
        self.mailbox.client.noop_status = "NO"
        with self.assertRaises(MailWatchError):
            self.watcher.wait(1)
        self.assertEqual(self.mailbox.close_calls, 1)

    def test_close_is_idempotent_and_reconnects_on_next_wait(self):
        self.watcher.wait()
        self.watcher.close()
        self.watcher.close()
        self.assertEqual(self.mailbox.close_calls, 1)
        self.assertTrue(self.watcher.wait())

    def test_close_uses_socket_shutdown_if_adapter_close_fails(self):
        self.watcher.wait()
        self.mailbox.close_error = OSError("logout failed")
        self.watcher.close()
        self.assertEqual(self.mailbox.client.shutdown_calls, 1)


if __name__ == "__main__":
    unittest.main()
