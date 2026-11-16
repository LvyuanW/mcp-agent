import base64
import hashlib
import hmac
import json
import ssl
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


MAX_BODY = 12 * 1024 * 1024


def load_config(path, defaults):
    result = dict(defaults)
    if path:
        with Path(path).open("rb") as stream:
            result.update(tomllib.load(stream))
    return result


def https_origin(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTPS URL without embedded credentials is required")
    if parsed.query or parsed.fragment:
        raise ValueError("Service URLs must not contain query strings or fragments")
    return f"https://{parsed.netloc}"


def ssl_client_context(ca_file=None):
    return ssl.create_default_context(cafile=str(ca_file) if ca_file else None)


def serve_https(handler, host, port, cert_file, key_file):
    if not cert_file or not key_file:
        raise ValueError("TLS certificate and private key are required; see scripts/dev_certs.py")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def sign_value(value, key, ttl=600):
    payload = json.dumps({"value": value, "expires": time.time() + ttl}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_value(value, key):
    try:
        encoded, signature = value.rsplit(".", 1)
        expected = hmac.new(key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["expires"] < time.time():
            raise ValueError("Expired value")
        return payload["value"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid signed value") from error


class Handler(BaseHTTPRequestHandler):
    server_version = "mcp-agent"

    def log_message(self, format, *args):
        # Query strings can contain authorization codes. Log only method/path/status.
        import sys
        status = args[1] if len(args) > 1 else "-"
        sys.stderr.write(f"{self.command} {urlparse(self.path).path} {status}\n")

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is unsupported; send Content-Length")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request body exceeds the 12 MiB limit")
        self.connection.settimeout(30)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("Incomplete request body")
        return data

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        value = json.loads(self.body())
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def respond(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def redirect(self, location, headers=None):
        self.respond(303, b"", headers=[("Location", location), *(headers or [])])

    def fail(self, status, message, code="request_error"):
        self.respond(status, {"error": {"code": code, "message": str(message)}})

    def do_OPTIONS(self):
        self.respond(405, {"error": {"message": "Method not allowed"}}, headers=[("Allow", "GET, POST")])


def cookie_header(name, value, *, max_age=86400):
    return ("Set-Cookie", f"{name}={value}; Path=/; Max-Age={int(max_age)}; Secure; HttpOnly; SameSite=Lax")
