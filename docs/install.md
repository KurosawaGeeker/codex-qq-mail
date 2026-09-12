# Installing mail2agent

mail2agent connects existing desktop tasks to email through a local macOS service.
The workflow is the same across the product: notify, verify a reply, continue the
original task, then deliver its result. Account authentication and mailbox behavior
belong to the provider adapter.

## Requirements

- macOS, Python 3.11 or newer, and a signed-in Codex App.
- An account supported by an available provider adapter.
- Keep the Mac awake, online, and the App open while using email remotely.

The shared App-server integration was tested with bundled CLI 0.153.4. Its protocol
is experimental; check compatibility after upgrading the desktop App.

## Choose an adapter

| Adapter | Status | Setup |
| --- | --- | --- |
| QQ Mail | Implemented and exercised end to end | [Account, Keychain, installation and removal](providers/qq.md) |
| Other email providers | Not implemented yet | Provider support requires its own configuration and verification |

SMTP and IMAP availability alone does not establish support. Each adapter must
verify authentication, the authenticated Sent folder, reply headers, provider
Message-ID behavior, and recovery before being considered supported.

## Installation and acceptance

Follow the selected adapter's guide. Review the installer's dry run before applying.
The installer creates a private environment and worker, registers MCP when requested,
and configures the App's shared backend for its next launch. It refuses to overwrite
an existing installation or task history.

After finishing active work, restart Codex App once to load that connection. Leave it
open for normal remote use. In a manually created task, ask Codex to enable email
continuation and send a test notification. Reply to it with a harmless local file-read
request. Verify that the original task shows the instruction and result, and that the
result returns in the same email conversation.

## Existing installation identifiers

The first adapter's implementation still uses `qq-mail`, `CODEX_QQ_MAIL_*`, and
related script/service names. They are compatibility identifiers for existing local
installations. The product and repository are named **mail2agent**. This naming change
does not migrate local state or add support for an unimplemented provider.
