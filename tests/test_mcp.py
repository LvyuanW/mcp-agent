import io
import json
from email.message import Message
import ssl
import unittest
from urllib.error import HTTPError

from client.mcp import MCPClient, MCPError, MCPToolError, MCPTransportError, encode_header, header_bindings, sse_messages


class Response(io.BytesIO):
    def __init__(self, payload, content_type="application/json"):
        super().__init__(payload)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


class Opener:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def open(self, req, timeout):
        self.requests.append(req)
        return self.responder(req)


def result(req, value):
    identifier = json.loads(req.data)["id"]
    return Response(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                "result": {"resultType": "complete", **value}}).encode())


class MCPTests(unittest.TestCase):
    def client(self, responder):
        client = MCPClient("https://example.test/mcp", "test-access-token")
        client.opener = Opener(responder)
        return client

    def test_verified_https_is_required(self):
        with self.assertRaises(ValueError):
            MCPClient("http://localhost/mcp", "secret")
        client = MCPClient("https://localhost/mcp", "secret")
        self.assertEqual(client.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.context.check_hostname)

    def test_http_error_response_is_closed_after_decoding(self):
        body = io.BytesIO(b'{"error":{"code":"forbidden","message":"Scope required"}}')
        failure = HTTPError("https://example.test/mcp", 403, "Forbidden", {}, body)

        def responder(req):
            raise failure

        client = self.client(responder)
        with self.assertRaisesRegex(MCPError, "Scope required") as caught:
            client.call("tools/list")
        self.assertEqual(caught.exception.status, 403)
        self.assertTrue(body.closed)

    def test_modern_headers_metadata_and_tool_result(self):
        client = self.client(lambda req: result(req, {"structuredContent": {"ok": True}}))
        self.assertEqual(client.call_tool("保存", {"text": "你好"}, {"operation_id": "operation-1"}), {"ok": True})
        req = client.opener.requests[0]
        headers = dict((key.lower(), value) for key, value in req.header_items())
        body = json.loads(req.data)
        self.assertEqual(headers["authorization"], "Bearer test-access-token")
        self.assertEqual(headers["mcp-method"], "tools/call")
        self.assertEqual(headers["mcp-name"], encode_header("保存"))
        self.assertEqual(headers["accept"], "application/json, text/event-stream")
        self.assertEqual(body["params"]["_meta"]["io.mcp-agent/operation_id"], "operation-1")
        self.assertEqual(body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"], "2026-07-28")

    def test_sse_notification_then_matching_result(self):
        notifications = []

        def responder(req):
            identifier = json.loads(req.data)["id"]
            stream = ': heartbeat\r\n\r\nevent: message\ndata: {"jsonrpc":"2.0",\ndata: "method":"notifications/progress","params":{}}\n\n'
            stream += 'data: ' + json.dumps({"jsonrpc": "2.0", "id": identifier,
                                            "result": {"resultType": "complete", "tools": []}}) + '\n\n'
            return Response(stream.encode(), "text/event-stream")

        client = self.client(responder)
        client.on_notification = notifications.append
        self.assertEqual(client.list_tools(), [])
        self.assertEqual(notifications[0]["method"], "notifications/progress")

    def test_incomplete_sse_does_not_accept_partial_result(self):
        client = self.client(lambda req: Response(b'data: {"jsonrpc":"2.0"}', "text/event-stream"))
        with self.assertRaises(MCPTransportError):
            client.call("tools/list")

    def test_response_id_must_match(self):
        client = self.client(lambda req: Response(b'{"jsonrpc":"2.0","id":"wrong","result":{"resultType":"complete"}}'))
        with self.assertRaisesRegex(MCPError, "ID"):
            client.call("tools/list")

    def test_paginated_tools_and_nested_mirrored_header(self):
        schema = {"type": "object", "properties": {"options": {"type": "object", "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"}}}}}

        def responder(req):
            body = json.loads(req.data)
            if body["method"] == "tools/call":
                return result(req, {"structuredContent": {"ok": True}})
            if body["params"].get("cursor"):
                return result(req, {"tools": [{"name": "second", "inputSchema": {"type": "object"}}]})
            return result(req, {"tools": [{"name": "first", "inputSchema": schema}], "nextCursor": "page-2"})

        client = self.client(responder)
        self.assertEqual([tool["name"] for tool in client.list_tools()], ["first", "second"])
        client.call_tool("first", {"options": {"region": "上海"}})
        headers = dict((key.lower(), value) for key, value in client.opener.requests[-1].header_items())
        self.assertEqual(headers["mcp-param-region"], encode_header("上海"))

    def test_invalid_annotation_path_rejected(self):
        with self.assertRaises(MCPError):
            header_bindings({"type": "object", "allOf": [{"properties": {
                "secret": {"type": "string", "x-mcp-header": "Unsafe"}}}]})
        with self.assertRaises(MCPError):
            header_bindings({"type": "object", "properties": {
                "count": {"type": "number", "x-mcp-header": "Count"}}})

    def test_tool_failure_preserves_structured_details(self):
        client = self.client(lambda req: result(req, {"isError": True, "structuredContent": {"error": "lease conflict"}}))
        with self.assertRaisesRegex(MCPToolError, "lease conflict") as caught:
            client.call_tool("session_claim", {})
        self.assertEqual(caught.exception.data, {"error": "lease conflict"})

    def test_domain_failure_exposes_code_and_human_message(self):
        detail = {"error": {"code": "lease_conflict", "message": "Another runner owns this session"}}
        client = self.client(lambda req: result(req, {"isError": True, "structuredContent": detail}))
        with self.assertRaisesRegex(MCPToolError, "Another runner") as caught:
            client.call_tool("shell", {"command": "pwd"})
        self.assertEqual(caught.exception.code, "lease_conflict")
        self.assertEqual(caught.exception.data, detail)

    def test_header_sentinel_is_unambiguous(self):
        self.assertEqual(encode_header("ordinary"), "ordinary")
        self.assertTrue(encode_header(" padded ").startswith("=?base64?"))
        self.assertNotEqual(encode_header("=?base64?literal?="), "=?base64?literal?=")


if __name__ == "__main__":
    unittest.main()
