---
name: remote-shell
description: Inspect and modify a real remote project through durable shell jobs, then verify the result.
---

Use this workflow for files, source changes, commands and tests in the configured remote workspace.

The shell tool accepts `command`, an optional workspace-relative `cwd`, and an optional `timeout_seconds` (default 300, maximum 86400). Commands use `/bin/sh` and start in the real configured directory. All paths returned by tools are on that remote host. The runtime owns durable operation IDs and collects job results.

Inspect the relevant files and project instructions before editing. Prefer `rg` for text searches. Use installed project tools when available. Shell can run a Python heredoc to make a precise edit after verifying the expected source. Preserve changes belonging to other users or agents. When replacing content, check that the source still matches what was read and report a conflict when it differs.

Run a command that prints the selected evidence, performs the requested edit or check, and waits for its child work. Do not detach commands with `nohup`, `setsid`, or `&` to outlive a shell job. The supervisor cleans remaining processes in the command's process group when the entry shell exits or times out. Choose a sufficient timeout for tests and builds.

Check `status`, `exit_code`, `stdout`, `stderr`, and `error`. A successful launch only establishes that a job was registered. Confirm the final result before reporting completion. Use the inspect-jobs skill for status, logs, or cancellation.

Environment variables are allowlisted. Provider and OAuth secrets are not inherited into command environments. The actual OS account and filesystem permissions determine which files and services commands can access. Execute within the user's authorized scope and inspect project-specific configuration for the intended dev, test, or production environment.
