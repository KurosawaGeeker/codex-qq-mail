#!/usr/bin/env python3
"""MCP adapter; mail state, credentials and SMTP live in the worker process."""

import json
from pathlib import Path
import subprocess
import sys

WORKER = Path(__file__).with_name("qq_mail_service.py")
UUID_FIELD = {"type": "string", "format": "uuid"}
TOOLS = [
    {
        "name": "configure_email_thread",
        "description": "Register an existing local Codex task's mail conversation and persist its notification preference. Use always only when the user requests email regardless of duration. This does not create a Codex task or send email.",
        "inputSchema": {"type": "object", "properties": {
            "thread_id": {**UUID_FIELD, "description": "The exact current Codex task UUID."},
            "subject": {"type": "string", "maxLength": 160, "description": "Stable task title; required on first registration."},
            "notification_mode": {"type": "string", "enum": ["long_tasks", "always", "off"]},
        }, "required": ["thread_id", "notification_mode"], "additionalProperties": False},
    },
    {
        "name": "get_email_thread",
        "description": "Read this task's stable mail UUID, notification preference and submission metadata; does not read the mailbox or send email.",
        "inputSchema": {"type": "object", "properties": {"thread_id": UUID_FIELD}, "required": ["thread_id"], "additionalProperties": False},
    },
    {
        "name": "resolve_email_submission",
        "description": "Resolve an uncertain SMTP submission only after the user confirms receipt (received) or explicitly authorizes a possible duplicate resend (retry_authorized). This records the decision and does not send. Never infer authorization from lack of a receipt.",
        "inputSchema": {"type": "object", "properties": {
            "thread_id": UUID_FIELD,
            "event_id": UUID_FIELD,
            "resolution": {"type": "string", "enum": ["received", "retry_authorized"]},
        }, "required": ["thread_id", "event_id", "resolution"], "additionalProperties": False},
    },
    {
        "name": "send_completion_email",
        "description": "Submit a completion/status email through the local QQ mail worker. Same task keeps one mail conversation. Default: send after more than 600 seconds; persisted always mode or an explicitly authorized send_now bypasses that threshold. Reuse event_id on retries. accepted means SMTP accepted, not verified inbox delivery.",
        "inputSchema": {"type": "object", "properties": {
            "thread_id": {**UUID_FIELD, "description": "Exact existing Codex task UUID; never guess."},
            "event_id": {**UUID_FIELD, "description": "UUID for this logical completion/status event. Reuse the same value and body on retries."},
            "subject": {"type": "string", "maxLength": 160, "description": "Task title; required if not registered. Later subjects stay fixed."},
            "body": {"type": "string", "maxLength": 1000000, "description": "Final user-facing result, verification and any blocker."},
            "elapsed_seconds": {"type": "number", "minimum": 0, "description": "Elapsed execution time for this turn, excluding idle time between turns."},
            "send_now": {"type": "boolean", "description": "Bypass the duration threshold for this message only when the user explicitly requested it."},
        }, "required": ["thread_id", "event_id", "body", "elapsed_seconds"], "additionalProperties": False},
    },
]

REPLY_TOOLS = {
    "enable_email_replies": "Enable replies for this existing local task only. Establish a read-only QQ Sent-mail baseline; older emails cannot execute. Does not start an automation or execute instructions.",
    "disable_email_replies": "Stop collecting and claiming email replies for this task. Does not delete stored task or mail history.",
    "poll_email_replies": "Read new messages from the authenticated QQ Sent mailbox, verify reply linkage, reconcile QQ-rewritten Message-IDs, and queue valid replies. Does not execute instructions. Empty results should remain quiet.",
    "claim_email_reply": "Atomically claim one verified queued reply for this exact task. Execute only a newly claimed body once. needs_attention means a prior claim exists: do not execute it again.",
    "get_email_reply_status": "Read this task's reply intake status and queue metadata without accessing the mailbox or executing instructions.",
}
for name, description in REPLY_TOOLS.items():
    TOOLS.append({"name": name, "description": description,
                  "inputSchema": {"type": "object", "properties": {"thread_id": UUID_FIELD},
                                  "required": ["thread_id"], "additionalProperties": False}})
TOOLS.append({
    "name": "retry_email_reply",
    "description": "Deliberately recheck one previously scanned Sent-mail UID after a parser compatibility fix. Only messages after this task's enable baseline and at or before its scan cursor are eligible; all identity, linkage and deduplication checks still apply. Does not execute instructions.",
    "inputSchema": {"type": "object", "properties": {
        "thread_id": UUID_FIELD, "uid": {"type": "integer", "minimum": 1},
    }, "required": ["thread_id", "uid"], "additionalProperties": False},
})
TOOLS.append({
    "name": "configure_email_listener",
    "description": "Enable or disable local daemon pickup for this existing Codex task. Enabling establishes authenticated QQ reply intake and adds only this UUID to the daemon allowlist. Empty waiting uses ordinary code and no model. Replies run through the shared native App-server while the App stays open. Does not create tasks or install/start launchd.",
    "inputSchema": {"type": "object", "properties": {"thread_id": UUID_FIELD, "enabled": {"type": "boolean"}},
                    "required": ["thread_id", "enabled"], "additionalProperties": False},
})
TOOLS.append({
    "name": "get_email_listener_status",
    "description": "Read this task's local daemon binding and last recorded state. Does not read mail, start a model, or assert that a recorded PID is still alive.",
    "inputSchema": {"type": "object", "properties": {"thread_id": UUID_FIELD},
                    "required": ["thread_id"], "additionalProperties": False},
})
TOOLS.append({
    "name": "complete_email_reply",
    "description": "Persist the completed reply's result and send it back into the same email chain, regardless of duration. Requires the original claim. First completion requires result_body. For result_pending, omit result_body to resend the exact stored result with the same claim; never re-execute the original command.",
    "inputSchema": {"type": "object", "properties": {
        "thread_id": UUID_FIELD, "reply_id": UUID_FIELD, "claim_token": UUID_FIELD,
        "result_body": {"type": "string", "minLength": 1, "maxLength": 1000000},
    }, "required": ["thread_id", "reply_id", "claim_token"], "additionalProperties": False},
})


def respond(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    payload["error" if error is not None else "result"] = error if error is not None else result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def invoke_worker(operation, arguments):
    try:
        completed = subprocess.run(
            [sys.executable, str(WORKER)],
            input=json.dumps({"operation": operation, "arguments": arguments}, ensure_ascii=False),
            capture_output=True, text=True, timeout=50,
        )
        if completed.returncode != 0:
            return {"status": "service_error", "code": "worker_exited"}
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "code": "worker_timeout_check_before_retry"}
    except (OSError, ValueError):
        return {"status": "service_error", "code": "worker_unavailable"}


def main():
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if method == "initialize":
                respond(request_id, {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                                     "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "qq-mail", "version": "5.0.0"}})
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                respond(request_id, {})
            elif method == "tools/list":
                respond(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                operation = params.get("name")
                if operation not in {tool["name"] for tool in TOOLS}:
                    raise ValueError("unknown tool")
                result = invoke_worker(operation, params.get("arguments", {}))
                respond(request_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                     "isError": result.get("status") not in {"accepted", "registered", "not_registered", "skipped", "resolved", "enabled", "disabled", "polled", "retried", "claimed", "empty", "completed"}})
            elif request_id is not None:
                respond(request_id, error={"code": -32601, "message": "Method not found"})
        except (ValueError, TypeError, AttributeError):
            respond(request_id, error={"code": -32602, "message": "Invalid request or arguments"})


if __name__ == "__main__":
    main()
