"""Configuration and account isolation checks without credentials or network."""

import json
import os
from email.message import EmailMessage
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bootstrap

import qq_mail_config
from qq_mail_service import MailService, keychain_credential, smtp_transport
from qq_reply_service import QQMailbox


class MailConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qq-config-test-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def test_missing_account_fails_before_state_or_external_access(self):
        new_state = self.state / "must-not-be-created"
        with patch.object(qq_mail_config, "ACCOUNT", ""), \
                patch("qq_mail_service.subprocess.run") as credential_process, \
                patch("qq_mail_service.smtplib.SMTP_SSL") as smtp, \
                patch("qq_reply_service.imaplib.IMAP4_SSL") as imap:
            for action in (lambda: MailService(new_state), keychain_credential, QQMailbox,
                           lambda: smtp_transport(EmailMessage())):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "mail_account_not_configured"):
                    action()
        credential_process.assert_not_called()
        smtp.assert_not_called()
        imap.assert_not_called()
        self.assertFalse(new_state.exists())

    def test_binding_cannot_be_reused_for_another_account(self):
        qq_mail_config.require_account(self.state)
        with patch.object(qq_mail_config, "ACCOUNT", "another-configured-user@qq.com"):
            with self.assertRaisesRegex(ValueError, "mail_account_state_mismatch"):
                MailService(self.state)
        binding = json.loads((self.state / "account-binding.json").read_text())
        self.assertEqual(binding, {"account": "configured-user@qq.com"})

    def test_populated_unbound_state_is_not_adopted(self):
        for filename in ("state.sqlite3", "replies.sqlite3", "daemon.sqlite3", "account-aliases.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                marker = state / filename
                marker.write_text("synthetic-existing-state")
                with self.assertRaisesRegex(ValueError, "mail_state_unbound"):
                    qq_mail_config.require_account(state)
                self.assertFalse((state / "account-binding.json").exists())
                self.assertEqual(marker.read_text(), "synthetic-existing-state")

    def test_binding_is_private_and_idempotent(self):
        for _ in range(2):
            self.assertEqual(qq_mail_config.require_account(self.state), "configured-user@qq.com")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for name in ("account-binding.json", "account-binding.lock"):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)

    def test_malformed_binding_does_not_get_replaced(self):
        binding = self.state / "account-binding.json"
        binding.write_text("not-json")
        with self.assertRaisesRegex(ValueError, "mail_account_binding_unreadable"):
            qq_mail_config.require_account(self.state)
        self.assertEqual(binding.read_text(), "not-json")

    def test_config_rejects_credentials_and_invalid_shapes(self):
        for content, error in (
            ('{"password": "synthetic-value"}', "mail_config_unsupported_fields"),
            ('{"account": 123}', "mail_config_fields_must_be_strings"),
            ('[]', "mail_config_must_be_object"),
            ('{', "mail_config_unreadable"),
        ):
            with self.subTest(error=error):
                (self.state / "config.json").write_text(content)
                with self.assertRaisesRegex(ValueError, error) as caught:
                    qq_mail_config.load_config(self.state)
                self.assertNotIn("synthetic-value", str(caught.exception))

    def read_config_in_child(self, account=None):
        environment = dict(os.environ)
        environment.pop("CODEX_QQ_MAIL_ACCOUNT", None)
        environment["CODEX_QQ_MAIL_STATE_DIR"] = str(self.state)
        environment["CODEX_HOME"] = str(self.state / "empty-codex-home")
        if account is not None:
            environment["CODEX_QQ_MAIL_ACCOUNT"] = account
        result = subprocess.run(
            [sys.executable, "-c", "import json,qq_mail_config as c; print(json.dumps({'account':c.ACCOUNT,'service':c.KEYCHAIN_SERVICE}))"],
            env=environment, cwd=bootstrap.SOURCE_DIR, check=True,
            capture_output=True, text=True, timeout=10,
        )
        return json.loads(result.stdout)

    def test_private_config_and_environment_override(self):
        (self.state / "config.json").write_text(json.dumps({
            "account": "config-fixture@qq.com", "keychain_service": "synthetic-mail-service",
        }))
        self.assertEqual(self.read_config_in_child(), {
            "account": "config-fixture@qq.com", "service": "synthetic-mail-service",
        })
        self.assertEqual(self.read_config_in_child(" ENV-FIXTURE@qq.com ")["account"], "env-fixture@qq.com")

    def test_import_without_config_has_no_default_account(self):
        self.assertEqual(self.read_config_in_child()["account"], "")
        self.assertFalse((self.state / "account-binding.json").exists())


if __name__ == "__main__":
    unittest.main()
