# Service contracts

Python 3.11+ standard-library implementation. The client web service and MCP service are independent HTTPS processes. All model-visible tools execute on the MCP service. Client processes hold transient connections and computation only. Sessions, configuration, tool-call checkpoints and job results live on the server.

## Server workspace API

`server.workspace.WorkspaceService(data_dir: Path)` manages SQLite state and tenant workspace mappings. Public methods `ensure_tenant(tenant_id, workspace: Path, execution_user=None)`, `tool_specs() -> list[dict]`, `call(tenant_id, name, arguments, context=None) -> dict`. Invalid input raises ValueError, missing resources raise LookupError, conflicting revisions/leases raise RuntimeError. HTTP adapts these exceptions into MCP tool errors with structured error codes. A successful job/session document may itself contain an `error` field; this does not turn the read operation into an MCP tool error.

`context` contains `session_id`, `operation_id`, `lease_token` when supplied by the runtime. HTTP adapts MCP `params._meta` keys `io.mcp-agent/session_id`, `io.mcp-agent/operation_id`, `io.mcp-agent/lease_token` into these names. Tenant identity comes exclusively from verified access tokens.

All tool specs have MCP name/description/inputSchema. Control tools carry `_meta: {"io.mcp-agent/visibility": "runtime"}`. Model-visible tools include `shell`, `job_status`, `job_cancel`. Shell accepts command, optional cwd (workspace-relative), optional timeout_seconds. It returns durable job_id and status immediately. The runtime polls job_status to collect its final tool result. Each shell dispatch carries a stable operation_id and is deduplicated remotely. Commands execute in the real configured workspace. Jobs survive web-client and MCP HTTP process restart.

Control tools and shapes:

- `workspace_info {}` -> {tenant_id, workspace, skills: [{name,description,path}], instructions}
- `files_list {path?}` -> {path, entries: [{name,path,type,size}]}
- `file_read {path}` -> {path, content, revision, size}; textual files, bounded output.
- `file_download {path}` -> {path, content_base64, mime_type, revision, size}; at most 16 MiB. Large content appears only in structuredContent, with a small textual summary.
- `file_upload {path,content_base64,expected_revision?}` -> {path, revision, size}; at most 8 MiB. Missing/null expected_revision means create-only; replacing a file requires its current SHA-256 revision.
- `sessions_list {}` -> {sessions: [{id,title,status,updated_at,revision}]}
- `session_create {title?,config?}` -> full session
- `session_get {session_id}` -> full session
- `session_update {session_id,expected_revision,patch,lease_token?}` -> full session. Merge top-level patch fields, increment revision. Active lease requires matching token. Session fields id, tenant_id, revision and lease are protected.
- `session_claim {session_id,owner,ttl?}` -> {lease_token,expires_at,session}. Renew same owner, reject active different owner, issue fresh lease after expiry.
- `session_release {session_id,lease_token}` -> full session
- `session_cancel {session_id}` -> full session; sets an independent durable cancellation flag without racing the active runner's message revision.

Full session includes id, title, status (idle/running/completed/error/cancelled), config, messages (provider-neutral list), pending (optional persisted tool execution checkpoint), context (derived summary/projection), draft, activity, error (optional), revision, created_at, updated_at. Lease lifetime default 30s, renew every 10s. Timestamps UNIX seconds. Shell job result includes job_id, status (queued/running/completed/failed/cancelled), stdout, stderr, exit_code; status responses bounded with optional offset/next_offset for logs. User cancellation is explicit and separate from client disconnection.

All tool calls require `workspace:read`. Session operations additionally require `agent:run` because full session documents contain model credentials. `shell`, `job_cancel`, and `file_upload` also require `workspace:write` and `agent:run`. `sessions_list` exposes summary metadata only. OAuth does not by itself constrain the OS permissions of a shell; see README deployment boundaries.

## Client runtime API

`client.mcp.MCPClient(endpoint, access_token, ca_file=None)` offers `call(method, params=None)`, `list_tools()` and `call_tool(name, arguments, context=None)` returning decoded structured tool results. It enforces HTTPS and certificate verification, supports JSON and SSE, metadata/header matching and error decoding. `client.runtime.AgentRunner(mcp, model_factory=None)` offers `run(session_id, on_event=None)` and `cancel(session_id)`. The runner acquires/renews a remote lease, restores checkpoints and persists results. No workspace filesystem or subprocess operations in client code.

Session config includes provider (openai or anthropic), model, base_url, api_format (chat/responses/auto), max_output_tokens, context_window, api_key (stored remotely, never echoed to the browser). Provider credentials can fall back to configured client process environment; resolved credentials are persisted before the first model request. Provider adapters originate from skills-agent. Model fixtures exist only in tests.

## Client web API and UI

`client/app.py` serves `client/static` and the JSON API. Frontend uses these same-origin routes:

- GET /api/bootstrap -> {authenticated, mcp_url, user?, workspace?, skills?, tools?, model_defaults?}; anonymous responses expose authenticated false and mcp_url. Login via GET /auth/login, logout via POST /auth/logout.
- GET /api/sessions -> {sessions:[...]}
- POST /api/sessions {title?,config?} -> session
- GET /api/sessions/{id} -> full session (config.api_key redacted as api_key_set)
- PATCH /api/sessions/{id} {expected_revision,patch} -> session
- POST /api/sessions/{id}/messages {content,request_id} -> session (persist user message, launch/resume runner)
- POST /api/sessions/{id}/resume -> {accepted:true}
- POST /api/sessions/{id}/cancel -> {accepted:true}
- GET /api/files?path= -> file list
- GET /api/file?path= -> file read
- GET /api/download?path= -> bounded attachment with original filename
- POST /api/upload {path,content_base64,expected_revision?} -> uploaded file metadata
- POST /auth/refresh -> rotates access/refresh cookies; browser retries an expired API request once

UI polls session while running, displays messages and tool calls/results, supports replay, errors/retry/cancel, new session, settings and remote workspace file inspection. Request mutations carry `X-Requested-With: mcp-agent` and same-origin fetch. Browser refresh restores selected session from URL; no browser storage of credentials. UI initially displays a real login/connect state, never mock messages or fake progress. Chinese primary UI, restrained developer-workbench aesthetic, accessible responsive layout.

## Transport and replacement

The endpoint accepts MCP 2026-07-28 JSON-RPC requests via POST `/mcp`. Each request carries protocol version and client capabilities in `_meta`, with the corresponding MCP HTTP headers. `server/discover` and `tools/list` are authorization-sensitive and uncached (`ttlMs: 0`, `cacheScope: private`). The service does not require a transport session or initialize handshake.

Client processes may retain transient worker threads and provider connections. Remote leases remain authoritative even when multiple client processes are present. Browser authorization lives in Secure/HttpOnly cookies; interchangeable workers behind the same origin share the cookie-signing secret. Separate origins perform their own OAuth login to reach the same remote tenant data.

Only shell jobs implement operation-id deduplication in this server. New tools must provide their own replay-safe execution semantics if their calls should survive an ambiguous transport failure. Working directory constraints are not a sandbox; shell uses actual OS account permissions.
