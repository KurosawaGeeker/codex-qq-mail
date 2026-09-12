"""Offline listener allowlist and task-scoped status tests."""

import fcntl
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch



import bootstrap  # Isolate state and add the source directory before runtime imports.

from qq_listener_control import handle


THREAD_A = "00000000-0000-4000-8000-000000000001"
THREAD_B = "00000000-0000-4000-8000-000000000002"


class FakeReplyService:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.calls = []
        self.result = {"status": "enabled", "thread_id": THREAD_A}
        self.before_enable = None

    def handle(self, operation, args):
        self.calls.append((operation, dict(args)))
        if self.before_enable:
            self.before_enable()
        return dict(self.result)


class ListenerControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qq-listener-control-test-")
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.config_path = self.state_dir / "daemon-config.json"
        self.service = FakeReplyService(self.state_dir)

    def call(self, operation="configure_email_listener", **args):
        return handle(operation, {"thread_id": THREAD_A, **args}, self.state_dir, self.service)

    def write_config(self, config):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_enable_intake_precedes_allowlist_update_and_is_idempotent(self):
        self.service.before_enable = lambda: self.assertFalse(self.config_path.exists())
        result = self.call(enabled=True)
        self.assertEqual(result["status"], "enabled")
        self.assertTrue(result["enabled"])
        self.assertEqual(self.service.calls, [("enable_email_replies", {"thread_id": THREAD_A})])
        self.assertEqual(self.read_config(), {"thread_ids": [THREAD_A]})
        self.service.before_enable = None
        self.call(enabled=True)
        self.assertEqual(self.read_config()["thread_ids"], [THREAD_A])

    def test_failed_enable_never_adds_unregistered_task(self):
        self.write_config({"thread_ids": [THREAD_B], "preserve": {"mode": "local"}})
        original = self.config_path.read_bytes()
        for status in ["not_registered", "busy", "disabled", "mailbox_reset"]:
            with self.subTest(status=status):
                self.service.result = {"status": status}
                result = self.call(enabled=True)
                self.assertEqual(result["status"], status)
                self.assertFalse(result["enabled"])
                self.assertEqual(self.config_path.read_bytes(), original)

    def test_disable_preserves_other_configuration_and_manual_intake(self):
        self.write_config({"thread_ids": [THREAD_A, THREAD_B], "interval": 30, "custom": {"x": 1}})
        result = self.call(enabled=False)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["enabled"])
        self.assertEqual(self.read_config(), {"thread_ids": [THREAD_B], "interval": 30, "custom": {"x": 1}})
        self.assertEqual(self.service.calls, [])

    def test_adding_one_task_preserves_existing_order_and_extra_keys(self):
        self.write_config({"thread_ids": [THREAD_B], "custom": "keep"})
        self.call(enabled=True)
        self.assertEqual(self.read_config(), {"thread_ids": [THREAD_B, THREAD_A], "custom": "keep"})

    def test_status_reports_only_target_snapshot_without_liveness_claim(self):
        self.write_config({"thread_ids": [THREAD_A, THREAD_B], "secret": "config-private"})
        snapshot = {
            "status": "running", "pid": 456, "updated_at": 123.5,
            "targets": {THREAD_A: {"status": "waiting_for_task"}, THREAD_B: {"status": "other-task-private"}},
            "extra": "daemon-private",
        }
        (self.state_dir / "daemon-status.json").write_text(json.dumps(snapshot), encoding="utf-8")
        result = self.call("get_email_listener_status")
        self.assertEqual(result, {
            "status": "enabled", "enabled": True, "thread_id": THREAD_A,
            "daemon": {"target": {"status": "waiting_for_task"}, "updated_at": 123.5, "pid": 456},
        })
        encoded = json.dumps(result)
        self.assertNotIn(THREAD_B, encoded)
        self.assertNotIn("private", encoded)
        self.assertNotIn("running", encoded)
        self.assertEqual(self.service.calls, [])

    def test_missing_configuration_is_disabled_without_creating_allowlist(self):
        result = self.call("get_email_listener_status")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["daemon"], {"target": None, "updated_at": None, "pid": None})
        self.assertFalse(self.config_path.exists())
        self.assertEqual(self.service.calls, [])

    def test_lock_contention_does_not_call_intake_or_write_configuration(self):
        with (self.state_dir / "daemon-config.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.call(enabled=True)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.config_path.exists())

    def test_configuration_and_lock_are_private(self):
        self.call(enabled=True)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state_dir / "daemon-config.lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    def test_atomic_replace_failure_keeps_old_config_and_cleans_temporary_file(self):
        self.write_config({"thread_ids": [THREAD_A, THREAD_B], "custom": 1})
        original = self.config_path.read_bytes()
        with patch("qq_listener_control.os.replace", side_effect=OSError("test interrupted write")):
            with self.assertRaises(OSError):
                self.call(enabled=False)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(list(self.state_dir.glob(".daemon-config-*.json")), [])

    def test_malformed_existing_config_is_not_replaced(self):
        for raw in ["broken JSON", "[]", '{"thread_ids":"not a list"}', '{"thread_ids":["not-a-uuid"]}']:
            with self.subTest(raw=raw):
                self.config_path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.call(enabled=True)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), raw)
                self.assertEqual(self.service.calls, [])

    def test_rejects_invalid_uuid_boolean_missing_or_extra_arguments(self):
        cases = [
            ("configure_email_listener", {"thread_id": THREAD_A[:8], "enabled": True}),
            ("configure_email_listener", {"thread_id": 123, "enabled": True}),
            ("configure_email_listener", {"thread_id": THREAD_A, "enabled": 1}),
            ("configure_email_listener", {"thread_id": THREAD_A, "enabled": "true"}),
            ("configure_email_listener", {"thread_id": THREAD_A}),
            ("configure_email_listener", {"thread_id": THREAD_A, "enabled": True, "subject": "new task"}),
            ("get_email_listener_status", {"thread_id": THREAD_A, "enabled": True}),
            ("get_email_listener_status", {}),
            ("start_new_task", {"thread_id": THREAD_A}),
        ]
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                with self.assertRaises(ValueError):
                    handle(operation, args, self.state_dir, self.service)
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
