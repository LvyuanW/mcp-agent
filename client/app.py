"""HTTPS web UI and replaceable harness. All user data lives behind MCP."""

import argparse
import base64
from copy import deepcopy
import hashlib
from http.cookies import SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib import error, request
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.http import Handler, cookie_header, https_origin, load_config, serve_https, sign_value, ssl_client_context, verify_value
from client.mcp import MCPClient, MCPError, MCPToolError
from client.runtime import AgentRunner


STATIC = Path(__file__).resolve().parent / "static"
DEFAULTS = {"host": "127.0.0.1", "port": 8443, "public_url": "https://localhost:8443",
            "mcp_url": "https://localhost:9443/mcp", "ca_file": "../.local/tls/ca.pem",
            "cert_file": "../.local/tls/localhost.pem", "key_file": "../.local/tls/localhost-key.pem",
            "cookie_secret": "", "model_defaults": {"provider": "openai", "model": "", "base_url": "",
                "api_format": "auto", "max_output_tokens": 8192, "context_window": 128000}}
CONFIG_FIELDS = {"provider", "model", "api_key", "base_url", "api_format", "max_output_tokens", "context_window",
                 "deepseek_effort", "system_prompt"}


def public_session(session):
    session = deepcopy(session)
    config = session.setdefault("config", {})
    config["api_key_set"] = bool(config.pop("api_key", ""))
    session.pop("lease_token", None)
    return session


def validate_config(value):
    if not isinstance(value, dict) or set(value) - CONFIG_FIELDS:
        raise ValueError("Unsupported model configuration fields")
    if value.get("provider", "openai") not in {"openai", "anthropic"}:
        raise ValueError("Provider must be openai or anthropic")
    if value.get("api_format", "auto") not in {"auto", "chat", "responses"}:
        raise ValueError("Invalid model API format")
    for key in ("max_output_tokens", "context_window"):
        if key in value and (type(value[key]) is not int or not 1 <= value[key] <= 10_000_000):
            raise ValueError(f"{key} must be a positive integer")
    if value.get("base_url"):
        https_origin(value["base_url"])
    for key, field in value.items():
        if key not in {"max_output_tokens", "context_window"} and not isinstance(field, str):
            raise ValueError(f"{key} must be a string")
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Authentication endpoints must not redirect token requests")


class Application:
    def __init__(self, config, model_factory=None):
        self.config = config
        self.origin = https_origin(config["public_url"])
        self.mcp_origin = https_origin(config["mcp_url"])
        self.secret = os.environ.get("MCP_AGENT_COOKIE_SECRET") or config.get("cookie_secret", "")
        if len(self.secret) < 32:
            raise ValueError("Set a shared MCP_AGENT_COOKIE_SECRET (32+ characters), or use scripts/dev_setup.py")
        self.opener = request.build_opener(request.HTTPSHandler(context=ssl_client_context(config.get("ca_file"))), NoRedirect())
        self.model_factory = model_factory
        # This map only suppresses duplicate work in this process. The server lease is authoritative.
        self.workers = {}
        self.worker_lock = threading.Lock()

    def fetch_json(self, url, data=None):
        https_origin(url)
        headers = {"Accept": "application/json"}
        if data is not None:
            data = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = request.Request(url, data, headers)
        try:
            with self.opener.open(req, timeout=30) as response:
                return json.loads(response.read(1024 * 1024))
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(4096))
                message = payload.get("error_description") or payload.get("error") or "Authorization failed"
            except ValueError:
                message = "Authorization failed"
            raise MCPError(str(message), status=exc.code) from None

    def discover_auth(self):
        # The configured MCP origin is the trust root for protected-resource discovery.
        path = urlparse(self.config["mcp_url"]).path.rstrip("/")
        metadata = self.fetch_json(self.mcp_origin + "/.well-known/oauth-protected-resource" + path)
        if metadata.get("resource") != self.config["mcp_url"]:
            raise ValueError("Protected resource metadata does not match the configured MCP endpoint")
        issuers = metadata.get("authorization_servers", [])
        if not issuers:
            raise ValueError("MCP service does not advertise an authorization server")
        issuer = issuers[0].rstrip("/")
        origin = https_origin(issuer)
        issuer_path = urlparse(issuer).path.rstrip("/")
        try:
            auth = self.fetch_json(origin + "/.well-known/oauth-authorization-server" + issuer_path)
        except MCPError as exc:
            if exc.status != 404:
                raise
            auth = self.fetch_json(issuer + "/.well-known/openid-configuration")
        if auth.get("issuer") != issuer or "S256" not in auth.get("code_challenge_methods_supported", []):
            raise ValueError("Invalid authorization-server metadata or unsupported PKCE")
        for key in ("authorization_endpoint", "token_endpoint"):
            https_origin(auth[key])
        return auth

    def mcp(self, access_token):
        return MCPClient(self.config["mcp_url"], access_token, self.config.get("ca_file"))

    def start_runner(self, access_token, tenant_id, session_id):
        key = (tenant_id, session_id)
        with self.worker_lock:
            current = self.workers.get(key)
            if current and current.is_alive():
                return

            def run():
                try:
                    AgentRunner(self.mcp(access_token), model_factory=self.model_factory).run(session_id)
                except Exception as exc:
                    # Runner saves recoverable errors remotely. Lease conflicts belong to another worker.
                    print(f"Agent runner stopped ({type(exc).__name__})", file=sys.stderr)
                finally:
                    with self.worker_lock:
                        self.workers.pop(key, None)

            thread = threading.Thread(target=run, name="agent-" + session_id[:8], daemon=True)
            self.workers[key] = thread
            thread.start()


def make_handler(app):
    class WebHandler(Handler):
        def cookies(self):
            return SimpleCookie(self.headers.get("Cookie", ""))

        def token(self):
            token = self.cookies().get("mcp_access")
            if not token or not token.value:
                raise MCPError("请先登录远程工作区。", status=401)
            return token.value

        def check_mutation(self):
            if self.headers.get("X-Requested-With") != "mcp-agent":
                raise MCPError("Missing request origin marker", status=403)
            if self.headers.get("Origin") != app.origin:
                raise MCPError("Cross-origin mutation is forbidden", status=403)
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                raise MCPError("Cross-site request is forbidden", status=403)

        def remote(self):
            token = self.token()
            return app.mcp(token), token

        def handle_error(self, exc):
            if isinstance(exc, MCPToolError):
                detail = exc.data.get("error", {}) if isinstance(exc.data, dict) else {}
                code = detail.get("code", "tool_error") if isinstance(detail, dict) else "tool_error"
                status = 409 if "conflict" in code else 404 if code == "not_found" else 400
                self.fail(status, detail.get("message", str(exc)) if isinstance(detail, dict) else str(exc), code)
            elif isinstance(exc, MCPError):
                self.fail(exc.status or 502, str(exc), str(exc.code or "remote_error"))
            elif isinstance(exc, (ValueError, KeyError)):
                self.fail(400, str(exc))
            else:
                import traceback
                traceback.print_exc()
                self.fail(500, "请求失败，请检查 Web 服务日志。")

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                if path == "/health":
                    self.respond(200, {"status": "ok", "service": "mcp-agent-client"})
                elif path == "/auth/login":
                    auth = app.discover_auth()
                    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                    flow = sign_value({"state": state, "verifier": verifier, "issuer": auth["issuer"],
                                       "token_endpoint": auth["token_endpoint"],
                                       "check_issuer": auth.get("authorization_response_iss_parameter_supported", False)}, app.secret)
                    params = {"client_id": "mcp-agent-web", "redirect_uri": app.config["public_url"].rstrip("/") + "/auth/callback",
                              "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
                              "scope": "workspace:read workspace:write agent:run", "state": state, "resource": app.config["mcp_url"]}
                    self.redirect(auth["authorization_endpoint"] + "?" + urlencode(params),
                                  [cookie_header("mcp_flow", flow, max_age=600)])
                elif path == "/auth/callback":
                    flow_cookie = self.cookies().get("mcp_flow")
                    if not flow_cookie:
                        raise ValueError("登录状态已过期，请重新登录。")
                    flow = verify_value(flow_cookie.value, app.secret)
                    if not secrets.compare_digest(query.get("state", [""])[0], flow["state"]):
                        raise ValueError("OAuth state mismatch")
                    issuer = query.get("iss", [None])[0]
                    if (flow["check_issuer"] or issuer is not None) and issuer != flow["issuer"]:
                        raise ValueError("OAuth issuer mismatch")
                    if "error" in query:
                        raise ValueError("工作区授权未完成，请重新登录。")
                    tokens = app.fetch_json(flow["token_endpoint"], {
                        "grant_type": "authorization_code", "client_id": "mcp-agent-web",
                        "code": query.get("code", [""])[0], "code_verifier": flow["verifier"],
                        "redirect_uri": app.config["public_url"].rstrip("/") + "/auth/callback", "resource": app.config["mcp_url"]})
                    self.redirect("/", [cookie_header("mcp_access", tokens["access_token"], max_age=tokens["expires_in"]),
                        cookie_header("mcp_refresh", tokens["refresh_token"], max_age=30 * 86400),
                        cookie_header("mcp_flow", "", max_age=0)])
                elif path == "/api/bootstrap":
                    try:
                        mcp, _ = self.remote()
                        workspace = mcp.call_tool("workspace_info", {})
                    except MCPError as exc:
                        if exc.status == 401:
                            self.respond(200, {"authenticated": False, "mcp_url": app.config["mcp_url"]})
                            return
                        raise
                    tools = [tool for tool in mcp.list_tools() if tool.get("_meta", {}).get("io.mcp-agent/visibility") != "runtime"]
                    self.respond(200, {"authenticated": True, "mcp_url": app.config["mcp_url"], "workspace": workspace,
                        "user": {"username": workspace.get("username", workspace["tenant_id"]), "tenant_id": workspace["tenant_id"]},
                        "skills": workspace.get("skills", []), "tools": tools, "model_defaults": app.config["model_defaults"]})
                elif path.startswith("/api/"):
                    mcp, _ = self.remote()
                    if path == "/api/sessions":
                        self.respond(200, mcp.call_tool("sessions_list", {}))
                    elif path.startswith("/api/sessions/"):
                        session_id = path[len("/api/sessions/"):]
                        if "/" in session_id:
                            raise ValueError("Invalid session path")
                        self.respond(200, public_session(mcp.call_tool("session_get", {"session_id": session_id})))
                    elif path in {"/api/files", "/api/file"}:
                        arguments = {"path": query.get("path", [""])[0]}
                        self.respond(200, mcp.call_tool("files_list" if path == "/api/files" else "file_read", arguments))
                    else:
                        self.fail(404, "API not found")
                else:
                    filename = "index.html" if path == "/" else path.lstrip("/")
                    target = (STATIC / filename).resolve()
                    if not target.is_relative_to(STATIC) or not target.is_file():
                        self.fail(404, "Not found")
                        return
                    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    self.respond(200, target.read_bytes(), content_type + "; charset=utf-8", headers=[
                        ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                         "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'self'")])
            except Exception as exc:
                self.handle_error(exc)

        def do_POST(self):
            try:
                self.check_mutation()
                path = urlparse(self.path).path
                if path == "/auth/logout":
                    token = self.cookies().get("mcp_refresh") or self.cookies().get("mcp_access")
                    if token:
                        auth = app.discover_auth()
                        if auth.get("revocation_endpoint"):
                            app.fetch_json(auth["revocation_endpoint"], {"token": token.value, "client_id": "mcp-agent-web"})
                    self.respond(200, {"ok": True}, headers=[cookie_header("mcp_access", "", max_age=0),
                                                             cookie_header("mcp_refresh", "", max_age=0)])
                    return
                if path == "/auth/refresh":
                    refresh = self.cookies().get("mcp_refresh")
                    if not refresh:
                        raise MCPError("请重新登录。", status=401)
                    auth = app.discover_auth()
                    tokens = app.fetch_json(auth["token_endpoint"], {"grant_type": "refresh_token", "client_id": "mcp-agent-web",
                        "resource": app.config["mcp_url"], "refresh_token": refresh.value})
                    self.respond(200, {"ok": True}, headers=[cookie_header("mcp_access", tokens["access_token"], max_age=tokens["expires_in"]),
                        cookie_header("mcp_refresh", tokens["refresh_token"], max_age=30 * 86400)])
                    return
                mcp, token = self.remote()
                body = self.json_body()
                if path == "/api/sessions":
                    config = {**app.config["model_defaults"], **validate_config(body.get("config", {}))}
                    session = mcp.call_tool("session_create", {"title": body.get("title") or "新会话", "config": config})
                    self.respond(201, public_session(session))
                    return
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "sessions"]:
                    self.fail(404, "API not found")
                    return
                session_id, action = parts[2:]
                if action == "cancel":
                    AgentRunner(mcp).cancel(session_id)
                    self.respond(202, {"accepted": True})
                    return
                session = mcp.call_tool("session_get", {"session_id": session_id})
                if action == "messages":
                    content = body.get("content", "")
                    operation = body.get("request_id")
                    if not isinstance(content, str) or not content.strip() or len(content) > 200_000:
                        raise ValueError("消息内容须为 1–200000 个字符。")
                    if not isinstance(operation, str) or not 1 <= len(operation) <= 128:
                        raise ValueError("A stable request_id is required")
                    messages = session.get("messages", [])
                    existing = next((message for message in messages if message.get("request_id") == operation), None)
                    if existing:
                        expected_content = [{"type": "text", "text": content.strip()}]
                        if existing.get("content") != expected_content:
                            raise MCPError("request_id 已用于另一条消息。", status=409)
                        if session.get("status") != "running":
                            self.respond(200, public_session(session))
                            return
                    else:
                        if session.get("status") == "running" or session.get("pending"):
                            raise MCPError("请先完成或取消当前任务。", status=409)
                        messages.append({"role": "user", "content": [{"type": "text", "text": content.strip()}], "request_id": operation})
                        title = content.strip().replace("\n", " ")[:40] if len(messages) == 1 else session["title"]
                        session = mcp.call_tool("session_update", {"session_id": session_id, "expected_revision": session["revision"],
                            "patch": {"messages": messages, "status": "running", "error": None, "title": title,
                                      "cancel_requested": False}})
                elif action != "resume":
                    self.fail(404, "Unknown session action")
                    return
                elif session.get("cancel_requested") and session.get("status") != "running":
                    session = mcp.call_tool("session_update", {"session_id": session_id,
                        "expected_revision": session["revision"], "patch": {"cancel_requested": False}})
                app.start_runner(token, session.get("tenant_id", ""), session_id)
                self.respond(202, public_session(session) if action == "messages" else {"accepted": True})
            except Exception as exc:
                self.handle_error(exc)

        def do_PATCH(self):
            try:
                self.check_mutation()
                parts = urlparse(self.path).path.strip("/").split("/")
                if len(parts) != 3 or parts[:2] != ["api", "sessions"]:
                    self.fail(404, "Not found")
                    return
                body = self.json_body()
                patch = body.get("patch")
                if not isinstance(patch, dict) or set(patch) - {"config", "title"}:
                    raise ValueError("Only title and model settings can be edited here")
                mcp, _ = self.remote()
                current = mcp.call_tool("session_get", {"session_id": parts[2]})
                if "config" in patch:
                    patch["config"] = {**current.get("config", {}), **validate_config(patch["config"])}
                result = mcp.call_tool("session_update", {"session_id": parts[2],
                    "expected_revision": body.get("expected_revision"), "patch": patch})
                self.respond(200, public_session(result))
            except Exception as exc:
                self.handle_error(exc)

    return WebHandler


def main():
    parser = argparse.ArgumentParser(description="MCP Agent HTTPS web harness")
    parser.add_argument("--config")
    args = parser.parse_args()
    config_path = args.config or ("config.local.toml" if Path("config.local.toml").exists() else None)
    config = load_config(config_path, DEFAULTS)
    app = Application(config)
    server = serve_https(make_handler(app), config["host"], config["port"], config["cert_file"], config["key_file"])
    print(f"Web Agent listening at {config['public_url']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
