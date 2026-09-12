# Security

Report vulnerabilities using this repository's private vulnerability reporting form under
**Security → Report a vulnerability**. Do not publish authorization bypass details together
with personal mail, credentials, or production logs in an issue.

This project runs locally and can continue an existing coding task with that task's permissions.
Only enable reply intake for tasks you intend to control by email. A conversation UUID routes
a message; it does not authenticate a sender. Intake verifies the configured account's
read-only Sent folder, the enable-time baseline, reply headers and previously submitted IDs.
Third-party messages moved into Sent are outside that trust model.

SMTP credentials belong in macOS Keychain. The repository and examples contain no credential.
Private local runtime state can contain pending messages and execution output. Keep it out of
Git, screenshots and support attachments. No listener check should call a model while idle.

Unknown execution or SMTP outcomes must not be retried blindly. Preserve the existing claim
and output event, inspect the recorded state, and follow the documented recovery flow.
