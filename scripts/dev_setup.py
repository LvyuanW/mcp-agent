"""Prepare separate HTTPS services and register a real existing workspace."""

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.dev_certs import generate
from server.auth import OAuthService
from server.workspace import WorkspaceService


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser(description="Set up local HTTPS and a developer account")
    parser.add_argument("--workspace", required=True, type=Path, help="Existing real directory to work in")
    parser.add_argument("--tenant", default="personal")
    parser.add_argument("--username", default="developer")
    parser.add_argument("--execution-user")
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        parser.error("workspace must be a directory")
    password = os.environ.get("MCP_AGENT_INIT_PASSWORD") or getpass.getpass("Developer password (12+ characters): ")
    if len(password) < 12:
        parser.error("password must contain at least 12 characters")
    certs = generate(ROOT / ".local" / "tls")
    data = ROOT / ".local" / "server"
    service = WorkspaceService(data)
    service.ensure_tenant(args.tenant, workspace, execution_user=args.execution_user)
    auth = OAuthService(data, "https://localhost:9443", ["https://localhost:8443/auth/callback"])
    auth.put_user(args.username, args.tenant, password)
    tls = (f'cert_file = {json.dumps(str(certs / "localhost.pem"))}\n'
           f'key_file = {json.dumps(str(certs / "localhost-key.pem"))}\n')
    server_config = ROOT / "server" / "config.local.toml"
    client_config = ROOT / "client" / "config.local.toml"
    if not server_config.exists():
        write_private(server_config, 'host = "127.0.0.1"\nport = 9443\npublic_url = "https://localhost:9443"\n'
                      f'data_dir = {json.dumps(str(data))}\nredirect_uris = ["https://localhost:8443/auth/callback"]\n' + tls)
    if not client_config.exists():
        write_private(client_config, 'host = "127.0.0.1"\nport = 8443\npublic_url = "https://localhost:8443"\n'
                      'mcp_url = "https://localhost:9443/mcp"\n'
                      f'ca_file = {json.dumps(str(certs / "ca.pem"))}\n'
                      f'cookie_secret = "{secrets.token_urlsafe(48)}"\n' + tls)
    print(f"Configured tenant {args.tenant} for {workspace}")
    print("Start MCP service: cd server && python3 app.py")
    print("Start web service: cd client && python3 app.py")
    print("Open https://localhost:8443 after trusting the development CA in your browser.")
    print(f"Development CA: {certs / 'ca.pem'}")


if __name__ == "__main__":
    main()
