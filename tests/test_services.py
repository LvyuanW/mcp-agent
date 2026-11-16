import base64
import hashlib
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading
import time
import unittest
from urllib import error, request
from urllib.parse import urlencode

from client.app import Application as WebApplication, make_handler as web_handler
from client.mcp import MCPClient, MCPError
from common.http import serve_https
from scripts.dev_certs import generate
from server.app import Application as ServerApplication, VERSION, make_handler as server_handler
from server.auth import AuthError, OAuthService


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FixtureModel:
    """Deterministic model peer used only to verify real HTTPS and shell orchestration."""
    def complete(self, system, messages, tools, **kwargs):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": [{"type": "text", "text": "已创建产物并验证输出。"}]}
        return {"role": "assistant", "content": [{"type": "text", "text": "正在远程工作区运行命令。"}],
                "tool_calls": [{"id": "fixture-shell", "name": "shell", "arguments": json.dumps({
                    "command": "printf 'verified over HTTPS\\n' > result.txt; cat result.txt"})}]}

    def count_tokens(self, *args, **kwargs):
        return 100


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth = OAuthService(self.temp.name, "https://workspace.test", ["https://client.test/auth/callback"])
        self.auth.put_user("alice", "tenant-a", "a-secure-test-password")
        self.verifier = "v" * 64
        self.params = {"client_id": "mcp-agent-web", "redirect_uri": "https://client.test/auth/callback",
            "resource": "https://workspace.test/mcp", "state": "random-state", "response_type": "code",
            "code_challenge_method": "S256", "scope": "workspace:read workspace:write agent:run",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")}

    def exchange(self, code, verifier=None):
        return self.auth.token({"grant_type": "authorization_code", "code": code,
            "client_id": "mcp-agent-web", "redirect_uri": self.params["redirect_uri"],
            "resource": self.params["resource"], "code_verifier": verifier or self.verifier})

    def test_pkce_code_single_use_audience_and_rotation(self):
        code = self.auth.authorize(self.params, "alice", "a-secure-test-password")
        with self.assertRaises(AuthError):
            self.exchange(code, "x" * 64)
        tokens = self.exchange(code)
        with self.assertRaises(AuthError):
            self.exchange(code)
        principal = self.auth.verify(tokens["access_token"])
        self.assertEqual(principal.tenant_id, "tenant-a")
        rotated = self.auth.token({"grant_type": "refresh_token", "client_id": "mcp-agent-web",
            "resource": self.params["resource"], "refresh_token": tokens["refresh_token"]})
        with self.assertRaises(AuthError):
            self.auth.token({"grant_type": "refresh_token", "client_id": "mcp-agent-web",
                "resource": self.params["resource"], "refresh_token": tokens["refresh_token"]})
        self.auth.revoke(rotated["refresh_token"])
        with self.assertRaises(AuthError):
            self.auth.verify(tokens["access_token"])
        with self.assertRaises(AuthError):
            self.auth.verify(rotated["access_token"])

    def test_redirect_resource_and_scope_are_validated(self):
        for changes in ({"redirect_uri": "https://attacker.test/callback"},
                        {"resource": "https://another.test/mcp"}, {"scope": "root"},
                        {"code_challenge_method": "plain"}):
            with self.subTest(changes=changes), self.assertRaises(AuthError):
                self.auth.validate_authorization({**self.params, **changes})


class HTTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.certs = generate(cls.root / "tls")
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()
        server_port, client_port = unused_port(), unused_port()
        cls.server_origin, cls.client_origin = f"https://localhost:{server_port}", f"https://localhost:{client_port}"
        tls = {"cert_file": str(cls.certs / "localhost.pem"), "key_file": str(cls.certs / "localhost-key.pem")}
        cls.server_app = ServerApplication({"public_url": cls.server_origin, "data_dir": str(cls.root / "data"),
            "redirect_uris": [cls.client_origin + "/auth/callback"], "token_ttl": 3600, **tls})
        cls.server_app.workspace.ensure_tenant("alpha", cls.workspace)
        cls.server_app.auth.put_user("alice", "alpha", "a-secure-test-password")
        cls.web_config = {"public_url": cls.client_origin, "mcp_url": cls.server_origin + "/mcp",
            "ca_file": str(cls.certs / "ca.pem"), "cookie_secret": "shared-cookie-secret-" * 4,
            "model_defaults": {"provider": "openai", "model": "fixture", "api_format": "chat",
                               "context_window": 128000, "max_output_tokens": 8192}, **tls}
        cls.web_app = WebApplication(cls.web_config, model_factory=lambda config: FixtureModel())
        cls.server = serve_https(server_handler(cls.server_app), "127.0.0.1", server_port, tls["cert_file"], tls["key_file"])
        cls.client = serve_https(web_handler(cls.web_app), "127.0.0.1", client_port, tls["cert_file"], tls["key_file"])
        cls.threads = []
        for service in (cls.server, cls.client):
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for service in (cls.client, cls.server):
            service.shutdown()
            service.server_close()
        for thread in cls.threads:
            thread.join(timeout=5)
        cls.temp.cleanup()

    def setUp(self):
        self.cookies = CookieJar()
        context = ssl.create_default_context(cafile=str(self.certs / "ca.pem"))
        self.browser = request.build_opener(request.HTTPSHandler(context=context), request.HTTPCookieProcessor(self.cookies))

    def login(self):
        with self.browser.open(self.client_origin + "/auth/login") as response:
            form = response.read().decode()
        flow = re.search(r'name="flow" value="([^"]+)"', form).group(1)
        body = urlencode({"flow": flow, "username": "alice", "password": "a-secure-test-password"}).encode()
        req = request.Request(self.server_origin + "/authorize", body,
                              {"Content-Type": "application/x-www-form-urlencoded", "Origin": self.server_origin})
        with self.browser.open(req) as response:
            self.assertEqual(response.status, 200)
        return next(cookie.value for cookie in self.cookies if cookie.name == "mcp_access")

    def api(self, path, data=None, method=None, origin=None):
        headers = {"Accept": "application/json"}
        if data is not None:
            headers.update({"Content-Type": "application/json", "X-Requested-With": "mcp-agent",
                            "Origin": origin or self.client_origin})
            data = json.dumps(data).encode()
        with self.browser.open(request.Request(self.client_origin + path, data, headers, method=method), timeout=20) as response:
            return json.load(response)

    def test_real_oauth_https_tools_loop_and_browser_replay(self):
        self.assertFalse(self.api("/api/bootstrap")["authenticated"])
        token = self.login()
        bootstrap = self.api("/api/bootstrap")
        self.assertTrue(bootstrap["authenticated"])
        self.assertEqual(bootstrap["user"]["username"], "alice")
        self.assertIn("shell", [tool["name"] for tool in bootstrap["tools"]])
        self.assertNotIn("session_update", [tool["name"] for tool in bootstrap["tools"]])
        created = self.api("/api/sessions", {"config": {"api_key": "test-secret-key"}})
        self.assertNotIn("api_key", created["config"])
        self.assertTrue(created["config"]["api_key_set"])
        path = "/api/sessions/" + created["id"]
        self.api(path + "/messages", {"content": "创建一个结果文件并检查", "request_id": "user-action-1"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            session = self.api(path)
            if session["status"] in {"completed", "error"}:
                break
            time.sleep(0.1)
        self.assertEqual(session["status"], "completed", session.get("error"))
        self.assertEqual(self.workspace.joinpath("result.txt").read_text(), "verified over HTTPS\n")
        self.assertEqual([message["role"] for message in session["messages"]], ["user", "assistant", "tool", "assistant"])
        original_messages = session["messages"]
        replay = self.api(path + "/messages", {"content": "创建一个结果文件并检查", "request_id": "user-action-1"})
        time.sleep(0.3)
        self.assertEqual(self.api(path)["messages"], original_messages)
        # A second web harness has no user-state files and sees the same remote session.
        replacement = WebApplication(self.web_config, model_factory=lambda config: FixtureModel())
        restored = replacement.mcp(token).call_tool("session_get", {"session_id": created["id"]})
        self.assertEqual(restored["messages"], original_messages)
        self.assertEqual(self.api("/api/file?path=result.txt")["content"], "verified over HTTPS\n")
        with self.assertRaises(error.HTTPError) as caught:
            self.api("/api/sessions", {}, origin="https://attacker.test")
        self.assertEqual(caught.exception.code, 403)

    def test_mcp_headers_origin_and_tenant_authorization(self):
        token = self.login()
        client = MCPClient(self.server_origin + "/mcp", token, str(self.certs / "ca.pem"))
        self.assertIn(VERSION, client.call("server/discover")["supportedVersions"])
        document = {"jsonrpc": "2.0", "id": 77, "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": VERSION, "io.modelcontextprotocol/clientCapabilities": {}}}}
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": VERSION,
                   "Mcp-Method": "tools/call"}
        req = request.Request(self.server_origin + "/mcp", json.dumps(document).encode(), headers)
        with self.assertRaises(error.HTTPError) as caught:
            self.browser.open(req)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.load(caught.exception)["error"]["code"], -32020)
        self.server_app.workspace.ensure_tenant("beta", self.workspace)
        foreign = self.server_app.workspace.call("beta", "session_create", {})
        with self.assertRaises(MCPError):
            client.call_tool("session_get", {"session_id": foreign["id"]})


if __name__ == "__main__":
    unittest.main()
