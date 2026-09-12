"""Configure task-specific mail listening without starting processes or models."""

import fcntl
import json
import os
from pathlib import Path
import tempfile

from qq_mail_service import STATE_DIR, uuid_string
from qq_reply_service import ReplyService


def _read_object(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("invalid listener state file") from None
    if not isinstance(value, dict):
        raise ValueError("listener state file must contain an object")
    return value


def _targets(config):
    values = config.get("thread_ids", [])
    if not isinstance(values, list):
        raise ValueError("thread_ids must be a UUID list")
    return list(dict.fromkeys(uuid_string(value, "thread_id") for value in values))


def _write_config(state_dir, config):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".daemon-config-",
                                         suffix=".json", dir=state_dir, delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(config, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, state_dir / "daemon-config.json")
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _status(state_dir, thread_id, enabled):
    snapshot = _read_object(state_dir / "daemon-status.json")
    targets = snapshot.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("invalid daemon status targets")
    return {
        "status": "enabled" if enabled else "disabled",
        "thread_id": thread_id,
        "enabled": enabled,
        # This is a recorded snapshot, not a liveness probe or process claim.
        "daemon": {
            "target": targets.get(thread_id),
            "updated_at": snapshot.get("updated_at"),
            "pid": snapshot.get("pid"),
            **{key: snapshot[key] for key in ('execution_backend', 'app_connected') if key in snapshot},
        },
    }


def handle(operation, args, state_dir=STATE_DIR, reply_service=None):
    allowed = {
        "configure_email_listener": {"thread_id", "enabled"},
        "get_email_listener_status": {"thread_id"},
    }
    if operation not in allowed or not isinstance(args, dict) or set(args) != allowed[operation]:
        raise ValueError("invalid listener operation or arguments")
    thread_id = uuid_string(args["thread_id"], "thread_id")
    if operation == "configure_email_listener" and not isinstance(args["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    with (state_dir / "daemon-config.lock").open("a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "thread_id": thread_id}
        config = _read_object(state_dir / "daemon-config.json")
        targets = _targets(config)
        if operation == "get_email_listener_status":
            return _status(state_dir, thread_id, thread_id in targets)
        if args["enabled"]:
            service = reply_service if reply_service is not None else ReplyService(state_dir)
            result = service.handle("enable_email_replies", {"thread_id": thread_id})
            if result.get("status") != "enabled":
                return {
                    "status": result.get("status", "unavailable"), "thread_id": thread_id,
                    "enabled": thread_id in targets, "code": "reply_intake_not_enabled",
                }
            if thread_id not in targets:
                targets.append(thread_id)
        else:
            targets = [item for item in targets if item != thread_id]
        _write_config(state_dir, {**config, "thread_ids": targets})
        return _status(state_dir, thread_id, thread_id in targets)
