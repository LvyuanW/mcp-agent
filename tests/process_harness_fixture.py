"""Test-only process entry point; no workspace or session files are read locally."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.mcp import MCPClient
from client.runtime import AgentRunner


class FixtureModel:
    def __init__(self, config):
        assert config["model"] == "process-recovery-fixture"
        assert config["api_key"] == "test-only-no-model-network"
        self.config = config

    def count_tokens(self, *args):
        return 100

    def complete(self, system, messages, tools, **kwargs):
        assert messages[0]["content"] == "Execute the configured remote job exactly once."
        assert "shell" in {tool["name"] for tool in tools}
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            assert result["status"] == "completed" and result["exit_code"] == 0
            assert result["stdout"] == "process-recovered"
            return {"role": "assistant", "content": "Restored config proof: " + self.config["proof"]}
        return {"role": "assistant", "content": "Executing the remote job.", "tool_calls": [
            {"id": "durable-process-call", "name": "shell",
             "arguments": json.dumps({"command": self.config["command"], "timeout_seconds": 10})}]}


def main():
    parser = argparse.ArgumentParser()
    for name in ("endpoint", "token", "ca", "session"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    client = MCPClient(args.endpoint, args.token, args.ca)
    result = AgentRunner(client, FixtureModel, poll_interval=0.05, lease_ttl=0.8).run(args.session)
    print(json.dumps({"pid": os.getpid(), "status": result["status"],
                      "proof": result["config"]["proof"]}), flush=True)


if __name__ == "__main__":
    main()
