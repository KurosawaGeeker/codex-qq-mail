"""Per-user configuration. Credentials never belong in this file or its JSON.

Set CODEX_QQ_MAIL_STATE_DIR for an isolated installation. Its private config.json
accepts account, cli_path and keychain_service. Environment overrides are useful
for launchd and tests; account binding prevents accidental reuse across mailboxes.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil


STATE_DIR = Path(os.environ.get(
    "CODEX_QQ_MAIL_STATE_DIR", str(Path.home() / "Library/Application Support/Codex QQ Mail")
)).expanduser().resolve()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()


def load_config(state_dir=STATE_DIR):
    path = Path(state_dir) / "config.json"
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        raise ValueError("mail_config_unreadable") from None
    if not isinstance(value, dict):
        raise ValueError("mail_config_must_be_object")
    if set(value) - {"account", "cli_path", "keychain_service"}:
        raise ValueError("mail_config_unsupported_fields")
    if any(not isinstance(item, str) for item in value.values()):
        raise ValueError("mail_config_fields_must_be_strings")
    return value


CONFIG = load_config()
ACCOUNT = os.environ.get("CODEX_QQ_MAIL_ACCOUNT", CONFIG.get("account", "")).strip().lower()
KEYCHAIN_SERVICE = CONFIG.get("keychain_service", "codex-qq-smtp-auth-code")
if not KEYCHAIN_SERVICE or len(KEYCHAIN_SERVICE) > 128 or any(ord(c) < 32 for c in KEYCHAIN_SERVICE):
    raise ValueError("invalid_keychain_service_name")


def require_account(state_dir=STATE_DIR):
    """Validate the account and atomically bind a state directory to it.

    A populated unbound directory is refused rather than silently adopting mail
    history from another installation. Use a fresh state directory to migrate.
    """
    if not ACCOUNT:
        raise ValueError("mail_account_not_configured")
    if not re.fullmatch(r"[a-z0-9._+-]+@qq\.com", ACCOUNT):
        raise ValueError("mail_account_must_be_qq_address")
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    binding = state / "account-binding.json"
    with (state / "account-binding.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binding.exists():
            try:
                existing = json.loads(binding.read_text())
            except (OSError, ValueError):
                raise ValueError("mail_account_binding_unreadable") from None
            if existing != {"account": ACCOUNT}:
                raise ValueError("mail_account_state_mismatch_use_new_state_directory")
        else:
            if any((state / name).exists() for name in (
                "state.sqlite3", "replies.sqlite3", "daemon.sqlite3", "account-aliases.json"
            )):
                raise ValueError("mail_state_unbound_use_new_state_directory")
            temporary = state / "account-binding.json.new"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"account": ACCOUNT}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(binding)
        binding.chmod(0o600)
    return ACCOUNT


def discover_codex():
    """Find the native Codex executable without embedding a user's home path."""
    configured = os.environ.get("CODEX_QQ_MAIL_CODEX_PATH", CONFIG.get("cli_path", ""))
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if (not candidate.is_file() or not os.access(candidate, os.X_OK)
                or candidate == Path(__file__).with_name("qq_app_bridge.py").resolve()):
            raise ValueError("configured_codex_executable_unavailable")
        return candidate
    candidates = [
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
        Path.home() / "Applications/Codex.app/Contents/Resources/codex",
        Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex",
    ]
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    # Importing parser/MCP metadata must work before Codex has been installed.
    # An attempted execution still fails closed with FileNotFoundError.
    return Path("codex")
