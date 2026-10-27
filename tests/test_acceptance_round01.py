"""Independent acceptance cases for candidate 26d1101.

Only synthetic skills/configuration under this worktree's ignored tmp/ are used.
The failing assertions describe required behavior, not accepted current behavior.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

import tomlkit

import configure
import library
from library import Library, LibraryError


ROOT = Path(__file__).resolve().parents[1]


class StdioClient:
    def __init__(self, registry):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "library.py"), "--registry", str(registry), "serve"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.buffer = b""
        self.serial = 0
        self.notifications = []
        self.started = time.monotonic()
        self.call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "independent-acceptance", "version": "1"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, request):
        self.proc.stdin.write((json.dumps(request) + "\n").encode())
        self.proc.stdin.flush()

    def receive(self, timeout=3):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                raise AssertionError("MCP response/notification timed out")
            data = os.read(self.proc.stdout.fileno(), 65536)
            if not data:
                self.proc.wait(timeout=3)
                raise AssertionError(
                    f"MCP process exited {self.proc.returncode}: "
                    + self.proc.stderr.read().decode()[-1200:]
                )
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def call(self, method, params=None):
        self.serial += 1
        request_id = self.serial
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": {} if params is None else params})
        while True:
            response = self.receive()
            if response.get("id") == request_id:
                return response
            self.notifications.append(response)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=3)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


class IndependentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="acceptance01-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / "registry.json"
        self.lib = Library(self.registry)
        self.lib.init()
        self.skill = self.fixture("中文 skill")
        self.entry = self.lib.register(self.skill, "personal:independent")
        self.config = self.root / "config.toml"
        self.original = b'# fixture only\nmodel = "fixture-model"\n[mcp_servers.other]\ncommand = "/bin/false"\n'
        self.config.write_bytes(self.original)

    def fixture(self, dirname, name="same-name", body="original fixture"):
        root = self.root / dirname
        root.mkdir(parents=True)
        (root / "references").mkdir()
        (root / "references" / "nested.txt").write_text("附件内容", encoding="utf-8")
        path = root / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: Synthetic acceptance workflow.\n---\n"
            + body + "\nRead references/nested.txt.\n",
            encoding="utf-8",
        )
        return path

    def plan(self):
        return configure.make_plan(
            self.config, self.registry, sys.executable, ROOT / "library.py",
        )

    def client(self):
        client = StdioClient(self.registry)
        self.addCleanup(client.close)
        return client

    def test_imports_are_bound_to_independent_worktree(self):
        self.assertEqual(Path(library.__file__).resolve(), ROOT / "library.py")
        self.assertEqual(Path(configure.__file__).resolve(), ROOT / "configure.py")

    def test_running_server_rejects_disabled_old_tool_after_explicit_version_rebind(self):
        client = self.client()
        name = client.call("tools/list")["result"]["tools"][0]["name"]
        second = self.fixture("plugin version 2", body="new version")
        self.lib.edit(self.entry["id"], enabled=False, tags=["preserved"])
        self.lib.edit(self.entry["id"], path=second)
        denied = client.call("tools/call", {"name": name, "arguments": {}})["result"]
        self.assertTrue(denied["isError"])
        self.assertEqual(client.call("tools/list")["result"]["tools"], [])
        with self.assertRaises(LibraryError):
            Library(self.registry).load(self.entry["id"])
        self.lib.edit(self.entry["id"], enabled=True)
        loaded = client.call("tools/call", {"name": name, "arguments": {}})["result"]
        value = json.loads(loaded["content"][0]["text"])
        self.assertEqual(value["path"], str(second))
        self.assertEqual(value["tags"], ["preserved"])
        self.assertIn("new version", value["text"])

    def test_first_disable_after_list_notifies_connected_client(self):
        client = self.client()
        self.assertEqual(len(client.call("tools/list")["result"]["tools"]), 1)
        self.assertLess(time.monotonic() - client.started, 0.8,
                        "Fixture did not reach the first polling interval; rerun the case")
        self.lib.edit(self.entry["id"], enabled=False)
        deadline = time.monotonic() + 2.4
        while time.monotonic() < deadline:
            event = client.receive(deadline - time.monotonic())
            if event.get("method") == "notifications/tools/list_changed":
                self.assertEqual(client.call("tools/list")["result"]["tools"], [])
                return
        self.fail("First mutation after initialized/tools/list was never announced")

    def test_restarted_server_rejects_previously_discovered_disabled_tool(self):
        first = self.client()
        name = first.call("tools/list")["result"]["tools"][0]["name"]
        self.lib.edit(self.entry["id"], enabled=False)
        first.close()
        second = self.client()
        self.assertEqual(second.call("tools/list")["result"]["tools"], [])
        denied = second.call("tools/call", {"name": name, "arguments": {}})["result"]
        self.assertTrue(denied["isError"])
        self.assertIn("disabled", denied["content"][0]["text"])

    def test_bad_registry_is_an_error_and_does_not_kill_running_server(self):
        client = self.client()
        saved = self.registry.read_bytes()
        self.registry.write_text("{broken", encoding="utf-8")
        response = client.call("tools/list")
        self.assertIn("error", response)
        self.registry.write_bytes(saved)
        self.assertEqual(client.call("ping")["result"], {})
        self.assertEqual(len(client.call("tools/list")["result"]["tools"]), 1)

    def test_symlink_loop_is_an_error_and_does_not_kill_running_server(self):
        client = self.client()
        original = self.skill.read_bytes()
        self.skill.unlink()
        self.skill.symlink_to("SKILL.md")
        response = client.call("tools/list")
        self.assertIn("error", response)
        self.assertEqual(client.call("ping")["result"], {})
        self.skill.unlink()
        self.skill.write_bytes(original)
        self.assertEqual(len(client.call("tools/list")["result"]["tools"]), 1)

    def test_malformed_protocol_request_is_bounded_and_service_recovers(self):
        client = self.client()
        client.proc.stdin.write(b"{not-json\n")
        client.proc.stdin.flush()
        self.assertIn("error", client.receive())
        self.assertEqual(client.call("ping")["result"], {})
        response = client.call("tools/call", {
            "name": "load_" + self.entry["id"], "arguments": {"unexpected": True},
        })
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(client.call("ping")["result"], {})

    def test_disabled_broken_skill_does_not_block_healthy_skill(self):
        healthy_path = self.fixture("healthy")
        healthy = self.lib.register(healthy_path, "plugin:healthy:workflow")
        self.lib.edit(self.entry["id"], enabled=False)
        self.skill.unlink()
        self.assertEqual([t["name"] for t in self.lib.tools()], ["load_" + healthy["id"]])
        with self.assertRaisesRegex(LibraryError, "disabled"):
            self.lib.load(self.entry["id"])

    def test_loading_does_not_execute_body_and_attachment_base_is_original(self):
        marker = self.root / "must-not-exist"
        self.skill.write_text(
            self.skill.read_text() + f"\nExecute: touch '{marker}'\n", encoding="utf-8",
        )
        before = self.skill.read_bytes()
        loaded = self.lib.load(self.entry["id"])
        self.assertFalse(marker.exists())
        self.assertEqual(loaded["text"], before.decode())
        self.assertEqual(Path(loaded["skill_root"]), self.skill.parent)
        self.assertEqual(
            (Path(loaded["relative_path_base"]) / "references" / "nested.txt").read_text(),
            "附件内容",
        )
        self.assertEqual(before, self.skill.read_bytes())

    def test_same_name_rebind_never_overwrites_other_identity(self):
        other_path = self.fixture("another source")
        other = self.lib.register(other_path, "plugin:other:same-name")
        before = self.registry.read_bytes()
        with self.assertRaises(LibraryError):
            self.lib.edit(self.entry["id"], path=other_path)
        self.assertEqual(before, self.registry.read_bytes())
        self.assertEqual(self.lib.load(other["id"])["path"], str(other_path))

    def test_apply_revalidates_deleted_source_after_plan(self):
        plan = self.plan()
        self.skill.unlink()
        before = self.config.read_bytes()
        try:
            result = configure.apply(plan, self.root / "transactions")
        except LibraryError:
            self.assertEqual(self.config.read_bytes(), before)
        else:
            self.fail(f"Apply accepted missing source and changed configuration: {result['changed']}")

    def test_apply_revalidates_invalid_frontmatter_after_plan(self):
        plan = self.plan()
        self.skill.write_text("no frontmatter anymore", encoding="utf-8")
        before = self.config.read_bytes()
        try:
            result = configure.apply(plan, self.root / "transactions")
        except LibraryError:
            self.assertEqual(self.config.read_bytes(), before)
        else:
            self.fail(f"Apply accepted invalid source and changed configuration: {result['changed']}")

    def test_apply_revalidates_missing_runtime_after_plan(self):
        program = self.root / "temporary_loader.py"
        program.write_bytes((ROOT / "library.py").read_bytes())
        plan = configure.make_plan(
            self.config, self.registry, sys.executable, program,
        )
        program.unlink()
        before = self.config.read_bytes()
        try:
            result = configure.apply(plan, self.root / "transactions")
        except LibraryError:
            self.assertEqual(self.config.read_bytes(), before)
        else:
            self.fail(f"Apply installed missing loader and changed configuration: {result['changed']}")

    def test_rollback_preserves_concurrent_unrelated_native_override(self):
        result = configure.apply(self.plan(), self.root / "transactions")
        doc = tomlkit.parse(self.config.read_text())
        doc["skills"]["config"].append({"path": str(self.root / "unrelated" / "SKILL.md"),
                                        "enabled": False})
        doc["mcp_servers"]["other"]["command"] = "/bin/true"
        self.config.write_text(tomlkit.dumps(doc), encoding="utf-8")
        configure.rollback(result["transaction"])
        after = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(after["skills"]["config"], [
            {"path": str(self.root / "unrelated" / "SKILL.md"), "enabled": False},
        ])
        self.assertEqual(after["mcp_servers"]["other"]["command"], "/bin/true")
        self.assertNotIn(configure.SERVER, after["mcp_servers"])

    def test_plan_cli_invalid_table_shapes_return_structured_errors(self):
        for text in ("skills = 1\n", "mcp_servers = 1\n"):
            with self.subTest(config=text.strip()):
                self.config.write_text(text, encoding="utf-8")
                result = subprocess.run([
                    sys.executable, str(ROOT / "configure.py"), "plan",
                    "--config", str(self.config), "--registry", str(self.registry),
                    "--output", str(self.root / "plan.json"),
                ], cwd=ROOT, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr,
                                 "Invalid configuration escaped the CLI's structured error handler")
                self.assertIn("error", json.loads(result.stderr))
                self.assertEqual(self.config.read_text(), text)
                self.assertFalse((self.root / "plan.json").exists())

    def test_apply_cli_invalid_plan_returns_structured_error(self):
        plan_path = self.root / "invalid-plan.json"
        plan_path.write_text("{}", encoding="utf-8")
        result = subprocess.run([
            sys.executable, str(ROOT / "configure.py"), "apply",
            "--plan", str(plan_path), "--transactions", str(self.root / "transactions"),
        ], cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr,
                         "Invalid plan escaped the CLI's structured error handler")
        self.assertIn("error", json.loads(result.stderr))
        self.assertEqual(self.config.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
