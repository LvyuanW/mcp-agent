"""Run from server/ with python3 app.py --config config.toml."""

import argparse
import base64
import collections
import getpass
import html
import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.http import Handler, cookie_header, https_origin, load_config, serve_https, sign_value, verify_value
from server.auth import AuthError, OAuthService
from server.workspace import WorkspaceService


VERSION = "2026-07-28"
META = "io.modelcontextprotocol/"
DEFAULTS = {"host": "127.0.0.1", "port": 9443, "public_url": "https://localhost:9443",
            "data_dir": "../.local/server", "cert_file": "../.local/tls/localhost.pem",
            "key_file": "../.local/tls/localhost-key.pem",
            "redirect_uris": ["https://localhost:8443/auth/callback"], "allowed_origins": [],
            "token_ttl": 28800}


class RPCError(Exception):
    def __init__(self, code, message, status=400, data=None):
        super().__init__(message)
        self.code, self.status, self.data = code, status, data


def header_value(value):
    if value is None:
        return None
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise RPCError(-32020, "Malformed encoded header") from error
    return value


def mirror_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_mirrors(headers, message, tool_spec=None):
    params = message.get("params", {})
    meta = params.get("_meta", {})
    expected = {"MCP-Protocol-Version": meta.get(META + "protocolVersion"), "Mcp-Method": message["method"]}
    if message["method"] in {"tools/call", "prompts/get", "resources/read"}:
        expected["Mcp-Name"] = params.get("uri") if message["method"] == "resources/read" else params.get("name")
    for key, value in expected.items():
        if value is None or header_value(headers.get(key)) != value:
            raise RPCError(-32020, f"Missing or mismatched {key}")
    if tool_spec:
        def inspect(schema, values):
            for name, field in schema.get("properties", {}).items():
                value = values.get(name) if isinstance(values, dict) else None
                suffix = field.get("x-mcp-header")
                if suffix and value is not None:
                    actual = header_value(headers.get("Mcp-Param-" + suffix))
                    if actual != mirror_value(value):
                        raise RPCError(-32020, "Missing or mismatched parameter header")
                if isinstance(value, dict):
                    inspect(field, value)
        inspect(tool_spec["inputSchema"], params.get("arguments", {}))


def form_values(body):
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=30)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("Duplicate form parameters")
    return {key: values[0] for key, values in parsed.items()}


class Application:
    def __init__(self, config):
        self.config = config
        https_origin(config["public_url"])
        self.workspace = WorkspaceService(Path(config["data_dir"]).resolve())
        self.auth = OAuthService(Path(config["data_dir"]).resolve(), config["public_url"],
                                 config["redirect_uris"], token_ttl=config["token_ttl"])
        key_path = Path(config["data_dir"]) / "authorization-signing.key"
        try:
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(48))
        self.signing_key = key_path.read_text().strip()
        self.origins = {https_origin(config["public_url"]), *config.get("allowed_origins", [])}
        self.attempts = collections.defaultdict(collections.deque)
        self.attempt_lock = threading.Lock()

    def login_attempt(self, address):
        with self.attempt_lock:
            attempts = self.attempts[address]
            now = time.monotonic()
            while attempts and attempts[0] < now - 300:
                attempts.popleft()
            if len(attempts) >= 20:
                raise AuthError("Too many login attempts; retry in five minutes", "slow_down", 429)
            attempts.append(now)

    def rpc(self, principal, headers, message):
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            raise RPCError(-32600, "Invalid JSON-RPC request")
        request_id = message.get("id")
        if type(request_id) not in (str, int):
            raise RPCError(-32600, "Request id must be a string or integer")
        params = message.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("_meta"), dict):
            raise RPCError(-32602, "Request metadata is required")
        meta = params["_meta"]
        validate_mirrors(headers, message)
        if meta.get(META + "protocolVersion") != VERSION:
            raise RPCError(-32022, "Unsupported protocol version", data={"supported": [VERSION],
                           "requested": meta.get(META + "protocolVersion")})
        if not isinstance(meta.get(META + "clientCapabilities"), dict):
            raise RPCError(-32602, "Client capabilities are required")
        method = message["method"]
        if method == "server/discover":
            result = {"supportedVersions": [VERSION], "capabilities": {"tools": {}},
                      "instructions": "Use the remote workspace's shell and skills. Session tools are runtime controls.",
                      "ttlMs": 0, "cacheScope": "private"}
        elif method == "tools/list":
            if params.get("cursor"):
                raise RPCError(-32602, "Invalid cursor")
            result = {"tools": self.workspace.tool_specs(), "ttlMs": 0, "cacheScope": "private"}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            spec = next((tool for tool in self.workspace.tool_specs() if tool["name"] == name), None)
            if spec is None:
                raise RPCError(-32602, "Unknown tool")
            if not isinstance(arguments, dict):
                raise RPCError(-32602, "Tool arguments must be an object")
            validate_mirrors(headers, message, spec)
            required = {"workspace:read"}
            if name.startswith("session_") and name not in {"session_get"}:
                required.add("agent:run")
            if name in {"shell", "job_cancel"}:
                required.update({"workspace:write", "agent:run"})
            if not required <= principal.scopes:
                raise AuthError("Insufficient permission", "insufficient_scope", 403)
            context = {key: meta.get("io.mcp-agent/" + key) for key in ("session_id", "operation_id", "lease_token")}
            try:
                value = self.workspace.call(principal.tenant_id, name, arguments, context=context)
                if name == "workspace_info":
                    value["username"] = principal.username
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                          "structuredContent": value, "isError": False}
            except (ValueError, LookupError, RuntimeError) as error:
                detail = {"error": {"code": getattr(error, "code", "tool_error"), "message": str(error)}}
                result = {"content": [{"type": "text", "text": str(error)}], "structuredContent": detail, "isError": True}
        else:
            raise RPCError(-32601, "Method not found", 404)
        result.update(resultType="complete", _meta={META + "serverInfo": {"name": "mcp-agent-workspace", "version": "0.1.0"}})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_handler(app):
    class MCPHandler(Handler):
        def check_origin(self):
            origin = self.headers.get("Origin")
            if origin and origin not in app.origins:
                raise AuthError("Origin is not allowed", "access_denied", 403)

        def principal(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise AuthError("Sign in to access your workspace", "invalid_token", 401)
            return app.auth.verify(header[7:])

        def auth_error(self, error):
            self.respond(error.status, {"error": error.code, "error_description": str(error)}, headers=[
                ("WWW-Authenticate", f'Bearer resource_metadata="{app.auth.issuer}/.well-known/oauth-protected-resource", '
                 'scope="workspace:read workspace:write agent:run"')])

        def login_form(self, params, message=""):
            params = app.auth.validate_authorization(params)
            nonce = secrets.token_urlsafe(24)
            flow = sign_value({"params": params, "nonce": nonce}, app.signing_key)
            markup = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录工作区 · MCP Agent</title><style>body{{margin:0;background:#f5f5f1;color:#202923;font:16px system-ui;display:grid;min-height:100vh;place-items:center}}main{{width:min(360px,82vw);padding:36px;background:white;border:1px solid #dde2dc;border-radius:20px}}h1{{font-size:26px}}p{{line-height:1.7;color:#667068}}label{{display:block;margin:20px 0 8px}}input,button{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccd4cc;border-radius:8px;font:inherit}}button{{margin-top:26px;background:#245a43;color:white;border:0;cursor:pointer}}.error{{color:#a33737}}small{{overflow-wrap:anywhere}}</style>
<main><small>MCP AGENT / WORKSPACE</small><h1>登录远程工作区</h1><p>授权网页 Agent 访问你的会话、技能和工作区，并执行任务。</p><p class="error">{html.escape(message)}</p>
<form method="post" action="/authorize"><input type="hidden" name="flow" value="{html.escape(flow)}">
<label for="username">开发者账号</label><input id="username" name="username" autocomplete="username" required autofocus>
<label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">登录并授权</button></form><p><small>客户端 {html.escape(params['client_id'])}<br>工作区服务 {html.escape(app.auth.issuer)}</small></p></main></html>'''
            self.respond(200, markup, "text/html; charset=utf-8", headers=[cookie_header("mcp_authorize", nonce, max_age=600),
                ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")])

        def do_GET(self):
            try:
                self.check_origin()
                path = urlparse(self.path).path
                if path == "/health":
                    self.respond(200, {"status": "ok", "service": "mcp-agent-server", "protocol": VERSION})
                elif path in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}:
                    self.respond(200, app.auth.resource_metadata())
                elif path == "/.well-known/oauth-authorization-server":
                    self.respond(200, app.auth.metadata())
                elif path == "/authorize":
                    self.login_form(form_values(urlparse(self.path).query.encode()))
                elif path == "/mcp":
                    self.respond(405, {"error": "Use POST for MCP requests"}, headers=[("Allow", "POST")])
                else:
                    self.fail(404, "Not found")
            except AuthError as error:
                self.auth_error(error)
            except ValueError as error:
                self.fail(400, error)

        def do_POST(self):
            message = None
            try:
                self.check_origin()
                path = urlparse(self.path).path
                if path in {"/token", "/revoke", "/authorize"}:
                    if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                        raise ValueError("Expected a form-encoded request")
                    params = form_values(self.body())
                    if path == "/token":
                        self.respond(200, app.auth.token(params))
                    elif path == "/revoke":
                        app.auth.revoke(params.get("token", ""))
                        self.respond(200, {})
                    else:
                        app.login_attempt(self.client_address[0])
                        flow = verify_value(params.get("flow", ""), app.signing_key)
                        cookies = SimpleCookie(self.headers.get("Cookie", ""))
                        nonce = cookies.get("mcp_authorize")
                        if not nonce or not secrets.compare_digest(nonce.value, flow["nonce"]):
                            raise AuthError("Authorization form expired; start sign-in again", "access_denied", 403)
                        try:
                            code = app.auth.authorize(flow["params"], params.get("username", ""), params.get("password", ""))
                        except AuthError as error:
                            if error.status == 401:
                                self.login_form(flow["params"], str(error))
                                return
                            raise
                        query = urlencode({"code": code, "state": flow["params"]["state"], "iss": app.auth.issuer})
                        separator = "&" if "?" in flow["params"]["redirect_uri"] else "?"
                        self.redirect(flow["params"]["redirect_uri"] + separator + query,
                                      [cookie_header("mcp_authorize", "", max_age=0)])
                elif path == "/mcp":
                    principal = self.principal()
                    accepts = self.headers.get("Accept", "")
                    if "application/json" not in accepts or "text/event-stream" not in accepts:
                        raise RPCError(-32600, "Accept must include application/json and text/event-stream", 406)
                    message = self.json_body()
                    self.respond(200, app.rpc(principal, self.headers, message))
                else:
                    self.fail(404, "Not found")
            except RPCError as error:
                result = {"jsonrpc": "2.0", "error": {"code": error.code, "message": str(error)}}
                if isinstance(message, dict) and type(message.get("id")) in (str, int):
                    result["id"] = message["id"]
                if error.data is not None:
                    result["error"]["data"] = error.data
                self.respond(error.status, result)
            except AuthError as error:
                self.auth_error(error)
            except (ValueError, KeyError) as error:
                if urlparse(self.path).path == "/mcp":
                    code = -32700 if isinstance(error, json.JSONDecodeError) else -32600
                    failure = {"jsonrpc": "2.0", "error": {"code": code, "message": str(error)}}
                    if isinstance(message, dict) and type(message.get("id")) in (str, int):
                        failure["id"] = message["id"]
                    self.respond(400, failure)
                else:
                    self.fail(400, error)
            except Exception:
                import traceback
                traceback.print_exc()
                self.fail(500, "Server error; inspect service logs")

    return MCPHandler


def main():
    parser = argparse.ArgumentParser(description="MCP workspace HTTPS service")
    parser.add_argument("--config")
    parser.add_argument("--init", action="store_true", help="Register a developer and existing workspace")
    parser.add_argument("--tenant", default="personal")
    parser.add_argument("--username", default="developer")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--execution-user", help="OS account for shell commands; requires a privileged supervisor")
    args = parser.parse_args()
    config_path = args.config or ("config.local.toml" if Path("config.local.toml").exists() else None)
    config = load_config(config_path, DEFAULTS)
    app = Application(config)
    if args.init:
        if args.workspace is None:
            parser.error("--init requires --workspace pointing to an existing directory")
        password = os.environ.get("MCP_AGENT_INIT_PASSWORD") or getpass.getpass("Developer password (12+ characters): ")
        app.workspace.ensure_tenant(args.tenant, args.workspace.resolve(), execution_user=args.execution_user)
        app.auth.put_user(args.username, args.tenant, password)
        print(f"Registered {args.username} in tenant {args.tenant}")
        return
    server = serve_https(make_handler(app), config["host"], config["port"], config["cert_file"], config["key_file"])
    print(f"MCP service listening at {config['public_url']}/mcp", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
