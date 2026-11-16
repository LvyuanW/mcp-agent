"""Verified HTTPS client for MCP 2026-07-28 Streamable HTTP."""

from __future__ import annotations

import base64
from copy import deepcopy
from http.client import HTTPException
import json
import logging
import ssl
from urllib import error, request
from urllib.parse import urlsplit
import uuid

PROTOCOL_VERSION = "2026-07-28"
META_PREFIX = "io.modelcontextprotocol/"
CONTEXT_PREFIX = "io.mcp-agent/"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class MCPError(RuntimeError):
    def __init__(self, message, *, code=None, status=None, data=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data


class MCPTransportError(MCPError):
    """The caller must recover using durable operation IDs after uncertain delivery."""


class MCPToolError(MCPError):
    pass


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MCPError("MCP endpoints must not redirect authenticated requests", status=code)


def encode_header(value) -> str:
    if type(value) is bool:
        value = "true" if value else "false"
    elif type(value) is int:
        value = str(value)
    elif not isinstance(value, str):
        raise MCPError("MCP mirrored headers require string, integer or boolean values")
    safe = value == value.strip() and all(32 <= ord(char) <= 126 for char in value)
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if safe and not sentinel:
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def header_bindings(schema: dict) -> list[tuple[str, tuple[str, ...]]]:
    """Validate all annotations, allowing only statically reachable properties."""
    bindings = []
    names = set()

    def walk(node, path, reachable):
        if isinstance(node, list):
            for item in node:
                walk(item, path, False)
            return
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name = node["x-mcp-header"]
            valid_chars = "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if (not reachable or not path or not isinstance(name, str) or not name
                    or any(char not in valid_chars for char in name)
                    or name.lower() in names or not isinstance(node.get("type"), str)
                    or node["type"] not in {"string", "integer", "boolean"}):
                raise MCPError("Invalid x-mcp-header annotation in tool schema")
            names.add(name.lower())
            bindings.append((name, path))
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for child_name, child in value.items():
                    walk(child, path + (child_name,), reachable)
            elif isinstance(value, (dict, list)):
                walk(value, path, False)

    walk(schema, (), True)
    return bindings


def sse_messages(stream):
    """Parse UTF-8 SSE events including comments, CRLF and multiline data."""
    data = []
    size = 0
    for raw_line in stream:
        size += len(raw_line)
        if size > MAX_RESPONSE_BYTES:
            raise MCPError("MCP response exceeds the 32 MiB response limit")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield json.loads("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" ") if separator else "")
    # SSE dispatch requires an event boundary. A partial final event is lost.


class MCPClient:
    def __init__(self, endpoint: str, access_token, ca_file=None, *, timeout=120, on_notification=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP endpoint must be an HTTPS URL without credentials or fragment")
        self.endpoint = endpoint
        self.access_token = access_token
        self.timeout = timeout
        self.on_notification = on_notification or (lambda message: None)
        self.context = ssl.create_default_context(cafile=ca_file)
        self.opener = request.build_opener(request.HTTPSHandler(context=self.context), _NoRedirect())
        self._tools = {}

    def call(self, method: str, params: dict | None = None) -> dict:
        params = deepcopy(params or {})
        meta = params.setdefault("_meta", {})
        meta.update({
            META_PREFIX + "protocolVersion": PROTOCOL_VERSION,
            META_PREFIX + "clientCapabilities": {},
            META_PREFIX + "clientInfo": {"name": "mcp-agent", "version": "0.1.0"},
        })
        identifier = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        token = self.access_token() if callable(self.access_token) else self.access_token
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
            "Authorization": "Bearer " + token,
        }
        if method in {"tools/call", "resources/read", "prompts/get"}:
            value = params.get("uri") if method == "resources/read" else params.get("name")
            if not isinstance(value, str) or not value:
                raise MCPError("MCP request requires a resource URI or name")
            headers["Mcp-Name"] = encode_header(value)
        if method == "tools/call" and params.get("name") in self._tools:
            spec = self._tools[params["name"]]
            for name, path in header_bindings(spec.get("inputSchema", {})):
                value = params.get("arguments", {})
                for key in path:
                    value = value.get(key) if isinstance(value, dict) else None
                if value is not None:
                    if type(value) is int and abs(value) > 2 ** 53 - 1:
                        raise MCPError("Mirrored integer is outside the safe range")
                    headers["Mcp-Param-" + name] = encode_header(value)
        req = request.Request(self.endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                return self._read_response(response, identifier)
        except error.HTTPError as exc:
            body = exc.read(MAX_RESPONSE_BYTES)
            try:
                value = json.loads(body)
                problem = value.get("error", {})
                message = problem.get("message", f"MCP HTTP {exc.code}")
            except (ValueError, AttributeError):
                problem = {}
                message = f"MCP HTTP {exc.code}"
            raise MCPError(message, code=problem.get("code"), status=exc.code, data=problem.get("data")) from None
        except (error.URLError, OSError, HTTPException) as exc:
            raise MCPTransportError(f"MCP connection failed: {type(exc).__name__}") from None
        except (UnicodeError, ValueError) as exc:
            raise MCPError(f"Invalid MCP response: {exc}") from None

    def _read_response(self, response, identifier):
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise MCPError("MCP response exceeds the 32 MiB response limit")
            return self._result(json.loads(data), identifier)
        if content_type != "text/event-stream":
            raise MCPError(f"Unsupported MCP response content type: {content_type}")
        for message in sse_messages(response):
            if not isinstance(message, dict):
                raise MCPError("MCP stream messages must be JSON objects")
            if "id" not in message and "method" in message:
                self.on_notification(message)
                continue
            return self._result(message, identifier)
        raise MCPTransportError("MCP stream ended before the final response")

    @staticmethod
    def _result(message, identifier):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != identifier:
            raise MCPError("MCP response ID or JSON-RPC version does not match the request")
        if "error" in message:
            problem = message["error"]
            if not isinstance(problem, dict):
                raise MCPError("MCP JSON-RPC error must be an object")
            raise MCPError(problem.get("message", "MCP request failed"), code=problem.get("code"), data=problem.get("data"))
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP response must contain an object result")
        if result.get("resultType") not in {"complete", "input_required"}:
            raise MCPError("Unsupported or absent MCP resultType")
        return result

    def list_tools(self) -> list[dict]:
        specs, cursors, cursor = {}, set(), None
        while True:
            result = self.call("tools/list", {"cursor": cursor} if cursor else {})
            for spec in result.get("tools", []):
                if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not isinstance(spec.get("inputSchema"), dict):
                    raise MCPError("Invalid MCP tool definition")
                try:
                    header_bindings(spec["inputSchema"])
                except MCPError:
                    logging.getLogger(__name__).warning("Ignoring tool %s with invalid x-mcp-header schema", spec["name"])
                    continue
                if spec["name"] in specs:
                    raise MCPError("Duplicate MCP tool name across discovery pages")
                specs[spec["name"]] = spec
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise MCPError("MCP tool pagination cursor repeats or is invalid")
            cursors.add(cursor)
        self._tools = specs
        return list(specs.values())

    def call_tool(self, name: str, arguments: dict, context: dict | None = None) -> dict:
        params = {"name": name, "arguments": arguments}
        if context:
            params["_meta"] = {CONTEXT_PREFIX + key: value for key, value in context.items()}
        result = self.call("tools/call", params)
        structured = result.get("structuredContent")
        if structured is None:
            content = result.get("content", [])
            text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
            try:
                structured = json.loads(text)
            except (ValueError, TypeError):
                structured = {"content": content}
        if result.get("isError"):
            problem = structured.get("error", "Remote tool failed") if isinstance(structured, dict) else str(structured)
            message = problem.get("message", "Remote tool failed") if isinstance(problem, dict) else str(problem)
            code = problem.get("code") if isinstance(problem, dict) else None
            raise MCPToolError(message, code=code, data=structured)
        if result.get("resultType") == "input_required":
            raise MCPToolError("Remote tool requires interactive input", data=result)
        return structured if isinstance(structured, dict) else {"value": structured}
