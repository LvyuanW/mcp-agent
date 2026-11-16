from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from client.context import message_groups, prepare_context
from client.mcp import MCPToolError, MCPTransportError
from client.runtime import AgentRunner, RemoteSession, RemoteTools
from server.workspace import WorkspaceService


class InProcessMCP:
    """Transport fixture using the real durable workspace implementation."""
    def __init__(self, service, tenant="alice", fail_after_shell=False):
        self.service = service
        self.tenant = tenant
        self.fail_after_shell = fail_after_shell
        self.discoveries = 0
        self.shell_operations = []

    def list_tools(self):
        self.discoveries += 1
        return self.service.tool_specs()

    def call_tool(self, name, arguments, context=None):
        try:
            result = self.service.call(self.tenant, name, arguments, context)
        except (ValueError, LookupError, RuntimeError) as exc:
            raise MCPToolError(str(exc), data={"error": str(exc)}) from exc
        if name == "shell":
            self.shell_operations.append(context["operation_id"])
            if self.fail_after_shell:
                self.fail_after_shell = False
                raise MCPTransportError("Connection lost after remote acceptance")
        return result


class Model:
    max_output_tokens = 1024

    def __init__(self, command="printf 'remote-ok\\n'"):
        self.command = command
        self.seen = []

    def count_tokens(self, system, messages, tools):
        return 100

    def complete(self, system, messages, tools, on_event=None, **kwargs):
        self.seen.append(deepcopy(messages))
        if on_event:
            on_event({"type": "text_delta", "text": "正在处理。"})
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": [{"type": "text", "text": "已执行并检查结果。"}]}
        return {"role": "assistant", "content": [{"type": "text", "text": "读取远程工作区。"}],
                "tool_calls": [{"id": "model-call-1", "name": "shell",
                                "arguments": json.dumps({"command": self.command})}]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.service = WorkspaceService(self.root / "state")
        self.service.ensure_tenant("alice", self.workspace)
        self.mcp = InProcessMCP(self.service)
        self.session = self.mcp.call_tool("session_create", {"title": "Remote work"})
        self.session = self.mcp.call_tool("session_update", {
            "session_id": self.session["id"], "expected_revision": self.session["revision"],
            "patch": {"messages": [{"role": "user", "content": "运行检查"}]},
        })

    def tearDown(self):
        with self.service._connection() as connection:
            jobs = [row[0] for row in connection.execute("SELECT id FROM jobs")]
        for job_id in jobs:
            self.mcp.call_tool("job_cancel", {"job_id": job_id})
        for job_id in jobs:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.mcp.call_tool("job_status", {"job_id": job_id})
                if job["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
        self.temporary.cleanup()

    def test_dynamic_remote_tools_and_complete_round_trip(self):
        model = Model()
        runner = AgentRunner(self.mcp, lambda config: model, poll_interval=0.02)
        result = runner.run(self.session["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["pending"])
        self.assertGreater(self.mcp.discoveries, 0)
        tool_message = next(message for message in result["messages"] if message["role"] == "tool")
        output = json.loads(tool_message["content"])
        self.assertEqual(output["exit_code"], 0)
        self.assertIn("remote-ok", output["stdout"])
        self.assertEqual(len(model.seen), 2)
        self.assertEqual(model.seen[1][-1]["tool_call_id"], "model-call-1")
        self.assertFalse(result.get("draft"))
        registry = RemoteTools(self.mcp)
        self.assertEqual(set(registry.specs), {"shell", "job_status", "job_cancel"})

    def test_lost_shell_receipt_is_recovered_by_new_harness(self):
        broken = InProcessMCP(self.service, fail_after_shell=True)
        model_a = Model("printf x >> result.txt; printf finished")
        with self.assertRaises(MCPTransportError):
            AgentRunner(broken, lambda config: model_a, poll_interval=0.02).run(self.session["id"])
        interrupted = self.mcp.call_tool("session_get", {"session_id": self.session["id"]})
        self.assertEqual(interrupted["status"], "error")
        operation = interrupted["pending"]["calls"][0]["operation_id"]
        self.assertNotIn("job_id", interrupted["pending"]["calls"][0])

        # A fresh client, provider and harness reconstruct everything from server state.
        restarted_service = WorkspaceService(self.root / "state")
        fresh_mcp = InProcessMCP(restarted_service)
        model_b = Model()
        result = AgentRunner(fresh_mcp, lambda config: model_b, poll_interval=0.02).run(self.session["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(fresh_mcp.shell_operations, [operation])
        self.assertEqual((self.workspace / "result.txt").read_text(), "x")
        self.assertEqual(len(model_b.seen), 1)
        with self.service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_remote_cancellation_closes_pending_call_group(self):
        errors = []
        results = []
        runner = AgentRunner(self.mcp, lambda config: Model("sleep 20"), poll_interval=0.02, lease_ttl=1)

        def run():
            try:
                results.append(runner.run(self.session["id"]))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.mcp.call_tool("session_get", {"session_id": self.session["id"]})
            calls = (current.get("pending") or {}).get("calls", [])
            if calls and calls[0].get("job_id"):
                break
            time.sleep(0.02)
        self.assertTrue(calls and calls[0].get("job_id"))
        AgentRunner(InProcessMCP(self.service)).cancel(self.session["id"])
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "cancelled")
        message_groups(results[0]["messages"])
        self.assertIsNone(results[0]["pending"])

    def test_concurrent_harness_cannot_claim_same_session(self):
        first = RemoteSession(self.mcp, self.session["id"])
        try:
            with self.assertRaises(MCPToolError):
                RemoteSession(InProcessMCP(self.service), self.session["id"])
        finally:
            first.close()

    def test_runtime_control_tools_are_never_model_callable(self):
        registry = RemoteTools(self.mcp)
        result = registry.call("session_create", {}, {})
        self.assertTrue(result["isError"])
        self.assertEqual(len(self.mcp.call_tool("sessions_list", {})["sessions"]), 1)


class CompactionTests(unittest.TestCase):
    def test_compaction_preserves_raw_history_and_complete_tool_groups(self):
        messages = [{"role": "user", "content": "Keep the original requirement."}]
        for index in range(12):
            messages += [
                {"role": "assistant", "tool_calls": [{"id": str(index), "name": "shell", "arguments": "{}"}]},
                {"role": "tool", "tool_call_id": str(index), "content": "evidence " * 40},
            ]
        original = deepcopy(messages)

        class SummaryModel:
            max_output_tokens = 512
            def count_tokens(self, *args):
                return 10000
            def complete(self, *args, **kwargs):
                return {"role": "assistant", "content": "Earlier work and evidence preserved."}

        session = {"messages": messages, "config": {"context_window": 4096, "max_output_tokens": 512}}
        system, selected, context = prepare_context(session, SummaryModel(), "System", [])
        self.assertEqual(messages, original)
        self.assertGreater(context["cursor"], 0)
        self.assertIn("Earlier work", system)
        self.assertEqual(selected[0]["content"], "Keep the original requirement.")
        message_groups(selected)
        self.assertLess(len(selected), len(messages))


if __name__ == "__main__":
    unittest.main()
