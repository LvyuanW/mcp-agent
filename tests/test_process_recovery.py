"""Crash/replacement proof using actual harness processes and verified TLS."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from client.mcp import MCPClient
from common.http import serve_https
from scripts.dev_certs import generate
from server.app import Application, make_handler


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.certs = generate(self.root / "tls")
        self.workspace = self.root / "remote-workspace"
        self.workspace.mkdir()
        with socket.socket() as socket_probe:
            socket_probe.bind(("127.0.0.1", 0))
            port = socket_probe.getsockname()[1]
        self.origin = f"https://localhost:{port}"
        redirect = "https://client.test/auth/callback"
        self.app = Application({"public_url": self.origin, "data_dir": str(self.root / "server-state"),
                                "redirect_uris": [redirect], "token_ttl": 60})
        self.app.workspace.ensure_tenant("process-tenant", self.workspace)
        self.app.auth.put_user("process-user", "process-tenant", "a-test-only-password")
        verifier = "v" * 64
        params = {"client_id": "mcp-agent-web", "redirect_uri": redirect, "resource": self.origin + "/mcp",
                  "response_type": "code", "code_challenge_method": "S256", "state": "test-state",
                  "scope": "workspace:read workspace:write agent:run",
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        code = self.app.auth.authorize(params, "process-user", "a-test-only-password")
        self.token = self.app.auth.token({"grant_type": "authorization_code", "code": code,
                                          "code_verifier": verifier, "client_id": "mcp-agent-web",
                                          "redirect_uri": redirect, "resource": self.origin + "/mcp"})["access_token"]
        handler = make_handler(self.app)
        handler.log_message = lambda *args: None
        self.server = serve_https(handler, "127.0.0.1", port, str(self.certs / "localhost.pem"),
                                  str(self.certs / "localhost-key.pem"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.mcp = MCPClient(self.origin + "/mcp", self.token, str(self.certs / "ca.pem"))
        self.children = []
        self.job_id = None

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        if self.job_id:
            self.mcp.call_tool("job_cancel", {"job_id": self.job_id})
            self.wait_for(lambda: self.mcp.call_tool("job_status", {"job_id": self.job_id})["status"]
                          in {"completed", "failed", "cancelled"})
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.03)
        self.fail("Timed out waiting for durable remote state")

    def launch(self, session_id, directory):
        directory.mkdir()
        fixture = Path(__file__).with_name("process_harness_fixture.py").resolve()
        child = subprocess.Popen([sys.executable, "-B", str(fixture), "--endpoint", self.origin + "/mcp",
                                  "--token", self.token, "--ca", str(self.certs / "ca.pem"), "--session", session_id],
                                 cwd=directory, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def test_fresh_process_restores_live_job_and_executes_it_once(self):
        config = {"model": "process-recovery-fixture", "api_key": "test-only-no-model-network",
                  "proof": "stored-only-on-mcp", "context_window": 128000,
                  "command": "printf x >> once.txt; while [ ! -f release-job ]; do sleep 0.05; done; printf process-recovered"}
        session = self.mcp.call_tool("session_create", {"title": "Process recovery", "config": config})
        identifier = session["id"]
        self.mcp.call_tool("session_update", {"session_id": identifier, "expected_revision": session["revision"],
            "patch": {"messages": [{"role": "user", "content": "Execute the configured remote job exactly once."}]}})
        first_dir, second_dir = self.root / "harness-a", self.root / "harness-b"
        first = self.launch(identifier, first_dir)

        def checkpoint_ready():
            current = self.mcp.call_tool("session_get", {"session_id": identifier})
            calls = (current.get("pending") or {}).get("calls", [])
            if calls and calls[0].get("job_id"):
                self.job_id = calls[0]["job_id"]
                if self.mcp.call_tool("job_status", {"job_id": self.job_id})["status"] == "running":
                    return current

        checkpoint = self.wait_for(checkpoint_ready)
        self.assertEqual(checkpoint["config"], config)
        self.assertEqual([message["role"] for message in checkpoint["messages"]], ["user", "assistant"])
        first.terminate()
        first.communicate(timeout=5)
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.mcp.call_tool("job_status", {"job_id": self.job_id})["status"], "running")
        self.wait_for(lambda: time.time() > self.mcp.call_tool("session_get", {"session_id": identifier})["lease"]["expires_at"])

        second = self.launch(identifier, second_dir)

        def replacement_claimed():
            current = self.mcp.call_tool("session_get", {"session_id": identifier})
            if current.get("lease") and current["lease"]["epoch"] > checkpoint["lease"]["epoch"]:
                return current

        restored = self.wait_for(replacement_claimed)
        self.assertEqual(restored["messages"], checkpoint["messages"])
        self.assertEqual(restored["pending"], checkpoint["pending"])
        self.assertEqual(restored["config"], config)
        self.assertEqual(self.mcp.call_tool("job_status", {"job_id": self.job_id})["status"], "running")
        self.mcp.call_tool("file_upload", {"path": "release-job", "content_base64": "", "expected_revision": None})
        stdout, stderr = second.communicate(timeout=8)
        self.assertEqual(second.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {"pid": second.pid, "status": "completed", "proof": config["proof"]})
        self.assertNotEqual(first.pid, second.pid)
        result = self.mcp.call_tool("session_get", {"session_id": identifier})
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["messages"][:2], checkpoint["messages"])
        self.assertEqual([message["role"] for message in result["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(json.loads(result["messages"][2]["content"])["job_id"], self.job_id)
        self.assertEqual(self.mcp.call_tool("file_read", {"path": "once.txt"})["content"], "x")
        with self.app.workspace._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE session_id=?", (identifier,)).fetchone()[0], 1)
        self.assertEqual(list(first_dir.iterdir()), [])
        self.assertEqual(list(second_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
