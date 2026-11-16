---
name: inspect-jobs
description: Inspect, page through logs, and explicitly cancel durable remote shell jobs.
---

Use `job_status` with `job_id` to inspect the latest state. States are `queued`, `running`, `completed`, `failed`, and `cancelled`. Queued and running jobs require a later check. Terminal responses include the command's exit code when it started, plus captured stdout/stderr and any supervisor error.

Output is bounded. When `truncated` is true, call `job_status` again using its `next_offset` object as `offset`. The object tracks stdout and stderr byte positions separately. `max_bytes` sets the per-stream page size from 1 to 1048576 bytes. For live incremental viewing, reuse the most recent offsets. To reread all available output, use offset 0.

Use `job_cancel` only when the user requests cancellation or the task requires stopping that work. Cancellation is durable and idempotent. Poll `job_status` until the job reaches a terminal state. The supervisor performs process-group termination and records completion. Closing a browser, losing an HTTP connection, or restarting the client does not itself cancel a durable job.

Treat unknown results or timeouts as uncertain until the durable status is checked. Do not rerun a side-effecting command merely because a client lost its connection. The runtime reuses the original operation ID to find the existing job.
