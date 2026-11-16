"""Replaceable Agent workers. All tools, checkpoints and jobs belong to MCP."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import threading
import time
import uuid

from .context import message_text, prepare_context, working_message
from .mcp import MCPError, MCPToolError, MCPTransportError
from .providers import create_provider
from .providers.base import ProviderError, validate_calls
from .providers.retry import retry_call

DEFAULT_SYSTEM = """You are an Agent working in the user's remote workspace.
Complete the requested work and verify the result through the available tools.
Respect the user's goals, scope and explicit constraints. Preserve existing work.
Base your claims on observed evidence and report errors and remaining uncertainty accurately.
All tools execute remotely. Paths and commands refer to the remote workspace.
Read the relevant SKILL.md before using a skill's workflow. Use shell and skills for concrete work.
Keep useful results in the workspace. Give concise progress updates for multi-step work.
Continue after tool results until the task is complete or user input is genuinely required.
"""
TERMINAL_JOBS = {"completed", "failed", "cancelled"}


class LeaseLost(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


def default_model_factory(config):
    provider = config.get("provider") or "openai"
    key = config.get("api_key") or os.environ.get(provider.upper() + "_API_KEY", "")
    model = config.get("model") or os.environ.get(provider.upper() + "_MODEL", "")
    if not key or not model:
        raise ValueError("请在会话设置中配置模型名称和 API Key。")
    return create_provider(
        provider, model, key,
        base_url=config.get("base_url") or os.environ.get(provider.upper() + "_BASE_URL") or None,
        api_format=config.get("api_format") or "auto",
        max_output_tokens=int(config.get("max_output_tokens") or 8192),
        model_context_window=int(config.get("context_window") or 128000),
        deepseek_effort=config.get("deepseek_effort"),
    )


class RemoteSession:
    def __init__(self, mcp, session_id, *, ttl=30):
        self.mcp, self.id, self.ttl = mcp, session_id, ttl
        self.owner = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.cancelled = threading.Event()
        self.lost = None
        started = time.monotonic()
        claim = self.mcp.call_tool("session_claim", {"session_id": session_id, "owner": self.owner, "ttl": ttl})
        self.token = claim["lease_token"]
        self.data = claim["session"]
        self.epoch = self.data["lease"]["epoch"]
        # Start before the request: network delay must never extend ownership.
        self.deadline = started + ttl
        self.thread = threading.Thread(target=self._renew, name="session-lease", daemon=True)
        self.thread.start()

    def _renew(self):
        while not self.stopped.wait(self.ttl / 3):
            try:
                with self.lock:
                    self._check_lease()
                    started = time.monotonic()
                    result = self.mcp.call_tool("session_claim", {"session_id": self.id, "owner": self.owner, "ttl": self.ttl})
                    if result["lease_token"] != self.token:
                        raise LeaseLost("续租期间原租约已过期。")
                    self.deadline = started + self.ttl
                    self.data = result["session"]
                    if self.data.get("cancel_requested"):
                        self.cancelled.set()
            except Exception as exc:
                self.lost = exc
                return

    def check(self):
        self._check_lease()
        if self.cancelled.is_set():
            raise RunCancelled("用户已取消执行。")

    def _check_lease(self):
        if time.monotonic() >= self.deadline and self.lost is None:
            self.lost = LeaseLost("会话执行租约已过期。")
        if self.lost is not None:
            raise LeaseLost("会话执行租约已失效，请重新接管会话。") from self.lost

    def _verify_owner(self):
        lease = self.data.get("lease") or {}
        if lease.get("owner") != self.owner or lease.get("epoch") != self.epoch:
            self.lost = LeaseLost("其他进程已接管会话。")
        self._check_lease()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.data)

    def refresh(self):
        with self.lock:
            self._check_lease()
            self.data = self.mcp.call_tool("session_get", {"session_id": self.id})
            self._verify_owner()
            if self.data.get("cancel_requested"):
                self.cancelled.set()
            return deepcopy(self.data)

    def update(self, **patch):
        with self.lock:
            self._check_lease()
            try:
                self.data = self.mcp.call_tool("session_update", {
                    "session_id": self.id, "expected_revision": self.data["revision"],
                    "patch": patch, "lease_token": self.token,
                })
            except MCPToolError as exc:
                if exc.code == "lease_conflict":
                    self.lost = exc
                    raise LeaseLost("会话执行租约已失效。") from exc
                raise
            self._verify_owner()
            return deepcopy(self.data)

    def context(self, operation_id=None):
        with self.lock:
            self.check()
            result = {"session_id": self.id, "lease_token": self.token}
            if operation_id:
                result["operation_id"] = operation_id
            return result

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=self.ttl + 1)
        if self.lost is None:
            try:
                self.mcp.call_tool("session_release", {"session_id": self.id, "lease_token": self.token})
            except (MCPError, RuntimeError):
                pass


class RemoteTools:
    def __init__(self, mcp):
        self.mcp = mcp
        self.refresh()

    def refresh(self):
        self.specs = {spec["name"]: spec for spec in self.mcp.list_tools()
                      if spec.get("_meta", {}).get("io.mcp-agent/visibility") != "runtime"}

    @property
    def schemas(self):
        return [{"name": spec["name"], "description": spec.get("description", ""),
                 "parameters": deepcopy(spec["inputSchema"])} for spec in self.specs.values()]

    def call(self, name, arguments, context):
        if name not in self.specs:
            self.refresh()
        if name not in self.specs:
            return {"isError": True, "error": f"Unknown or unavailable tool: {name}"}
        try:
            return self.mcp.call_tool(name, arguments, context)
        except MCPToolError as exc:
            if exc.code == "lease_conflict":
                raise LeaseLost("远程工具拒绝了过期的会话租约。") from exc
            if exc.code == "cancelled":
                raise RunCancelled("用户已取消执行。") from exc
            return {"isError": True, "error": str(exc), "details": exc.data}


class AgentRunner:
    def __init__(self, mcp, model_factory=None, *, poll_interval=0.5, lease_ttl=30):
        self.mcp = mcp
        self.model_factory = model_factory or default_model_factory
        self.poll_interval = poll_interval
        self.lease_ttl = lease_ttl

    def cancel(self, session_id):
        result = self.mcp.call_tool("session_cancel", {"session_id": session_id})
        current = self.mcp.call_tool("session_get", {"session_id": session_id})
        pending = current.get("pending") or {}
        for call in pending.get("calls", []):
            if call.get("job_id"):
                self.mcp.call_tool("job_cancel", {"job_id": call["job_id"]})
        return result

    def run(self, session_id, on_event=None):
        emit = on_event or (lambda event: None)
        session = RemoteSession(self.mcp, session_id, ttl=self.lease_ttl)
        try:
            initial = session.snapshot()
            if initial.get("cancel_requested"):
                session.cancelled.set()
                raise RunCancelled("用户已取消执行。")
            messages = initial.get("messages", [])
            if (not initial.get("pending") and messages and messages[-1].get("role") == "assistant"
                    and not messages[-1].get("tool_calls")):
                return session.update(status="completed", activity=None, draft=None, error=None)
            tools = RemoteTools(self.mcp)
            workspace = self.mcp.call_tool("workspace_info", {})
            model = self.model_factory(initial.get("config") or {})
            if self.model_factory is default_model_factory:
                config = deepcopy(initial.get("config") or {})
                config.update(provider=config.get("provider") or "openai", model=model.model,
                              api_key=model.api_key, base_url=model.base_url)
                initial["config"] = config
                session.update(config=config)
            session.update(status="running", error=None, activity={"kind": "starting"}, draft=None)
            system = self._system(initial.get("config") or {}, workspace)
            while True:
                session.refresh()
                session.check()
                state = session.snapshot()
                if state.get("pending"):
                    self._execute_pending(session, tools, emit)
                    continue
                messages = state.get("messages", [])
                if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
                    return session.update(status="completed", activity=None, draft=None, error=None)
                tools.refresh()
                system_message, working, context = prepare_context(
                    state, model, system, tools.schemas,
                    on_activity=lambda activity: session.update(activity=activity),
                )
                session.update(context=context, activity={"kind": "model", "message": "模型正在生成回应"})
                reply = self._complete(model, system_message, working, tools.schemas, session, emit)
                session.refresh()
                session.check()
                validate_calls(reply.get("tool_calls", []))
                accepted = working_message(reply)
                accepted["role"] = "assistant"
                calls = [{**deepcopy(call), "operation_id": uuid.uuid4().hex}
                         for call in accepted.get("tool_calls", [])]
                if not calls and not message_text(accepted).strip():
                    raise ValueError("模型没有返回文本或工具调用。")
                messages = session.snapshot().get("messages", []) + [accepted]
                session.update(messages=messages, pending={"calls": calls, "index": 0} if calls else None,
                               draft=None, activity=None)
                emit({"type": "message", "message": accepted})
                if not calls:
                    return session.update(status="completed", error=None)
        except RunCancelled:
            self._finish_cancel(session)
            return session.snapshot()
        except LeaseLost:
            raise
        except Exception as exc:
            try:
                session.refresh()
                session.update(status="error", error=str(exc), activity=None, draft=None)
            except Exception:
                pass
            raise
        finally:
            session.close()

    @staticmethod
    def _system(config, workspace):
        instructions = config.get("system_prompt") or DEFAULT_SYSTEM
        instructions += "\n\nRemote workspace\n" + str(workspace.get("workspace", ""))
        if workspace.get("instructions"):
            instructions += "\n" + workspace["instructions"]
        skills = workspace.get("skills", [])
        if skills:
            instructions += "\n\nAvailable skills\n" + "\n".join(
                f"- {skill['name']}: {skill.get('description', '')} ({skill['path']})" for skill in skills)
        return instructions

    @staticmethod
    def _complete(model, system, messages, schemas, session, emit):
        def operation():
            session.check()
            draft = {"text": "", "thinking": ""}
            saved_at = 0.0

            def receive(event):
                nonlocal saved_at
                session.check()
                emit(event)
                kind = event.get("type")
                if kind in {"text_delta", "thinking_delta"}:
                    key = "text" if kind == "text_delta" else "thinking"
                    draft[key] += event.get("text", "")
                    if time.monotonic() - saved_at >= 0.5:
                        session.update(draft=draft)
                        saved_at = time.monotonic()

            return model.complete(system, deepcopy(messages), deepcopy(schemas), on_event=receive)

        def retry(exc, attempt):
            session.check()
            activity = {"kind": "retry", "attempt": attempt, "message": str(exc)}
            session.update(activity=activity, draft=None)
            emit({"type": "activity", "activity": activity})

        return retry_call(operation, on_retry=retry, max_attempts=3, max_elapsed_seconds=120)

    def _execute_pending(self, session, tools, emit):
        while True:
            state = session.snapshot()
            pending = deepcopy(state["pending"])
            index = pending.get("index", 0)
            if index >= len(pending["calls"]):
                session.update(pending=None, activity=None)
                return
            session.check()
            call = pending["calls"][index]
            session.update(activity={"kind": "tool", "name": call["name"], "tool_call_id": call["id"]})
            try:
                arguments = json.loads(call.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
            except (ValueError, TypeError) as exc:
                result = {"isError": True, "error": str(exc)}
            else:
                if call.get("job_id"):
                    result = self._wait_job(session, call["job_id"], emit)
                else:
                    result = tools.call(call["name"], arguments, session.context(call["operation_id"]))
                    if call["name"] == "shell" and result.get("job_id"):
                        call["job_id"] = result["job_id"]
                        pending["calls"][index] = call
                        session.update(pending=pending)
                        result = self._wait_job(session, call["job_id"], emit)
            session.refresh()
            session.check()
            message = {"role": "tool", "name": call["name"], "tool_call_id": call["id"],
                       "content": json.dumps(result, ensure_ascii=False), "operation_id": call["operation_id"]}
            pending["index"] = index + 1
            session.update(messages=session.snapshot().get("messages", []) + [message], pending=pending)
            emit({"type": "message", "message": message})

    def _wait_job(self, session, job_id, emit):
        while True:
            session.refresh()
            if session.cancelled.is_set():
                self.mcp.call_tool("job_cancel", {"job_id": job_id}, self._cancel_context(session))
            session.check()
            result = self.mcp.call_tool("job_status", {"job_id": job_id, "offset": 0, "max_bytes": 262144})
            session.update(activity={"kind": "job", "job_id": job_id, "status": result.get("status"),
                                     "stdout": result.get("stdout", "")[-8192:], "stderr": result.get("stderr", "")[-8192:]})
            emit({"type": "job", "job": result})
            if result.get("status") in TERMINAL_JOBS:
                return result
            session.stopped.wait(self.poll_interval)

    def _finish_cancel(self, session):
        state = session.refresh()
        pending = state.get("pending") or {}
        messages = state.get("messages", [])
        for call in pending.get("calls", [])[pending.get("index", 0):]:
            job_id = call.get("job_id")
            if job_id:
                self.mcp.call_tool("job_cancel", {"job_id": job_id}, self._cancel_context(session))
            result = {"isError": True, "status": "cancelled", "error": "用户取消了本次执行。"}
            if job_id:
                result["job_id"] = job_id
            messages.append({"role": "tool", "name": call["name"], "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False), "operation_id": call["operation_id"]})
        session.update(messages=messages, pending=None, status="cancelled", error=None,
                       activity=None, draft=None, cancel_requested=False)

    @staticmethod
    def _cancel_context(session):
        # Cancellation itself must remain possible after the cancel flag is set,
        # but never after another runner has acquired the execution lease.
        with session.lock:
            session._check_lease()
            return {"session_id": session.id, "lease_token": session.token}
