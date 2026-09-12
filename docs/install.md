# macOS installation

The installer creates a per-user Python environment, installs the `qq-mail` skill and worker, and configures two LaunchAgents. It uses the native CLI bundled in Codex App. It does not send email, invoke a model, or enable any task for email replies.

## Requirements

- macOS, Python 3.11 or newer, and a working signed-in Codex App.
- QQ Mail with IMAP/SMTP enabled and a QQ **authorization code**. Your QQ login password is not the authorization code.
- The native Codex CLI must support App-server Unix sockets, `app-server proxy`, and queued task submissions. This project was tested with bundled CLI **0.153.4**. Desktop launch options and protocol methods may change in future releases.
- Keep the Mac awake, connected to the network, and Codex App open during remote use. Closing the lid or sleeping stops useful mail processing.

## Review the installation

Clone the repository, enter its directory, and run:

```sh
python3 scripts/install.py --account you@qq.com --register-mcp --dry-run
```

Replace `you@qq.com` with the account you will both send from and receive at. The account appears only in private local configuration. No password argument is accepted. The script looks for the bundled executable in `/Applications/Codex.app` or `/Applications/ChatGPT.app`. If needed, specify the real native executable:

```sh
python3 scripts/install.py --account you@qq.com \
  --codex-cli '/Applications/Codex.app/Contents/Resources/codex' \
  --register-mcp --dry-run
```

`--dry-run` performs no writes, network requests, launchd changes, or model calls. It prints the paths, account, and any existing path conflicts. It is a plan, not a live health check.

The installer refuses to replace an existing skill, state directory, launcher, LaunchAgent, `qq-mail` MCP registration, or custom App execution environment. This includes earlier private installations. There is deliberately no automatic migration of existing mail history or active execution claims. Back up and resolve an earlier installation before installing this release; do not delete its state to force installation while replies are pending.

## Install

Run the reviewed command again without `--dry-run`:

```sh
python3 scripts/install.py --account you@qq.com --register-mcp
```

The installer downloads the pinned `websockets==16.0` dependency from the configured Python package index. `--register-mcp` explicitly adds the `qq-mail` stdio MCP registration using `codex mcp add`; other registrations are preserved. Omit the flag to manage that registration yourself.

Default locations:

| Item | Path |
| --- | --- |
| Skill and scripts | `~/.agents/skills/qq-mail/` |
| Private configuration, Python environment and state | `~/Library/Application Support/Codex QQ Mail/` |
| CLI launcher | `~/.local/bin/codex-qq-shared` |
| Listener LaunchAgent | `~/Library/LaunchAgents/io.github.codex-qq-mail.listener.plist` |
| App environment LaunchAgent | `~/Library/LaunchAgents/io.github.codex-qq-mail.app-env.plist` |

Advanced installations may use `--install-root` and `--state-dir`. The generated launcher and MCP registration set `CODEX_QQ_MAIL_STATE_DIR` for their children. Keep that setting consistent when invoking workers manually.

### Store the authorization code in Keychain

Open **Keychain Access**, select your login keychain, and create a new generic password item:

| Field | Value |
| --- | --- |
| Keychain Item Name / service | `codex-qq-smtp-auth-code` |
| Account Name | The exact QQ login address passed to `--account` |
| Password | The QQ IMAP/SMTP authorization code |

Enter the code directly in Keychain Access. Do not place it in shell arguments, shell history, `.env`, screenshots, Git, or the Codex conversation. If macOS requests access when the worker first uses the item, allow the expected local worker access. The worker reads the credential internally through the macOS `security` utility and never returns it through MCP.

### Activate the App connection once

Finish active tasks, quit Codex App once, then reopen it normally. This initial restart loads the `CODEX_CLI_PATH` launch environment. The installer does not quit or kill the App for you. Afterwards, **leave the App open during normal email use**; individual emails do not require a restart.

The bridge preserves the App's native launch configuration and joins the listener to the same private App-server. It does not connect to the App's private IPC endpoint or bypass peer authorization. A future Codex release may need a compatibility update; check real task execution after an App upgrade.

## Enable and verify one existing task

In a task that you created manually in Codex App, ask Codex to enable email continuation for that task. The skill uses the verified current task UUID, registers a stable mail conversation, and calls `configure_email_listener` only with your authorization. Starting the listener alone does not enable any task.

Ask it to send one test notification. Reply directly to that notification using the configured QQ account. A useful test is to ask it to read a harmless local test file and return its marker. Verify all three outcomes:

1. Your reply and its answer appear in the original App task.
2. The answer contains the actual local file marker.
3. The result email arrives in the same mail conversation.

SMTP acceptance alone does not prove inbox delivery. A two-client protocol test alone does not prove App acceptance. If any of these checks fails, retain the state and diagnose before retrying the same instruction.

Replies are verified against the account's authenticated **Sent Messages** folder and standard reply headers, not arbitrary incoming mail. Use the same QQ account in your phone/mail client. An account alias requires deliberate local confirmation; changing `From` or knowing a task UUID does not grant access.

## Inspect and remove

Check the LaunchAgent directly, then inspect the saved daemon state:

```sh
launchctl print "gui/$(id -u)/io.github.codex-qq-mail.listener"
"$HOME/Library/Application Support/Codex QQ Mail/runtime/bin/python" \
  "$HOME/.agents/skills/qq-mail/scripts/qq_mail_daemon.py" status
```

Saved status is diagnostic evidence; its PID alone is not a liveness check. Logs and SQLite state may contain pending replies, execution output and private file paths. Do not publish the state directory.

To remove this release, run the uninstaller from the repository:

```sh
python3 scripts/uninstall.py --dry-run
python3 scripts/uninstall.py
```

Quit Codex App before actual removal so its configured bridge can be removed cleanly. If a task is still finishing after the App closes, let the shared backend and its supervisor exit first; the uninstaller checks their process identities and refuses to remove active worker code. It removes only an installation with this installer's receipt and checks whether the launcher, LaunchAgents or MCP registration has been replaced. It preserves the private state directory, Python environment, and Keychain item. Remove those separately only after reviewing pending results and backing up any history you want to keep. Reopen Codex App to use its normal backend again.
