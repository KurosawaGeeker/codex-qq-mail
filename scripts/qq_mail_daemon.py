#!/usr/bin/env python3
"""Local mail event loop. No Codex process is launched without a verified reply."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from qq_mail_service import STATE_DIR, uuid_string
from qq_mail_config import CODEX_HOME, discover_codex, require_account
from qq_reply_service import ReplyService

CLI_PATH = discover_codex()


def read_thread(thread_id, codex_home=CODEX_HOME):
    # Read only the existing App/CLI shared index; never manufacture a task ID.
    candidates = sorted(Path(codex_home).glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
    for path in candidates:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT id,cwd,rollout_path,archived FROM threads WHERE id=?', (thread_id,)).fetchone()
            if row:
                if row['archived'] or not Path(row['cwd']).is_dir() or not Path(row['rollout_path']).is_file():
                    raise ValueError('task_unavailable')
                return dict(row)
    raise ValueError('task_not_found')


def writer_available(thread_id, codex_home=CODEX_HOME):
    path = Path(codex_home) / 'thread-writer-locks' / (thread_id + '.lock')
    if not path.exists():
        return True
    with path.open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    # Codex itself takes the native lock again, closing the check/start race.
    return True


class MailDaemon:
    def __init__(self, state_dir=STATE_DIR, codex_home=CODEX_HOME, cli_path=CLI_PATH,
                 reply_service=None, executor=None, thread_reader=None, availability=None, recovery=None):
        self.state_dir = Path(state_dir)
        require_account(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.codex_home = Path(codex_home)
        self.cli_path = Path(cli_path)
        self.replies = reply_service or ReplyService(self.state_dir)
        self.executor = executor or self.execute_cli
        self.thread_reader = thread_reader or (lambda thread_id: read_thread(thread_id, self.codex_home))
        self.availability = availability or (lambda thread_id: writer_available(thread_id, self.codex_home))
        self.recovery = recovery
        self.journal_path = self.state_dir / 'daemon.sqlite3'
        with closing(self.connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS executions (
                reply_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, claim_token TEXT NOT NULL,
                state TEXT NOT NULL, result_body TEXT, error_code TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS attention_notices (
                reply_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, code TEXT NOT NULL)''')
            db.commit()
        self.journal_path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.journal_path)
        db.row_factory = sqlite3.Row
        return db

    def process_thread(self, thread_id):
        with (self.state_dir / 'dispatch.lock').open('a') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'busy'}
            return self._process_thread(thread_id)

    def _process_thread(self, thread_id):
        thread_id = uuid_string(thread_id, 'thread_id')
        status = self.replies.handle('get_email_reply_status', {'thread_id': thread_id})
        if status.get('status') != 'enabled':
            return {'status': status.get('status', 'unavailable')}
        rows = status.get('replies', [])
        active = next((r for r in rows if r['state'] in {'claimed', 'result_pending'}), None)
        with closing(self.connect()) as db:
            if active:
                job = db.execute('SELECT * FROM executions WHERE reply_id=? AND thread_id=?', (active['reply_id'], thread_id)).fetchone()
                if job is None or job['claim_token'] != active['claim_token']:
                    return {'status': 'attention', 'code': 'claim_owned_elsewhere', 'reply_id': active['reply_id']}
                if job['state'] in {'launching', 'attention'} and self.recovery is not None:
                    claim = self.restore_claim(job)
                    if claim is None:
                        return {'status': 'attention', 'code': 'prepared_claim_missing', 'reply_id': active['reply_id']}
                    # Recovery reads a durable native queue/turn receipt. It never submits again.
                    recovered = self.recovery(claim, self.thread_reader(thread_id), self.state_dir / 'runs' / claim['reply_id'])
                    if recovered.get('status') == 'completed' and isinstance(recovered.get('body'), str) and recovered['body'].strip():
                        db.execute("UPDATE executions SET state='result_ready',result_body=?,error_code=NULL WHERE reply_id=?", (recovered['body'], claim['reply_id']))
                        db.commit()
                        job = db.execute('SELECT * FROM executions WHERE reply_id=?', (claim['reply_id'],)).fetchone()
                        return self.deliver(db, job, active)
                    if recovered.get('status') in {'running', 'deferred'}:
                        return {'status': 'waiting_for_result', 'code': recovered.get('code', 'native_turn_pending'), 'reply_id': claim['reply_id']}
                    return {'status': 'attention', 'code': recovered.get('code', 'execution_outcome_unknown'), 'reply_id': claim['reply_id']}
                if job['state'] == 'launching':
                    # The prior CLI may have run. Never infer safe replay from a missing PID.
                    return {'status': 'attention', 'code': 'execution_outcome_unknown', 'reply_id': active['reply_id']}
                if job['state'] == 'attention':
                    return {'status': 'attention', 'code': job['error_code'], 'reply_id': active['reply_id']}
                if job['state'] == 'result_ready':
                    return self.deliver(db, job, active)
            else:
                if not any(r['state'] == 'queued' for r in rows):
                    if status.get('rejected_replies'):
                        return {'status': 'reply_rejected', 'rejections': status['rejected_replies']}
                    return {'status': 'idle'}
                job = None
            if not self.availability(thread_id):
                details = getattr(getattr(self.availability, '__self__', None), 'last_availability', {})
                return {'status': 'waiting_for_task', 'code': details.get('code', 'native_writer_lock_busy')}
            metadata = self.thread_reader(thread_id)
            if job is None:
                claim = self.replies.handle('claim_email_reply', {'thread_id': thread_id})
                if claim.get('status') != 'claimed':
                    return {'status': claim.get('status', 'attention')}
                db.execute("INSERT INTO executions (reply_id,thread_id,claim_token,state) VALUES (?,?,?,'prepared')", (claim['reply_id'], thread_id, claim['claim_token']))
                db.commit()
                job = db.execute('SELECT * FROM executions WHERE reply_id=?', (claim['reply_id'],)).fetchone()
            else:
                # Recovery of prepared occurs before any process could have started.
                # Read its body only from the original queue and exact claim token.
                claim = self.restore_claim(job)
                if claim is None:
                    return {'status': 'attention', 'code': 'prepared_claim_missing'}
            run_dir = self.state_dir / 'runs' / claim['reply_id']
            run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            db.execute("UPDATE executions SET state='launching' WHERE reply_id=?", (claim['reply_id'],))
            db.commit()
            try:
                result = self.executor(claim, metadata, run_dir)
            except Exception as exc:
                result = {'status': 'attention', 'code': 'executor_' + type(exc).__name__}
            if result.get('status') == 'deferred':
                db.execute("UPDATE executions SET state='prepared',error_code=NULL WHERE reply_id=?", (claim['reply_id'],))
                db.commit()
                return {'status': 'waiting_for_task', 'code': result.get('code', 'native_writer_lock_busy')}
            if result.get('status') == 'running':
                # Submission exists in the native service. Keep the launch journal
                # so the next pass only recovers that exact queue item or turn.
                return {'status': 'waiting_for_result', 'code': result.get('code', 'native_turn_pending'), 'reply_id': claim['reply_id']}
            if result.get('status') != 'completed' or not isinstance(result.get('body'), str) or not result['body'].strip():
                code = result.get('code', 'missing_execution_result')
                db.execute("UPDATE executions SET state='attention',error_code=? WHERE reply_id=?", (code, claim['reply_id']))
                db.commit()
                return {'status': 'attention', 'code': code, 'reply_id': claim['reply_id']}
            db.execute("UPDATE executions SET state='result_ready',result_body=? WHERE reply_id=?", (result['body'], claim['reply_id']))
            db.commit()
            job = db.execute('SELECT * FROM executions WHERE reply_id=?', (claim['reply_id'],)).fetchone()
            return self.deliver(db, job, {'state': 'claimed'})

    def restore_claim(self, job):
        with closing(sqlite3.connect(self.state_dir / 'replies.sqlite3')) as queue:
            queue.row_factory = sqlite3.Row
            row = queue.execute("SELECT * FROM replies WHERE reply_id=? AND thread_id=? AND claim_token=? AND state='claimed'", (job['reply_id'], job['thread_id'], job['claim_token'])).fetchone()
            return {**dict(row), 'status': 'claimed'} if row else None

    def notify_attention(self, thread_id, result):
        if result.get('status') != 'attention' or not result.get('reply_id'):
            return result
        code = result.get('code', 'execution_outcome_unknown')
        with closing(self.connect()) as db:
            db.execute('INSERT OR IGNORE INTO attention_notices VALUES (?,?,?)', (result['reply_id'], thread_id, code))
            db.commit()
        notice = self.flush_attention(thread_id, result['reply_id'])
        return {**result, 'email_feedback': {k: notice.get(k) for k in ['status', 'event_id', 'code']}}

    def flush_attention(self, thread_id, reply_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT code FROM attention_notices WHERE reply_id=? AND thread_id=?', (reply_id, thread_id)).fetchone()
        if row is None:
            return {'status': 'accepted'}
        code = row['code']
        # A stable event and body make repeated health checks a read-only duplicate
        # after acceptance. SMTP uncertainty is still owned by the mail worker.
        event_id = str(uuid.uuid5(uuid.UUID(reply_id), 'attention-v1:' + code))
        body = ('已收到你的邮件，但这次执行需要处理，暂时没有可确认的最终结果。\n\n'
                '任务：' + thread_id + '\n回复编号：' + reply_id + '\n状态：' + code + '\n\n'
                '后台保留了这次执行记录，不会自动重复执行原指令。若原任务仍在运行，恢复连接后会继续核对并返回它的结果。')
        return self.replies.mail.handle('send_completion_email', {'thread_id': thread_id,
            'event_id': event_id, 'body': body, 'elapsed_seconds': 0, 'send_now': True})

    def deliver(self, db, job, active):
        notice = self.flush_attention(job['thread_id'], job['reply_id'])
        if notice.get('status') != 'accepted':
            return {'status': 'attention', 'code': 'status_notice_delivery_pending', 'reply_id': job['reply_id']}
        args = {key: job[key] for key in ['thread_id', 'reply_id', 'claim_token']}
        if active['state'] == 'result_pending':
            # The mail service owns SMTP uncertainty and the immutable output event ID.
            thread = self.replies.mail.handle('get_email_thread', {'thread_id': job['thread_id']})
            pending = [row for row in thread.get('messages', []) if row.get('status') == 'unknown']
            if pending:
                return {'status': 'attention', 'code': 'smtp_outcome_unknown', 'reply_id': job['reply_id']}
        else:
            args['result_body'] = job['result_body']
        result = self.replies.handle('complete_email_reply', args)
        if result.get('status') == 'completed':
            # The queue is the authoritative delivery state. Do not retain a second body.
            db.execute('UPDATE executions SET result_body=NULL WHERE reply_id=?', (job['reply_id'],))
            db.commit()
            for name in ['result.txt', 'events.jsonl', 'stderr.txt']:
                (self.state_dir / 'runs' / job['reply_id'] / name).unlink(missing_ok=True)
        return result

    def execute_cli(self, claim, metadata, run_dir):
        run_dir.chmod(0o700)
        prompt = ('The following is a verified QQ email reply from the user for this exact existing task. '
                  'Continue this task once. Follow the user request and applicable policies. '
                  'The local mail worker will send your final answer into the same email chain, including for long work. '
                  'Do not send a separate completion email for this invocation. '
                  'Do not change the mail daemon or schedule a polling automation unless the user explicitly requests it.\n'
                  'QQ_MAIL_REPLY_ID: ' + claim['reply_id'] + '\n\n' + claim['body'])
        result_file = run_dir / 'result.txt'
        for name in ['result.txt', 'events.jsonl', 'stderr.txt']:
            path = run_dir / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(descriptor)
            path.chmod(0o600)
        command = [str(self.cli_path), 'exec', '--json', '--output-last-message', str(result_file),
                   '-C', metadata['cwd'], '-s', 'workspace-write',
                   'resume', '--skip-git-repo-check', claim['thread_id'], '-']
        env = os.environ.copy()
        env['CODEX_HOME'] = str(self.codex_home)
        env.pop('CODEX_THREAD_ID', None)
        env['QQ_MAIL_DELIVERY_OWNER'] = 'daemon'
        with (run_dir / 'events.jsonl').open('w') as output, (run_dir / 'stderr.txt').open('w') as errors:
            child = subprocess.run(command, input=prompt, text=True, stdout=output, stderr=errors, env=env, cwd=metadata['cwd'])
        completed = False
        exact_thread = False
        wrong_thread = False
        events_started = False
        with (run_dir / 'events.jsonl').open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started':
                    events_started = True
                    exact_thread = event.get('thread_id') == claim['thread_id']
                    wrong_thread = wrong_thread or not exact_thread
                if event.get('type', '').startswith('turn.'):
                    events_started = True
                if event.get('type') == 'turn.completed':
                    completed = True
        lock_error = f"Error: thread/resume: thread/resume failed: thread {claim['thread_id']} already has an active writer (code -32600)"
        if child.returncode == 1 and not events_started and lock_error in (run_dir / 'stderr.txt').read_text().splitlines():
            return {'status': 'deferred', 'code': 'native_writer_lock_busy'}
        if child.returncode != 0 or not completed or not exact_thread or wrong_thread or not result_file.is_file():
            return {'status': 'attention', 'code': 'cli_incomplete_check_run_files'}
        body = result_file.read_text()
        if not body.strip() or len(body) > 1_000_000:
            return {'status': 'attention', 'code': 'invalid_cli_result'}
        return {'status': 'completed', 'body': body}


def write_status(state_dir, data):
    temporary = state_dir / 'daemon-status.json.new'
    temporary.write_text(json.dumps({'updated_at': time.time(), 'pid': os.getpid(), **data}, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(state_dir / 'daemon-status.json')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'once', 'status'])
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    parser.add_argument('--codex-home', type=Path, default=CODEX_HOME)
    parser.add_argument('--cli-path', type=Path, default=CLI_PATH)
    args = parser.parse_args()
    if args.command == 'status':
        path = args.state_dir / 'daemon-status.json'
        print(path.read_text() if path.is_file() else json.dumps({'status': 'not_started'}))
        return
    config = json.loads((args.state_dir / 'daemon-config.json').read_text())
    targets = [uuid_string(item, 'thread_id') for item in config['thread_ids']]
    from qq_mail_watcher import MailWatcher
    from qq_shared_executor import SharedExecutor
    shared = SharedExecutor(args.state_dir / 'shared-app.sock', metadata_path=args.state_dir / 'shared-app.json')
    daemon = MailDaemon(args.state_dir, args.codex_home, args.cli_path,
                        executor=shared, availability=shared.availability, recovery=shared.recover)
    stop = False
    def shutdown(signum, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with (args.state_dir / 'daemon.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'already_running'}))
            return
        watcher = MailWatcher()
        dirty = True
        try:
            while not stop:
                current_config = json.loads((args.state_dir / 'daemon-config.json').read_text())
                current_targets = [uuid_string(item, 'thread_id') for item in current_config['thread_ids']]
                if current_targets != targets:
                    dirty = True
                    targets = current_targets
                states = {}
                needs_scan = False
                scan_failed = False
                for thread_id in targets:
                    try:
                        if dirty:
                            poll = daemon.replies.handle('poll_email_replies', {'thread_id': thread_id})
                            if poll.get('status') != 'polled':
                                needs_scan = True
                                scan_failed = True
                                states[thread_id] = poll
                                continue
                            needs_scan = needs_scan or poll.get('has_more', False)
                        states[thread_id] = daemon.notify_attention(thread_id, daemon.process_thread(thread_id))
                    except Exception as exc:
                        needs_scan = True
                        scan_failed = True
                        states[thread_id] = {'status': 'error', 'code': type(exc).__name__}
                write_status(args.state_dir, {'status': 'running', 'listener_mode': watcher.mode,
                                             'execution_backend': 'shared_app', 'app_connected': shared.ready(), 'targets': states})
                if args.command == 'once':
                    print(json.dumps(states))
                    break
                if not targets:
                    watcher.close()
                    for _ in range(30):
                        if stop:
                            break
                        time.sleep(1)
                    dirty = True
                    continue
                try:
                    dirty = watcher.wait(timeout=1 if needs_scan and not scan_failed else 30) or needs_scan
                except Exception as exc:
                    write_status(args.state_dir, {'status': 'reconnecting', 'code': type(exc).__name__, 'targets': states})
                    for _ in range(30):
                        if stop:
                            break
                        time.sleep(1)
                    dirty = True
        finally:
            watcher.close()
            write_status(args.state_dir, {'status': 'stopped'})


if __name__ == '__main__':
    main()
