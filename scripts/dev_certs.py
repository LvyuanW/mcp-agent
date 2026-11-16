"""Generate a development CA and localhost certificate without changing OS trust."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def generate(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca = directory / "ca.pem"
    ca_key = directory / "ca-key.pem"
    cert = directory / "localhost.pem"
    key = directory / "localhost-key.pem"
    if all(path.exists() for path in (ca, ca_key, cert, key)):
        return directory
    if any(path.exists() for path in (ca, ca_key, cert, key)):
        raise ValueError("Incomplete certificate set; choose a new output directory")

    def openssl(*arguments):
        subprocess.run(["openssl", *map(str, arguments)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    old_mask = os.umask(0o077)
    try:
        openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "365", "-sha256",
                "-keyout", ca_key, "-out", ca, "-subj", "/CN=MCP Agent Development CA",
                "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        with tempfile.TemporaryDirectory() as temp:
            request = Path(temp) / "localhost.csr"
            extensions = Path(temp) / "extensions.cnf"
            extensions.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1\n"
                                  "basicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                                  "extendedKeyUsage=serverAuth\n")
            openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                    "-out", request, "-subj", "/CN=localhost")
            openssl("x509", "-req", "-in", request, "-CA", ca, "-CAkey", ca_key, "-set_serial", "1",
                    "-out", cert, "-days", "90", "-sha256", "-extfile", extensions)
    finally:
        os.umask(old_mask)
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".local" / "tls")
    args = parser.parse_args()
    location = generate(args.output)
    print(f"Development certificates are in {location}")
