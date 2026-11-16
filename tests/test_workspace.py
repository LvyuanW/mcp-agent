import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from server.workspace import ConflictError, InputError, NotFoundError, WorkspaceService


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.other_workspace = self.root / "other-project"
        self.other_workspace.mkdir()
        self.service = WorkspaceService(self.root / "state")
        self.service.ensure_tenant("alice", self.workspace)
        self.service.ensure_tenant("bob", self.other_workspace)
        self.jobs = []

    def tearDown(self):
        for tenant, job_id in self.jobs:
            self.service.call(tenant, "job_cancel", {"job_id": job_id})
            self.wait(job_id, tenant=tenant)
        self.temporary.cleanup()

    def call(self, name, arguments=None, context=None, tenant="alice"):
        return self.service.call(tenant, name, arguments or {}, context)

    def shell(self, command, *, operation="operation-1", timeout=5, context=None):
        context = {"operation_id": operation, **(context or {})}
        job = self.call("shell", {"command": command, "timeout_seconds": timeout}, context)
        self.jobs.append(("alice", job["job_id"]))
        return job

    def wait(self, job_id, *, tenant="alice", timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.service.call(tenant, "job_status", {"job_id": job_id})
            if result["status"] in {"completed", "failed", "cancelled"}:
                return result
            time.sleep(0.05)
        self.fail(f"Job {job_id} did not finish: {result}")

    def test_workspace_mapping_requires_existing_directory(self):
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError):
            self.service.ensure_tenant("new", missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(ConflictError):
            self.service.ensure_tenant("alice", self.other_workspace)

    def test_tenant_sessions_and_files_are_separate(self):
        session = self.call("session_create", {"title": "alice session", "config": {"model": "test"}})
        with self.assertRaises(NotFoundError):
            self.call("session_get", {"session_id": session["id"]}, tenant="bob")
        with self.assertRaises(NotFoundError):
            self.call("session_claim", {"session_id": session["id"], "owner": "bob"}, tenant="bob")
        self.assertEqual(self.call("sessions_list", tenant="bob"), {"sessions": []})
        (self.workspace / "hello.txt").write_text("你好\n", encoding="utf-8")
        result = self.call("file_read", {"path": "hello.txt"})
        self.assertEqual(result["content"], "你好\n")
        self.assertEqual(len(result["revision"]), 64)
        with self.assertRaises(InputError):
            self.call("file_read", {"path": "../state/workspace.sqlite3"})

    def test_lease_cas_expiration_and_fencing(self):
        session = self.call("session_create")
        arguments = {"session_id": session["id"], "owner": "runner-a", "ttl": 0.15}
        claimed = self.call("session_claim", arguments)
        with self.assertRaises(ConflictError):
            self.call("session_claim", {**arguments, "owner": "runner-b"})
        with self.assertRaises(ConflictError):
            self.call("session_update", {"session_id": session["id"], "expected_revision": 0, "patch": {"status": "running"}})
        updated = self.call("session_update", {"session_id": session["id"], "expected_revision": 0,
            "lease_token": claimed["lease_token"], "patch": {"status": "running", "draft": {"text": "draft"}}})
        self.assertEqual(updated["revision"], 1)
        with self.assertRaises(ConflictError) as failed:
            self.call("session_update", {"session_id": session["id"], "expected_revision": 0,
                "lease_token": claimed["lease_token"], "patch": {"status": "idle"}})
        self.assertEqual(failed.exception.code, "revision_conflict")
        time.sleep(0.2)
        replacement = self.call("session_claim", {**arguments, "owner": "runner-b", "ttl": 30})
        self.assertNotEqual(replacement["lease_token"], claimed["lease_token"])
        with self.assertRaises(ConflictError):
            self.call("session_update", {"session_id": session["id"], "expected_revision": 1,
                "lease_token": claimed["lease_token"], "patch": {"status": "completed"}})
        with self.assertRaises(ConflictError):
            self.call("session_release", {"session_id": session["id"], "lease_token": claimed["lease_token"]})
        self.assertEqual(replacement["session"]["revision"], 1)

    def test_cancellation_signal_does_not_race_revision(self):
        session = self.call("session_create")
        claimed = self.call("session_claim", {"session_id": session["id"], "owner": "runner"})
        cancelled = self.call("session_cancel", {"session_id": session["id"]})
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["revision"], session["revision"])
        updated = self.call("session_update", {"session_id": session["id"], "expected_revision": 0,
            "lease_token": claimed["lease_token"], "patch": {"status": "cancelled"}})
        self.assertTrue(updated["cancel_requested"])

    def test_shell_success_failure_and_operation_deduplication(self):
        first = self.shell("printf 'once\\n' >> count.txt; printf 'out'; printf 'err' >&2")
        second = self.shell("printf 'once\\n' >> count.txt; printf 'out'; printf 'err' >&2")
        self.assertEqual(first["job_id"], second["job_id"])
        result = self.wait(first["job_id"])
        self.assertEqual((result["status"], result["exit_code"], result["stdout"], result["stderr"]),
                         ("completed", 0, "out", "err"))
        self.assertEqual((self.workspace / "count.txt").read_text(), "once\n")
        with self.assertRaises(ConflictError):
            self.shell("printf different", operation="operation-1")
        failure = self.wait(self.shell("printf 'failure' >&2; exit 7", operation="failure")["job_id"])
        self.assertEqual((failure["status"], failure["exit_code"]), ("failed", 7))
        with self.assertRaises(NotFoundError):
            self.call("job_status", {"job_id": first["job_id"]}, tenant="bob")
        with self.assertRaises(NotFoundError):
            self.call("job_cancel", {"job_id": first["job_id"]}, tenant="bob")

    def test_concurrent_shell_submissions_execute_once(self):
        def submit(_):
            return self.call("shell", {"command": "printf 'one\\n' >> concurrent.txt", "timeout_seconds": 5},
                             {"operation_id": "concurrent"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(submit, range(4)))
        identifiers = {reply["job_id"] for reply in replies}
        self.assertEqual(len(identifiers), 1)
        identifier = identifiers.pop()
        self.jobs.append(("alice", identifier))
        self.assertEqual(self.wait(identifier)["status"], "completed")
        self.assertEqual((self.workspace / "concurrent.txt").read_text(), "one\n")

    def test_shell_requires_operation_and_current_session_lease(self):
        with self.assertRaises(InputError):
            self.call("shell", {"command": "true"})
        session = self.call("session_create")
        with self.assertRaises(ConflictError):
            self.shell("true", context={"session_id": session["id"]})
        lease = self.call("session_claim", {"session_id": session["id"], "owner": "runner"})
        job = self.shell("true", context={"session_id": session["id"], "lease_token": lease["lease_token"]})
        self.assertEqual(self.wait(job["job_id"])["status"], "completed")

    def test_cancellation_and_timeout_stop_child_processes(self):
        job = self.shell("sleep 3; printf 'wrong' > cancelled.txt", operation="cancel", timeout=10)
        self.call("job_cancel", {"job_id": job["job_id"]})
        self.assertEqual(self.wait(job["job_id"])["status"], "cancelled")
        timed = self.shell("sleep 3; printf 'wrong' > timed.txt", operation="timeout", timeout=0.2)
        result = self.wait(timed["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout_seconds", result["error"])
        self.assertFalse((self.workspace / "cancelled.txt").exists())
        self.assertFalse((self.workspace / "timed.txt").exists())

    def test_subprocess_environment_excludes_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "provider-secret", "OAUTH_CLIENT_SECRET": "oauth-secret",
                                     "ANTHROPIC_API_KEY": "another-secret", "CUSTOM_PRIVATE_TOKEN": "private"}):
            result = self.wait(self.shell("env", operation="environment")["job_id"])
        for secret in ("provider-secret", "oauth-secret", "another-secret", "CUSTOM_PRIVATE_TOKEN"):
            self.assertNotIn(secret, result["stdout"])
        self.assertIn(f"MCP_WORKSPACE={self.workspace}", result["stdout"])

    def test_logs_have_independent_cursors(self):
        job = self.shell("printf 'abcdefghij'; printf '1234567' >&2", operation="logs")
        self.wait(job["job_id"])
        first = self.call("job_status", {"job_id": job["job_id"], "max_bytes": 4})
        self.assertEqual((first["stdout"], first["stderr"]), ("abcd", "1234"))
        self.assertTrue(first["truncated"])
        second = self.call("job_status", {"job_id": job["job_id"], "max_bytes": 4, "offset": first["next_offset"]})
        self.assertEqual((second["stdout"], second["stderr"]), ("efgh", "567"))
        third = self.call("job_status", {"job_id": job["job_id"], "max_bytes": 4, "offset": second["next_offset"]})
        self.assertEqual((third["stdout"], third["stderr"]), ("ij", ""))
        self.assertFalse(third["truncated"])

    def test_job_survives_creator_process_exit_and_service_restart(self):
        code = """
import json, os, sys
from pathlib import Path
from server.workspace import WorkspaceService
service = WorkspaceService(Path(sys.argv[1]))
job = service.call('alice', 'shell', {'command': "sleep 0.5; printf 'persisted' > durable.txt", 'timeout_seconds': 5},
                   {'operation_id': 'restart'})
print(json.dumps(job), flush=True)
os._exit(0)
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.service.data_dir)],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        job = json.loads(process.stdout)
        self.jobs.append(("alice", job["job_id"]))
        self.service = WorkspaceService(self.root / "state")
        result = self.wait(job["job_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.workspace / "durable.txt").read_text(), "persisted")
        duplicate = self.shell("sleep 0.5; printf 'persisted' > durable.txt", operation="restart")
        self.assertEqual(duplicate["job_id"], job["job_id"])

    def test_queued_operation_recovers_after_service_restart(self):
        with patch.object(self.service, "_start_worker"):
            job = self.shell("printf recovered", operation="queued")
        self.assertEqual(job["status"], "queued")
        self.service = WorkspaceService(self.root / "state")
        self.assertEqual(self.wait(job["job_id"])["stdout"], "recovered")

    def test_executable_skills_are_remote_files(self):
        info = self.call("workspace_info")
        self.assertEqual(info["workspace"], str(self.workspace))
        self.assertEqual({skill["name"] for skill in info["skills"]}, {"remote-shell", "inspect-jobs"})
        result = self.wait(self.shell("cat " + shlex.quote(info["skills"][0]["path"]), operation="skill")["job_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("name:", result["stdout"])

    def test_binary_artifact_roundtrip_and_revision_conflict(self):
        content = bytes(range(256)) * 20
        arguments = {"path": "artifacts/output.bin", "content_base64": base64.b64encode(content).decode(),
                     "expected_revision": None}
        uploaded = self.call("file_upload", arguments)
        downloaded = self.call("file_download", {"path": arguments["path"]})
        self.assertEqual(base64.b64decode(downloaded["content_base64"]), content)
        self.assertEqual(downloaded["mime_type"], "application/octet-stream")
        self.assertEqual(downloaded["revision"], uploaded["revision"])
        with self.assertRaises(ConflictError):
            self.call("file_upload", arguments)
        arguments.update(expected_revision=uploaded["revision"], content_base64=base64.b64encode(b"updated").decode())
        self.call("file_upload", arguments)
        with self.assertRaises(ConflictError):
            self.call("file_upload", arguments)
        self.assertEqual((self.workspace / "artifacts/output.bin").read_bytes(), b"updated")

    def test_artifact_traversal_and_symlinks_are_rejected(self):
        outside = self.other_workspace / "secret.bin"
        outside.write_bytes(b"other-tenant")
        (self.workspace / "link").symlink_to(self.other_workspace, target_is_directory=True)
        for path in ("../other-project/secret.bin", str(outside), "link/secret.bin"):
            with self.subTest(path=path):
                with self.assertRaises(InputError):
                    self.call("file_download", {"path": path})
                with self.assertRaises(InputError):
                    self.call("file_upload", {"path": path, "content_base64": "b3ZlcndyaXR0ZW4="})
        self.assertEqual(outside.read_bytes(), b"other-tenant")
        with self.assertRaises(InputError):
            self.call("file_upload", {"path": "invalid.bin", "content_base64": "not!base64"})

    def test_stale_runner_cannot_cancel_job(self):
        session = self.call("session_create")
        claim = self.call("session_claim", {"session_id": session["id"], "owner": "runner"})
        context = {"session_id": session["id"], "lease_token": claim["lease_token"]}
        job = self.shell("sleep 3", operation="fenced-cancel", timeout=5, context=context)
        self.call("session_release", {"session_id": session["id"], "lease_token": claim["lease_token"]})
        with self.assertRaises(ConflictError):
            self.call("job_cancel", {"job_id": job["job_id"]}, context)
        self.call("session_cancel", {"session_id": session["id"]})
        self.assertEqual(self.wait(job["job_id"])["status"], "cancelled")

    def test_execution_user_requires_privileged_configuration(self):
        if os.geteuid() == 0:
            with self.assertRaises(InputError):
                self.service.ensure_tenant("root-execution", self.workspace, execution_user="root")
        else:
            with self.assertRaises(InputError):
                self.service.ensure_tenant("separate-user", self.workspace, execution_user="nobody")


if __name__ == "__main__":
    unittest.main()
