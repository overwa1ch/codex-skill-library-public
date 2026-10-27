#!/usr/bin/env python3
"""Read-only standalone Codex app-server inventory and MCP probe. Not Desktop acceptance."""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--mcp-server")
    args = parser.parse_args()
    command = ["codex", "app-server"]
    for value in args.config:
        command += ["-c", value]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    inbox = queue.Queue()

    def reader():
        for line in proc.stdout:
            try:
                inbox.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=reader, daemon=True).start()

    def send(data):
        proc.stdin.write(json.dumps(data) + "\n")
        proc.stdin.flush()

    def call(i, method, params):
        send({"id": i, "method": method, "params": params})
        end = time.monotonic() + 45
        while time.monotonic() < end:
            value = inbox.get(timeout=max(.01, end - time.monotonic()))
            if value.get("id") == i:
                if "error" in value:
                    raise RuntimeError(json.dumps(value["error"]))
                return value["result"]
        raise TimeoutError(method)

    try:
        init = call(1, "initialize", {"clientInfo": {"name": "skill_library_probe", "version": "0.1"},
                                     "capabilities": {"experimentalApi": True}})
        send({"method": "initialized"})
        skills = call(2, "skills/list", {"cwds": [args.cwd], "forceReload": True})
        result = {"surface": "standalone-app-server-not-desktop", "host": init, "skills": skills}
        if args.mcp_server:
            result["mcp"] = call(3, "mcpServerStatus/list",
                                 {"serverName": args.mcp_server, "detail": "toolsAndAuthOnly"})
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        items = skills["data"][0]["skills"]
        print(json.dumps({"output": args.output, "surface": result["surface"],
                          "skills": len(items), "enabled": sum(s["enabled"] for s in items),
                          "mcp_servers": len(result.get("mcp", {}).get("data", []))}))
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
