from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from client.app import Application, make_handler, public_session
from client.mcp import MCPToolError
from server.workspace import WorkspaceService


class WebWorkerTests(unittest.TestCase):
    def application(self):
        app = Application({"public_url": "https://client.test", "mcp_url": "https://workspace.test/mcp",
                           "cookie_secret": "test-cookie-secret" * 4})
        app.mcp = lambda token: token
        return app

    def test_request_arriving_before_old_worker_exits_is_drained(self):
        app = self.application()
        first_released_lease = threading.Event()
        finish_first = threading.Event()
        handled_second = threading.Event()
        tokens = []
        active = 0
        peak_active = 0

        class ControlledRunner:
            def __init__(inner, token, model_factory=None):
                inner.token = token

            def run(inner, session_id):
                nonlocal active, peak_active
                active += 1
                peak_active = max(peak_active, active)
                tokens.append(inner.token)
                try:
                    if len(tokens) == 1:
                        # Remote work has finished; the Python thread has not exited.
                        first_released_lease.set()
                        if not finish_first.wait(3):
                            raise RuntimeError("Test did not release the first worker")
                    else:
                        handled_second.set()
                finally:
                    active -= 1

        with patch("client.app.AgentRunner", ControlledRunner):
            app.start_runner("initial-token", "tenant", "session")
            self.assertTrue(first_released_lease.wait(3))
            with app.worker_lock:
                thread = app.workers[("tenant", "session")]["thread"]
            app.start_runner("refreshed-token", "tenant", "session")
            finish_first.set()
            self.assertTrue(handled_second.wait(3), "New request was lost during worker exit")
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(tokens, ["initial-token", "refreshed-token"])
        self.assertEqual(peak_active, 1)
        self.assertEqual(app.workers, {})

    def test_public_session_does_not_expose_nested_lease_credentials(self):
        source = {"config": {"api_key": "model-secret"}, "lease_token": "outer-secret",
                  "lease": {"owner": "worker", "expires_at": 123, "epoch": 4,
                            "token": "nested-secret", "lease_token": "nested-secret-2"}}
        original = deepcopy(source)
        visible = public_session(source)
        self.assertEqual(visible["config"], {"api_key_set": True})
        self.assertEqual(visible["lease"], {"owner": "worker", "expires_at": 123, "epoch": 4})
        self.assertNotIn("lease_token", visible)
        self.assertEqual(source, original)


class CancelledOrphanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.service = WorkspaceService(root / "state")
        self.service.ensure_tenant("tenant", workspace)
        self.session = self.service.call("tenant", "session_create", {})
        self.session = self.service.call("tenant", "session_update", {
            "session_id": self.session["id"], "expected_revision": self.session["revision"],
            "patch": {"status": "running", "messages": [{"role": "user", "content": "Resume work"}]}})
        self.service.call("tenant", "session_claim", {"session_id": self.session["id"], "owner": "old-worker"})
        self.service.call("tenant", "session_cancel", {"session_id": self.session["id"]})

    def request_resume(self):
        service = self.service
        starts = []

        class Remote:
            def call_tool(inner, name, arguments):
                try:
                    return service.call("tenant", name, arguments)
                except RuntimeError as exc:
                    raise MCPToolError(str(exc), code=exc.code,
                                       data={"error": {"code": exc.code, "message": str(exc)}}) from exc

        class App:
            def start_runner(inner, *args):
                starts.append(args)

        handler = object.__new__(make_handler(App()))
        handler.path = "/api/sessions/" + self.session["id"] + "/resume"
        handler.check_mutation = lambda: None
        handler.remote = lambda: (Remote(), "access-token")
        handler.json_body = lambda: {}
        replies = []
        handler.respond = lambda status, body: replies.append((status, body))
        handler.fail = lambda status, message, code=None: replies.append((status, {"message": message, "code": code}))
        handler.do_POST()
        return replies, starts

    def test_cancelled_orphan_resumes_after_lease_expiry_in_one_request(self):
        with self.service._connection(transaction=True) as connection:
            connection.execute("UPDATE sessions SET lease_expires = 0 WHERE id = ?", (self.session["id"],))
        replies, starts = self.request_resume()
        self.assertEqual(replies, [(202, {"accepted": True})])
        self.assertEqual(starts, [("access-token", "tenant", self.session["id"])])
        current = self.service.call("tenant", "session_get", {"session_id": self.session["id"]})
        self.assertFalse(current["cancel_requested"])

    def test_resume_cannot_clear_an_active_workers_cancellation(self):
        replies, starts = self.request_resume()
        self.assertEqual(replies[0][0], 409)
        self.assertEqual(starts, [])
        current = self.service.call("tenant", "session_get", {"session_id": self.session["id"]})
        self.assertTrue(current["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
