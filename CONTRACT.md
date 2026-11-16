# Service contracts

Python 3.11+ standard-library implementation. The client web service and MCP service are independent HTTPS processes. All model-visible tools execute on the MCP service. Client processes hold transient connections and computation only. Sessions, configuration, tool-call checkpoints and job results live on the server.

## Server workspace API

`server.workspace.WorkspaceService(data_dir: Path)` manages SQLite state and tenant workspace mappings. Public methods `ensure_tenant(tenant_id, workspace: Path)`, `tool_specs() -> list[dict]`, `call(tenant_id, name, arguments, context=None) -> dict`. Invalid input raises ValueError, missing resources raise LookupError, conflicting revisions/leases raise RuntimeError. Tool application failures may return a structured result carrying `error`.

`context` contains `session_id`, `operation_id`, `lease_token` when supplied by the runtime. HTTP adapts MCP `params._meta` keys `io.mcp-agent/session_id`, `io.mcp-agent/operation_id`, `io.mcp-agent/lease_token` into these names. Tenant identity comes exclusively from verified access tokens.

All tool specs have MCP name/description/inputSchema. Control tools carry `_meta: {"io.mcp-agent/visibility": "runtime"}`. Model-visible tools include `shell`, `job_status`, `job_cancel`. Shell accepts command, optional cwd (workspace-relative), optional timeout_seconds. It returns durable job_id and status immediately. The runtime polls job_status to collect its final tool result. Each shell dispatch carries a stable operation_id and is deduplicated remotely. Commands execute in the real configured workspace. Jobs survive web-client and MCP HTTP process restart.

Control tools and shapes:

- `workspace_info {}` -> {tenant_id, workspace, skills: [{name,description,path}], instructions}
- `files_list {path?}` -> {path, entries: [{name,path,type,size}]}
- `file_read {path}` -> {path, content, revision, size}; textual files, bounded output.
- `sessions_list {}` -> {sessions: [{id,title,status,updated_at,revision}]}
- `session_create {title?,config?}` -> full session
- `session_get {session_id}` -> full session
- `session_update {session_id,expected_revision,patch,lease_token?}` -> full session. Merge top-level patch fields, increment revision. Active lease requires matching token. Session fields id, tenant_id, revision and lease are protected.
- `session_claim {session_id,owner,ttl?}` -> {lease_token,expires_at,session}. Renew same owner, reject active different owner, issue fresh lease after expiry.
- `session_release {session_id,lease_token}` -> full session

Full session includes id, title, status (idle/running/completed/error/cancelled), config, messages (provider-neutral list), pending (optional persisted tool execution checkpoint), error (optional), revision, created_at, updated_at. Lease lifetime default 30s, renew every 10s. Timestamps UNIX seconds. Shell job result includes job_id, status (queued/running/completed/failed/cancelled), stdout, stderr, exit_code; status responses bounded with optional offset/next_offset for logs. User cancellation is explicit and separate from client disconnection.

## Client runtime API

`client.mcp.MCPClient(endpoint, access_token, ca_file=None)` offers `call(method, params=None)`, `list_tools()` and `call_tool(name, arguments, context=None)` returning decoded structured tool results. It enforces HTTPS and certificate verification, supports JSON and SSE, metadata/header matching and error decoding. `client.runtime.AgentRunner(mcp, model_factory=None)` offers `run(session_id, on_event=None)` and `cancel(session_id)`. The runner acquires/renews a remote lease, restores checkpoints and persists results. No workspace filesystem or subprocess operations in client code.

Session config includes provider (openai or anthropic), model, base_url, api_format (chat/responses/auto), max_output_tokens, context_window, api_key (stored remotely, redact in browser responses except dedicated configuration update). Provider credentials can fall back to configured client process environment. Providers should reuse skills-agent provider implementation where feasible. A model fixture belongs only in tests.

## Client web API and UI

Root owner implements client/app.py to serve client/static and JSON API. Frontend uses these same-origin routes:

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

UI polls session while running, displays messages and tool calls/results, supports replay, errors/retry/cancel, new session, settings and remote workspace file inspection. Request mutations carry `X-Requested-With: mcp-agent` and same-origin fetch. Browser refresh restores selected session from URL; no browser storage of credentials. UI initially displays a real login/connect state, never mock messages or fake progress. Chinese primary UI, restrained developer-workbench aesthetic, accessible responsive layout.

## Collaboration ownership

Root owns server/app.py, server/auth.py, client/app.py, common/, scripts/, root packaging/docs and integration tests. Server agent owns server/workspace.py, server/job_worker.py, server/skills/, tests/test_workspace.py. Runtime agent owns client/mcp.py, client/runtime.py, client/providers/, client/context.py, related provider dependencies and tests/test_runtime.py, tests/test_mcp.py. Frontend agent owns client/static/. Agents do not commit until root requests a scoped commit. No edits to skills-agent or mcp-from-scratch source.
