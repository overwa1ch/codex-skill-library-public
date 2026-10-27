import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from library import Library, LibraryError


ROOT = Path(__file__).resolve().parents[1]


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = Library(self.root / "state" / "registry.json")
        self.lib.init()

    def fixture(self, dirname="中文 folder", name="sample", description="Use for sample workflows."):
        root = self.root / dirname
        root.mkdir(parents=True, exist_ok=True)
        (root / "references").mkdir(exist_ok=True)
        (root / "references" / "example.txt").write_text("nested attachment", encoding="utf-8")
        (root / "check.py").write_text("print('fixture-only')\n")
        skill = root / "SKILL.md"
        skill.write_text("---\nname: " + name + "\ndescription: >-\n  " + description
                         + "\n---\n# 示例\nRead references/example.txt; run check.py.\n", encoding="utf-8")
        return skill

    def register(self, **kwargs):
        return self.lib.register(self.fixture(), "personal:sample", **kwargs)

    def test_live_source_and_attachments(self):
        entry = self.register()
        p = Path(entry["path"])
        before = {str(f): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in p.parent.rglob("*") if f.is_file()}
        loaded = self.lib.load(entry["id"])
        self.assertEqual(loaded["text"], p.read_text())
        self.assertEqual(loaded["sha256"], before[str(p)])
        base = Path(loaded["relative_path_base"])
        self.assertEqual((base / "references/example.txt").read_text(), "nested attachment")
        r = subprocess.run([sys.executable, str(base / "check.py")], cwd="/",
                           text=True, capture_output=True, check=True)
        self.assertEqual(r.stdout.strip(), "fixture-only")
        after = {str(f): hashlib.sha256(f.read_bytes()).hexdigest()
                 for f in p.parent.rglob("*") if f.is_file()}
        self.assertEqual(before, after)
        p.write_text(p.read_text().replace("# 示例", "# Updated"))
        self.assertIn("# Updated", self.lib.load(entry["id"])["text"])

    def test_system_global_and_manual_override(self):
        entry = self.register(scope="system")
        self.assertEqual(self.lib.tools(), [])
        self.lib.edit(entry["id"], placement="library")
        self.assertEqual(len(self.lib.tools()), 1)

    def test_disabled_id_and_tools_stay_disabled_after_rescan_restart(self):
        entry = self.register()
        self.lib.edit(entry["id"], enabled=False)
        self.assertEqual(self.lib.tools(), [])
        with self.assertRaisesRegex(LibraryError, "disabled"):
            self.lib.load(entry["id"])
        again = self.lib.register(entry["path"], "personal:sample")
        self.assertFalse(again["enabled"])
        self.assertEqual(again["id"], entry["id"])
        restarted = Library(self.lib.path)
        with self.assertRaises(LibraryError):
            restarted.load(entry["id"])

    def test_same_name_different_sources_and_symlink_dedup(self):
        one = self.register()
        two = self.lib.register(self.fixture("second"), "personal:other")
        self.assertNotEqual(one["id"], two["id"])
        alias = self.root / "alias"
        alias.symlink_to(Path(one["path"]).parent, target_is_directory=True)
        again = self.lib.register(alias / "SKILL.md", "personal:sample")
        self.assertEqual(again["id"], one["id"])
        with self.assertRaises(LibraryError):
            self.lib.register(one["path"], "conflicting-source")

    def test_name_and_version_rebind_keep_identity_and_disabled(self):
        entry = self.register()
        p = Path(entry["path"])
        p.write_text(p.read_text().replace("name: sample", "name: renamed"))
        self.assertEqual(self.lib.load(entry["id"])["name"], "renamed")
        self.lib.edit(entry["id"], enabled=False, tags=["media"])
        new = self.fixture("new-version", name="renamed")
        with self.assertRaisesRegex(LibraryError, "rebind"):
            self.lib.register(new, "personal:sample")
        self.lib.edit(entry["id"], path=new)
        r = self.lib.read()["skills"][0]
        self.assertEqual(r["id"], entry["id"])
        self.assertFalse(r["enabled"])
        self.assertEqual(r["tags"], ["media"])

    def test_tags_and_independent_cwd(self):
        entry = self.register(tags=["media", "media"])
        self.assertEqual(len(self.lib.listing(tag="media")), 1)
        self.assertEqual(self.lib.listing(tag="unknown"), [])
        self.lib.edit(entry["id"], tags=["research"])
        for cwd in ("/", str(self.root)):
            r = subprocess.run([sys.executable, str(ROOT / "library.py"), "--registry",
                                str(self.lib.path), "load", entry["id"]],
                               cwd=cwd, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["id"], entry["id"])

    def test_invalid_missing_and_corrupt_sources_fail(self):
        entry = self.register()
        p = Path(entry["path"])
        p.write_text("---\nname: [invalid]\ndescription: d\n---\n")
        with self.assertRaisesRegex(LibraryError, "name"):
            self.lib.load(entry["id"])
        p.unlink()
        with self.assertRaises(LibraryError):
            self.lib.tools()
        self.lib.edit(entry["id"], enabled=False)
        self.assertEqual(self.lib.tools(), [])
        self.lib.path.write_text("{invalid")
        with self.assertRaisesRegex(LibraryError, "Unreadable"):
            self.lib.listing()

    def test_duplicate_id_rejected_without_reset(self):
        self.register()
        state = self.lib.read()
        state["skills"].append(dict(state["skills"][0]))
        raw = json.dumps(state)
        self.lib.path.write_text(raw)
        with self.assertRaisesRegex(LibraryError, "duplicate"):
            self.lib.init()
        self.assertEqual(self.lib.path.read_text(), raw)

    def test_duplicate_rebind_is_atomic(self):
        one = self.register()
        two = self.lib.register(self.fixture("second"), "other")
        before = self.lib.path.read_bytes()
        with self.assertRaises(LibraryError):
            self.lib.edit(one["id"], path=two["path"])
        self.assertEqual(self.lib.path.read_bytes(), before)

    def test_stdio_protocol_and_old_tool_rejection(self):
        entry = self.register()
        p = subprocess.Popen([sys.executable, str(ROOT / "library.py"), "--registry",
                              str(self.lib.path), "serve"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        def stop():
            p.terminate()
            p.wait(timeout=5)
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()
        self.addCleanup(stop)

        def call(i, method, params):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                      "params": params}) + "\n")
            p.stdin.flush()
            while True:
                response = json.loads(p.stdout.readline())
                if response.get("id") == i:
                    return response
        init = call(1, "initialize", {"protocolVersion": "2025-03-26",
                    "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        p.stdin.flush()
        tools = call(2, "tools/list", {})["result"]["tools"]
        self.assertEqual(len(tools), 1)
        name = tools[0]["name"]
        loaded = call(3, "tools/call", {"name": name, "arguments": {}})["result"]
        self.assertFalse(loaded["isError"])
        self.assertEqual(json.loads(loaded["content"][0]["text"])["id"], entry["id"])
        self.lib.edit(entry["id"], enabled=False)
        self.assertEqual(call(4, "tools/list", {})["result"]["tools"], [])
        denied = call(5, "tools/call", {"name": name, "arguments": {}})["result"]
        self.assertTrue(denied["isError"])
        self.assertIn("disabled", denied["content"][0]["text"])
        self.lib.edit(entry["id"], enabled=True)
        invalid = call(6, "tools/call", {"name": name, "arguments": {"id": "fake"}})["result"]
        self.assertTrue(invalid["isError"])


if __name__ == "__main__":
    unittest.main()
