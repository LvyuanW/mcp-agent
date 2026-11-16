"""OAuth authorization-code + PKCE service with opaque, audience-bound tokens."""

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


SCOPES = frozenset({"workspace:read", "workspace:write", "agent:run"})


class AuthError(ValueError):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Principal:
    username: str
    tenant_id: str
    scopes: frozenset


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()


class OAuthService:
    def __init__(self, data_dir, issuer, redirect_uris, *, token_ttl=28800):
        self.path = Path(data_dir) / "auth.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.redirect_uris = frozenset(redirect_uris)
        self.token_ttl = int(token_ttl)
        for uri in self.redirect_uris:
            parsed = urlparse(uri)
            if parsed.scheme != "https" or not parsed.hostname or parsed.fragment or parsed.username:
                raise ValueError("OAuth redirect URIs must be registered HTTPS URLs")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codes (
                    digest TEXT PRIMARY KEY, username TEXT NOT NULL, client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
                    resource TEXT NOT NULL, scope TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    digest TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
                    username TEXT NOT NULL, client_id TEXT NOT NULL, resource TEXT NOT NULL,
                    scope TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_user(self, username, tenant_id, password):
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", username):
            raise ValueError("Username must use 1–100 letters, numbers, _, ., @ or -")
        if not tenant_id or len(tenant_id) > 100:
            raise ValueError("A tenant identifier is required")
        if len(password) < 12:
            raise ValueError("Use a password of at least 12 characters")
        salt = secrets.token_hex(24)
        with self.connection() as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                       "tenant_id=excluded.tenant_id,salt=excluded.salt,password_hash=excluded.password_hash",
                       (username, tenant_id, salt, password_hash(password, salt)))
            db.execute("DELETE FROM tokens WHERE username=?", (username,))
            db.execute("DELETE FROM codes WHERE username=?", (username,))

    def check_password(self, username, password):
        with self.connection() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        salt = user["salt"] if user else "00" * 24
        actual = password_hash(password, salt)
        if not user or not hmac.compare_digest(actual, user["password_hash"]):
            raise AuthError("用户名或密码错误", "access_denied", 401)
        return user

    def validate_authorization(self, params):
        required = ("client_id", "redirect_uri", "resource", "state", "code_challenge")
        if any(not params.get(key) for key in required):
            raise AuthError("Missing authorization parameter")
        if params["client_id"] != "mcp-agent-web" or params["redirect_uri"] not in self.redirect_uris:
            raise AuthError("Client or redirect URI is not registered")
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256":
            raise AuthError("Authorization code with PKCE S256 is required")
        if params["resource"] != self.resource:
            raise AuthError("Token audience must identify this MCP endpoint", "invalid_target")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params["code_challenge"]):
            raise AuthError("Invalid PKCE challenge")
        scopes = frozenset(params.get("scope", "").split())
        if not scopes or not scopes <= SCOPES:
            raise AuthError("Unsupported scope", "invalid_scope")
        if len(params["state"]) > 256:
            raise AuthError("Invalid state")
        return {key: str(value) for key, value in params.items()}

    def authorize(self, params, username, password):
        params = self.validate_authorization(params)
        self.check_password(username, password)
        code = secrets.token_urlsafe(40)
        with self.connection() as db:
            db.execute("DELETE FROM codes WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO codes VALUES (?,?,?,?,?,?,?,?)", (
                digest(code), username, params["client_id"], params["redirect_uri"],
                params["code_challenge"], params["resource"], params["scope"], time.time() + 120))
        return code

    def _issue_tokens(self, db, username, client_id, resource, scope, grant_id=None):
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        grant_id = grant_id or secrets.token_urlsafe(24)
        now = time.time()
        for token, kind, expiry in ((access, "access", now + self.token_ttl),
                                    (refresh, "refresh", now + 30 * 86400)):
            db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0)", (
                digest(token), kind, grant_id, username, client_id, resource, scope, expiry))
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": self.token_ttl, "scope": scope}

    def token(self, params):
        if params.get("client_id") != "mcp-agent-web":
            raise AuthError("Unknown OAuth client", "invalid_client")
        if params.get("resource") != self.resource:
            raise AuthError("Incorrect token audience", "invalid_target")
        grant_type = params.get("grant_type")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if grant_type == "authorization_code":
                row = db.execute("SELECT * FROM codes WHERE digest=?", (digest(params.get("code", "")),)).fetchone()
                verifier = params.get("code_verifier", "")
                if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                    raise AuthError("Invalid PKCE verifier", "invalid_grant")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                valid = row and row["expires"] > time.time() and all((
                    row["client_id"] == params["client_id"], row["resource"] == params["resource"],
                    row["redirect_uri"] == params.get("redirect_uri"),
                    hmac.compare_digest(row["challenge"], challenge)))
                if not valid:
                    raise AuthError("Expired or invalid authorization code", "invalid_grant")
                db.execute("DELETE FROM codes WHERE digest=?", (row["digest"],))
                return self._issue_tokens(db, row["username"], row["client_id"], row["resource"], row["scope"])
            if grant_type == "refresh_token":
                row = db.execute("SELECT * FROM tokens WHERE digest=? AND kind='refresh'",
                                 (digest(params.get("refresh_token", "")),)).fetchone()
                if not row or row["used"] or row["expires"] < time.time():
                    raise AuthError("Expired or consumed refresh token", "invalid_grant")
                if row["client_id"] != params["client_id"] or row["resource"] != params["resource"]:
                    raise AuthError("Refresh token audience mismatch", "invalid_grant")
                db.execute("UPDATE tokens SET used=1 WHERE digest=?", (row["digest"],))
                return self._issue_tokens(db, row["username"], row["client_id"], row["resource"],
                                          row["scope"], row["grant_id"])
            raise AuthError("Unsupported grant type", "unsupported_grant_type")

    def verify(self, token, required_scopes=()):
        with self.connection() as db:
            row = db.execute("SELECT tokens.*, users.tenant_id FROM tokens JOIN users USING(username) "
                             "WHERE tokens.digest=? AND kind='access'", (digest(token),)).fetchone()
        if not row or row["used"] or row["expires"] < time.time() or row["resource"] != self.resource:
            raise AuthError("Authorization required or token expired", "invalid_token", 401)
        scopes = frozenset(row["scope"].split())
        if not set(required_scopes) <= scopes:
            raise AuthError("Insufficient permission", "insufficient_scope", 403)
        return Principal(row["username"], row["tenant_id"], scopes)

    def revoke(self, token):
        with self.connection() as db:
            row = db.execute("SELECT grant_id FROM tokens WHERE digest=?", (digest(token),)).fetchone()
            if row:
                db.execute("DELETE FROM tokens WHERE grant_id=?", (row["grant_id"],))

    def metadata(self):
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token", "revocation_endpoint": self.issuer + "/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": sorted(SCOPES), "authorization_response_iss_parameter_supported": True}

    def resource_metadata(self):
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": sorted(SCOPES), "bearer_methods_supported": ["header"]}
