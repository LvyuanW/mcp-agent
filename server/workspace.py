"""Durable tenant workspaces, agent sessions and supervised shell operations."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4


class InputError(ValueError):
    code = "invalid_input"


class NotFoundError(LookupError):
    code = "not_found"


class ConflictError(RuntimeError):
    def __init__(self, message, code="conflict"):
        super().__init__(message)
        self.code = code


FINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
SESSION_STATUSES = {"idle", "running", "completed", "error", "cancelled"}
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
MAX_FILE_BYTES = 512 * 1024
MAX_LOG_BYTES = 1024 * 1024


def connect_database(path):
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def shell_environment(workspace, job_id):
    """Pass a deliberate environment allowlist to the independent worker."""
    allowed = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.setdefault("PATH", os.defpath)
    environment.update({"PWD": str(workspace), "MCP_WORKSPACE": str(workspace),
                        "MCP_SKILLS_DIR": str(SKILLS_DIR), "MCP_JOB_ID": job_id,
                        "PYTHONUNBUFFERED": "1"})
    return environment


def _text(value, name, *, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise InputError(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _number(value, name, *, minimum=0, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputError(f"{name} must be a finite number")
    if not minimum < value <= maximum:
        raise InputError(f"{name} must be greater than {minimum} and at most {maximum}")
    return value


def _schema(name, description, properties=None, required=(), *, runtime=True):
    specification = {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": list(required),
        "additionalProperties": False,
    }}
    if runtime:
        specification["_meta"] = {"io.mcp-agent/visibility": "runtime"}
    return specification


class WorkspaceService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(exist_ok=True, mode=0o700)
        self.database = self.data_dir / "workspace.sqlite3"
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, created_at REAL NOT NULL,
                    execution_user TEXT, execution_uid INTEGER, execution_gid INTEGER, execution_home TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    document TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    lease_owner TEXT, lease_token TEXT, lease_expires REAL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sessions_tenant ON sessions(tenant_id, updated_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    session_id TEXT NOT NULL DEFAULT '', operation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL,
                    timeout_seconds REAL NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    started_at REAL, ended_at REAL, worker_pid INTEGER, command_pid INTEGER,
                    heartbeat REAL, exit_code INTEGER, error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(tenant_id, session_id, operation_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_tenant ON jobs(tenant_id, created_at);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tenants)")}
            for name, kind in (("execution_user", "TEXT"), ("execution_uid", "INTEGER"),
                               ("execution_gid", "INTEGER"), ("execution_home", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tenants ADD COLUMN {name} {kind}")
        os.chmod(self.database, 0o600)
        self._recover_queued_jobs()

    @contextmanager
    def _connection(self, *, transaction=False):
        connection = connect_database(self.database)
        try:
            if transaction:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if transaction:
                connection.commit()
        except BaseException:
            if transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_tenant(self, tenant_id, workspace: Path, *, execution_user=None):
        tenant_id = _text(tenant_id, "tenant_id", maximum=200)
        workspace = Path(workspace).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise InputError("The configured workspace must be an existing directory")
        account = None
        if execution_user is not None:
            execution_user = _text(execution_user, "execution_user", maximum=200)
            if os.geteuid() != 0:
                raise InputError("Configuring execution_user requires running the service as root")
            try:
                account = pwd.getpwnam(execution_user)
            except KeyError:
                raise InputError("Configured execution_user does not exist") from None
            if account.pw_uid == 0:
                raise InputError("execution_user must be a non-root OS account")
            check = subprocess.run([sys.executable, "-c",
                "import os,sys; sys.exit(0 if os.path.isdir(sys.argv[1]) and os.access(sys.argv[1], os.R_OK | os.W_OK | os.X_OK) else 1)",
                str(workspace)], user=account.pw_uid, group=account.pw_gid, extra_groups=[],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd="/", env={"PATH": os.defpath}, timeout=10, close_fds=True)
            if check.returncode:
                raise InputError("execution_user must be able to read, write and traverse the configured workspace")
        with self._connection(transaction=True) as connection:
            row = connection.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row and (row["workspace"] != str(workspace) or row["execution_user"] != execution_user):
                raise ConflictError("Tenant already has a different workspace or execution user mapping", "workspace_conflict")
            connection.execute("INSERT OR IGNORE INTO tenants(id, workspace, created_at, execution_user, execution_uid, "
                               "execution_gid, execution_home) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (tenant_id, str(workspace), time.time(), execution_user,
                                account.pw_uid if account else None, account.pw_gid if account else None,
                                account.pw_dir if account else None))

    def _workspace(self, connection, tenant_id):
        row = connection.execute("SELECT workspace FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if row is None:
            raise NotFoundError("Workspace not found")
        return Path(row["workspace"])

    def tool_specs(self):
        text = {"type": "string"}
        session = {"session_id": text}
        return [
            _schema("workspace_info", "Read this tenant's real workspace, remote skill paths and execution instructions."),
            _schema("files_list", "List a directory in the remote workspace.", {"path": text}),
            _schema("file_read", "Read a UTF-8 file in the remote workspace, bounded to 512 KiB.", {"path": text}, ["path"]),
            _schema("sessions_list", "List the authenticated tenant's sessions."),
            _schema("session_create", "Create a durable remote session.", {"title": text, "config": {"type": "object"}}),
            _schema("session_get", "Load a durable session and current cancellation signal.", session, ["session_id"]),
            _schema("session_update", "Compare and update session fields while holding its active lease.", {
                **session, "expected_revision": {"type": "integer", "minimum": 0},
                "patch": {"type": "object"}, "lease_token": text}, ["session_id", "expected_revision", "patch"]),
            _schema("session_claim", "Acquire or renew a session execution lease.", {
                **session, "owner": text, "ttl": {"type": "number", "exclusiveMinimum": 0, "maximum": 300}},
                ["session_id", "owner"]),
            _schema("session_release", "Release the current session lease.", {**session, "lease_token": text},
                    ["session_id", "lease_token"]),
            _schema("session_cancel", "Persist a cancellation request for the active session runner.", session, ["session_id"]),
            _schema("shell", "Start a durable shell job in the real remote workspace. Read remote-shell SKILL.md "
                    "from workspace_info when needed. Returns a job_id immediately. The runtime collects its final "
                    "result. This uses the service account's actual OS permissions.", {
                "command": {"type": "string", "description": "Shell command or multiline script."},
                "cwd": {"type": "string", "description": "Optional workspace-relative working directory."},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400,
                                    "description": "Execution deadline in seconds. Default 300."}}, ["command"], runtime=False),
            _schema("job_status", "Read a durable shell job and its bounded stdout/stderr. Offsets are byte positions "
                    "per stream. Follow next_offset while truncated is true to read remaining logs.", {
                "job_id": text, "offset": {"anyOf": [{"type": "integer", "minimum": 0}, {
                    "type": "object", "properties": {"stdout": {"type": "integer", "minimum": 0},
                    "stderr": {"type": "integer", "minimum": 0}}, "additionalProperties": False}]},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_BYTES}}, ["job_id"], runtime=False),
            _schema("job_cancel", "Request cancellation of a remote shell job. Poll job_status until terminal.",
                    {"job_id": text}, ["job_id"], runtime=False),
        ]

    def call(self, tenant_id, name, arguments, context=None):
        if not isinstance(arguments, dict) or not isinstance(context or {}, dict):
            raise InputError("Tool arguments and context must be objects")
        specification = next((item for item in self.tool_specs() if item["name"] == name), None)
        if specification is None:
            raise NotFoundError("Tool not found")
        schema = specification["inputSchema"]
        if not set(schema["required"]) <= set(arguments) <= set(schema["properties"]):
            raise InputError("Missing required or unexpected tool arguments")
        with self._connection() as connection:
            workspace = self._workspace(connection, tenant_id)
        handlers = {
            "workspace_info": self._workspace_info, "files_list": self._files_list, "file_read": self._file_read,
            "sessions_list": self._sessions_list, "session_create": self._session_create,
            "session_get": self._session_get, "session_update": self._session_update,
            "session_claim": self._session_claim, "session_release": self._session_release,
            "session_cancel": self._session_cancel, "shell": self._shell,
            "job_status": self._job_status, "job_cancel": self._job_cancel,
        }
        return handlers[name](tenant_id, workspace, arguments, context or {})

    def _workspace_info(self, tenant_id, workspace, arguments, context):
        with self._connection() as connection:
            tenant = connection.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        account = tenant["execution_user"] or pwd.getpwuid(os.geteuid()).pw_name
        skills = []
        for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            fields = {}
            for line in path.read_text(encoding="utf-8").split("---", 2)[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()
            skills.append({"name": fields.get("name", path.parent.name),
                           "description": fields.get("description", ""), "path": str(path)})
        return {"tenant_id": tenant_id, "workspace": str(workspace), "skills": skills,
                "execution_user": account, "dedicated_execution_user": tenant["execution_user"] is not None,
                "instructions": "All tools and skills operate on the configured remote workspace. Use shell to "
                "read files, run commands, edit code and run tests. Paths in tool results belong to this remote host. "
                f"Read a relevant SKILL.md with shell before applying its workflow. Shell runs as OS account {account} "
                f"with working directory {workspace}. Commands receive an explicit environment allowlist. "
                "Inspect exit_code, stdout and stderr before reporting completion. "
                "Jobs continue independently of the client connection and expose durable status and cancellation."}

    def _path(self, workspace, value="."):
        if not isinstance(value, str) or "\0" in value:
            raise InputError("path must be a string")
        candidate = (workspace / value).resolve()
        if not candidate.is_relative_to(workspace):
            raise InputError("Path must remain inside the configured workspace")
        return candidate

    def _files_list(self, tenant_id, workspace, arguments, context):
        path = self._path(workspace, arguments.get("path", "."))
        if not path.is_dir():
            raise NotFoundError("Directory not found")
        entries = []
        for entry in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            if len(entries) >= 2000:
                break
            info = entry.lstat()
            kind = "symlink" if entry.is_symlink() else "directory" if entry.is_dir() else "file"
            entries.append({"name": entry.name, "path": entry.relative_to(workspace).as_posix(),
                            "type": kind, "size": info.st_size})
        return {"path": path.relative_to(workspace).as_posix(), "entries": entries,
                "truncated": len(entries) == 2000}

    def _file_read(self, tenant_id, workspace, arguments, context):
        path = self._path(workspace, arguments["path"])
        if not path.is_file():
            raise NotFoundError("File not found")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InputError("File exceeds the 512 KiB browser limit; inspect a selected range with shell")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise InputError("File is not UTF-8 text") from None
        return {"path": path.relative_to(workspace).as_posix(), "content": content,
                "revision": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def _session_row(self, connection, tenant_id, session_id):
        session_id = _text(session_id, "session_id", maximum=200)
        row = connection.execute("SELECT * FROM sessions WHERE id = ? AND tenant_id = ?",
                                 (session_id, tenant_id)).fetchone()
        if row is None:
            raise NotFoundError("Session not found")
        return row

    def _session_document(self, row):
        document = json.loads(row["document"])
        document.update(id=row["id"], tenant_id=row["tenant_id"], revision=row["revision"],
                        created_at=row["created_at"], updated_at=row["updated_at"],
                        cancel_requested=bool(row["cancel_requested"]))
        document["lease"] = ({"owner": row["lease_owner"], "expires_at": row["lease_expires"],
                               "epoch": row["lease_epoch"]} if row["lease_token"] else None)
        return document

    def _sessions_list(self, tenant_id, workspace, arguments, context):
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM sessions WHERE tenant_id = ? ORDER BY updated_at DESC",
                                      (tenant_id,)).fetchall()
        documents = [self._session_document(row) for row in rows]
        return {"sessions": [{key: document[key] for key in ("id", "title", "status", "updated_at", "revision")}
                             for document in documents]}

    def _session_create(self, tenant_id, workspace, arguments, context):
        title = _text(arguments.get("title", "新会话"), "title", maximum=500)
        config = arguments.get("config", {})
        if not isinstance(config, dict):
            raise InputError("config must be an object")
        identifier, now = uuid4().hex, time.time()
        document = {"title": title, "status": "idle", "config": config, "messages": [],
                    "pending": None, "error": None}
        with self._connection(transaction=True) as connection:
            connection.execute("INSERT INTO sessions(id, tenant_id, document, revision, created_at, updated_at) "
                               "VALUES (?, ?, ?, 0, ?, ?)",
                               (identifier, tenant_id, json.dumps(document, ensure_ascii=False), now, now))
            return self._session_document(self._session_row(connection, tenant_id, identifier))

    def _session_get(self, tenant_id, workspace, arguments, context):
        with self._connection() as connection:
            return self._session_document(self._session_row(connection, tenant_id, arguments["session_id"]))

    def _check_lease(self, row, token):
        active = row["lease_token"] is not None and row["lease_expires"] > time.time()
        if active and not (isinstance(token, str) and secrets.compare_digest(token, row["lease_token"])):
            raise ConflictError("Session has an active execution lease", "lease_conflict")
        if token is not None and not active:
            raise ConflictError("Session execution lease expired or was released", "lease_conflict")

    def _session_update(self, tenant_id, workspace, arguments, context):
        revision, patch = arguments["expected_revision"], arguments["patch"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise InputError("expected_revision must be a nonnegative integer")
        if not isinstance(patch, dict):
            raise InputError("patch must be an object")
        protected = {"id", "tenant_id", "revision", "lease", "created_at", "updated_at"}
        if protected.intersection(patch):
            raise InputError("Session identity, revision, timestamps and lease are protected")
        if "status" in patch and patch["status"] not in SESSION_STATUSES:
            raise InputError("Invalid session status")
        for field, expected_type in (("config", dict), ("messages", list), ("cancel_requested", bool)):
            if field in patch and not isinstance(patch[field], expected_type):
                raise InputError(f"{field} has the wrong type")
        if "title" in patch:
            _text(patch["title"], "title", maximum=500)
        with self._connection(transaction=True) as connection:
            row = self._session_row(connection, tenant_id, arguments["session_id"])
            self._check_lease(row, arguments.get("lease_token", context.get("lease_token")))
            if row["revision"] != revision:
                raise ConflictError("Session revision changed; reload before updating", "revision_conflict")
            document = json.loads(row["document"])
            document.update({key: value for key, value in patch.items() if key != "cancel_requested"})
            connection.execute("UPDATE sessions SET document = ?, revision = revision + 1, updated_at = ?, "
                               "cancel_requested = ? WHERE id = ?", (json.dumps(document, ensure_ascii=False), time.time(),
                               int(patch.get("cancel_requested", row["cancel_requested"])), row["id"]))
            return self._session_document(self._session_row(connection, tenant_id, row["id"]))

    def _session_claim(self, tenant_id, workspace, arguments, context):
        owner = _text(arguments["owner"], "owner", maximum=200)
        ttl = _number(arguments.get("ttl", 30), "ttl", maximum=300)
        now = time.time()
        with self._connection(transaction=True) as connection:
            row = self._session_row(connection, tenant_id, arguments["session_id"])
            active = row["lease_token"] and row["lease_expires"] > now
            if active and row["lease_owner"] != owner:
                raise ConflictError("Another runner holds the session lease", "lease_conflict")
            token = row["lease_token"] if active else secrets.token_urlsafe(32)
            epoch = row["lease_epoch"] if active else row["lease_epoch"] + 1
            connection.execute("UPDATE sessions SET lease_owner = ?, lease_token = ?, lease_expires = ?, "
                               "lease_epoch = ? WHERE id = ?", (owner, token, now + ttl, epoch, row["id"]))
            return {"lease_token": token, "expires_at": now + ttl,
                    "session": self._session_document(self._session_row(connection, tenant_id, row["id"]))}

    def _session_release(self, tenant_id, workspace, arguments, context):
        token = _text(arguments["lease_token"], "lease_token")
        with self._connection(transaction=True) as connection:
            row = self._session_row(connection, tenant_id, arguments["session_id"])
            if row["lease_token"] and not secrets.compare_digest(row["lease_token"], token):
                raise ConflictError("Lease token no longer owns this session", "lease_conflict")
            connection.execute("UPDATE sessions SET lease_owner = NULL, lease_token = NULL, lease_expires = NULL "
                               "WHERE id = ?", (row["id"],))
            return self._session_document(self._session_row(connection, tenant_id, row["id"]))

    def _session_cancel(self, tenant_id, workspace, arguments, context):
        with self._connection(transaction=True) as connection:
            row = self._session_row(connection, tenant_id, arguments["session_id"])
            connection.execute("UPDATE sessions SET cancel_requested = 1 WHERE id = ?", (row["id"],))
            connection.execute("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE tenant_id = ? "
                               "AND session_id = ? AND status IN ('queued', 'running')",
                               (time.time(), tenant_id, row["id"]))
            return self._session_document(self._session_row(connection, tenant_id, row["id"]))

    def _shell(self, tenant_id, workspace, arguments, context):
        command = _text(arguments["command"], "command", maximum=1024 * 1024)
        operation_id = _text(context.get("operation_id"), "context.operation_id", maximum=300)
        cwd = self._path(workspace, arguments.get("cwd", "."))
        if not cwd.is_dir():
            raise NotFoundError("Working directory not found")
        timeout = _number(arguments.get("timeout_seconds", 300), "timeout_seconds")
        session_id = context.get("session_id", "")
        normalized = json.dumps({"command": command, "cwd": str(cwd), "timeout_seconds": timeout}, sort_keys=True)
        request_hash = hashlib.sha256(normalized.encode()).hexdigest()
        now = time.time()
        with self._connection(transaction=True) as connection:
            if session_id:
                session = self._session_row(connection, tenant_id, session_id)
                self._check_lease(session, context.get("lease_token"))
                if not session["lease_token"] or session["lease_expires"] <= now:
                    raise ConflictError("Shell dispatch requires an active session lease", "lease_conflict")
                if session["cancel_requested"]:
                    raise ConflictError("Session cancellation was requested", "session_cancelled")
            row = connection.execute("SELECT * FROM jobs WHERE tenant_id = ? AND session_id = ? AND operation_id = ?",
                                     (tenant_id, session_id, operation_id)).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise ConflictError("Operation ID was already used with different arguments", "operation_conflict")
                identifier = row["id"]
            else:
                identifier = uuid4().hex
                (self.jobs_dir / identifier).mkdir(mode=0o700)
                connection.execute("INSERT INTO jobs(id, tenant_id, session_id, operation_id, request_hash, command, "
                                   "cwd, timeout_seconds, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                                   (identifier, tenant_id, session_id, operation_id, request_hash, command, str(cwd), timeout, now, now))
        self._start_worker(identifier)
        return self._job_status(tenant_id, workspace, {"job_id": identifier}, context)

    def _start_worker(self, identifier):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (identifier,)).fetchone()
            if row is None or row["status"] != "queued":
                return
        directory = self.jobs_dir / identifier
        directory.mkdir(exist_ok=True, mode=0o700)
        try:
            with (directory / "supervisor.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("job_worker.py")),
                                            "--database", str(self.database), "--job", identifier],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           cwd=row["cwd"], env=shell_environment(row["cwd"], identifier),
                                           start_new_session=True, close_fds=True)
            threading.Thread(target=process.wait, daemon=True).start()
        except OSError as error:
            with self._connection(transaction=True) as connection:
                connection.execute("UPDATE jobs SET error = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                                   (f"Worker launch failed: {error}. A retry can resume this queued operation.", time.time(), identifier))

    def _recover_queued_jobs(self):
        with self._connection() as connection:
            rows = connection.execute("SELECT id FROM jobs WHERE status = 'queued'").fetchall()
        for row in rows:
            self._start_worker(row["id"])

    def _job_row(self, connection, tenant_id, arguments, context):
        identifier = _text(arguments["job_id"], "job_id", maximum=200)
        row = connection.execute("SELECT * FROM jobs WHERE id = ? AND tenant_id = ?", (identifier, tenant_id)).fetchone()
        if row is None or (context.get("session_id") and row["session_id"] != context["session_id"]):
            raise NotFoundError("Job not found")
        return row

    def _job_status(self, tenant_id, workspace, arguments, context):
        offset = arguments.get("offset", 0)
        offsets = {"stdout": offset, "stderr": offset} if isinstance(offset, int) else offset
        if not isinstance(offsets, dict) or set(offsets) - {"stdout", "stderr"}:
            raise InputError("offset must be a byte offset or an object with stdout/stderr byte offsets")
        for value in offsets.values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InputError("Log offsets must be nonnegative integers")
        maximum = arguments.get("max_bytes", 65536)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_LOG_BYTES:
            raise InputError("max_bytes must be an integer between 1 and 1048576")
        with self._connection() as connection:
            row = self._job_row(connection, tenant_id, arguments, context)
        result = {key: row[key] for key in ("status", "exit_code", "created_at", "started_at", "ended_at", "error")}
        result.update(job_id=row["id"], cancel_requested=bool(row["cancel_requested"]), truncated=False)
        next_offset = {}
        for stream in ("stdout", "stderr"):
            position = offsets.get(stream, 0)
            path = self.jobs_dir / row["id"] / f"{stream}.log"
            try:
                with path.open("rb") as source:
                    source.seek(position)
                    data = source.read(maximum)
                    remaining = bool(source.read(1))
            except FileNotFoundError:
                data, remaining = b"", False
            result[stream] = data.decode("utf-8", errors="replace")
            next_offset[stream] = position + len(data)
            result["truncated"] = result["truncated"] or remaining
        result["next_offset"] = next_offset
        return result

    def _job_cancel(self, tenant_id, workspace, arguments, context):
        with self._connection(transaction=True) as connection:
            row = self._job_row(connection, tenant_id, arguments, context)
            connection.execute("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? "
                               "AND status IN ('queued', 'running')", (time.time(), row["id"]))
        self._start_worker(row["id"])
        return self._job_status(tenant_id, workspace, arguments, context)
