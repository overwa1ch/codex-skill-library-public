import json
from pathlib import Path
import sys
import tempfile
import unittest
import tomlkit

from configure import make_plan, apply, rollback, SERVER
from library import Library, LibraryError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.toml"
        self.original = b'# preserved\nmodel = "unchanged"\n[mcp_servers.existing]\nurl = "https://example.test/mcp"\n[plugins."existing@test"]\nenabled = true\n'
        self.config.write_bytes(self.original)
        self.registry = self.root / "registry.json"
        self.lib = Library(self.registry)
        self.lib.init()
        path = self.root / "SKILL.md"
        path.write_text("---\nname: example\ndescription: Example task.\n---\nExample\n")
        self.entry = self.lib.register(path, "test")
        self.program = Path(__file__).resolve().parents[1] / "library.py"

    def plan(self):
        return make_plan(self.config, self.registry, sys.executable, self.program)

    def apply(self):
        return apply(self.plan(), self.root / "transactions")

    def test_apply_only_owned_targets_and_byte_exact_rollback(self):
        p = self.plan()
        self.assertEqual(p["changes"][0]["after"]["command"], sys.executable)
        result = self.apply()
        new = tomlkit.parse(self.config.read_text()).unwrap()
        old = tomlkit.parse(self.original.decode()).unwrap()
        self.assertEqual(new["plugins"], old["plugins"])
        self.assertEqual(new["mcp_servers"]["existing"], old["mcp_servers"]["existing"])
        self.assertEqual(new["skills"]["config"][0]["enabled"], False)
        self.assertEqual(self.plan()["changes"], [])
        self.assertFalse(self.apply()["changed"])
        rollback(result["transaction"])
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(rollback(result["transaction"])["status"], "already_rolled_back")

    def test_preserves_unrelated_concurrent_edits(self):
        result = self.apply()
        doc = tomlkit.parse(self.config.read_text())
        doc["model"] = "new-external-model"
        doc["plugins"]["other@test"] = {"enabled": True}
        self.config.write_text(tomlkit.dumps(doc))
        rollback(result["transaction"])
        final = tomlkit.parse(self.config.read_text()).unwrap()
        self.assertEqual(final["model"], "new-external-model")
        self.assertTrue(final["plugins"]["other@test"]["enabled"])
        self.assertNotIn(SERVER, final["mcp_servers"])
        self.assertNotIn("skills", final)

    def test_conflicting_target_blocks_entire_rollback(self):
        result = self.apply()
        doc = tomlkit.parse(self.config.read_text())
        doc["skills"]["config"][0]["enabled"] = True
        self.config.write_text(tomlkit.dumps(doc))
        before = self.config.read_bytes()
        with self.assertRaisesRegex(LibraryError, "conflict"):
            rollback(result["transaction"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_stale_plan_and_registry_refused(self):
        p = self.plan()
        self.config.write_bytes(self.original + b"\n# concurrent change\n")
        with self.assertRaisesRegex(LibraryError, "Configuration changed"):
            apply(p, self.root / "transactions")
        p = self.plan()
        self.lib.edit(self.entry["id"], tags=["changed"])
        with self.assertRaisesRegex(LibraryError, "Registry changed"):
            apply(p, self.root / "transactions")

    def test_existing_disabled_stays_disabled(self):
        doc = tomlkit.parse(self.original.decode())
        doc["skills"] = {"config": [{"path": self.entry["path"], "enabled": False}]}
        self.config.write_text(tomlkit.dumps(doc))
        self.lib.edit(self.entry["id"], enabled=False)
        p = self.plan()
        self.assertEqual(len(p["changes"]), 1)
        self.assertEqual(p["changes"][0]["kind"], "server")

    def test_conflicting_server_refused(self):
        doc = tomlkit.parse(self.original.decode())
        doc["mcp_servers"][SERVER] = {"command": "/unrelated/service"}
        self.config.write_text(tomlkit.dumps(doc))
        with self.assertRaisesRegex(LibraryError, "differs"):
            self.plan()

    def test_create_config_then_rollback(self):
        self.config.unlink()
        result = self.apply()
        self.assertTrue(self.config.exists())
        rollback(result["transaction"])
        self.assertFalse(self.config.exists())

    def test_global_system_not_disabled(self):
        self.lib.edit(self.entry["id"], placement="global")
        p = self.plan()
        self.assertEqual(len(p["changes"]), 1)
        self.assertEqual(p["changes"][0]["kind"], "server")

    def test_duplicate_native_config_is_rejected(self):
        doc = tomlkit.parse(self.original.decode())
        doc["skills"] = {"config": [{"path": self.entry["path"], "enabled": True},
                                    {"path": self.entry["path"], "enabled": False}]}
        self.config.write_text(tomlkit.dumps(doc))
        with self.assertRaisesRegex(LibraryError, "Duplicate"):
            self.plan()

    def test_subset_plan_checks_every_skill_the_server_serves(self):
        other = self.root / "other" / "SKILL.md"
        other.parent.mkdir()
        other.write_text("---\nname: other\ndescription: Other workflow.\n---\nBody\n")
        self.lib.register(other, "other")
        p = make_plan(self.config, self.registry, sys.executable, self.program, [self.entry["id"]])
        self.assertEqual(len(p["sources"]), 2)
        other.unlink()
        with self.assertRaises(LibraryError):
            apply(p, self.root / "transactions")
        self.assertEqual(self.config.read_bytes(), self.original)
        with self.assertRaises(LibraryError):
            make_plan(self.config, self.registry, sys.executable, self.program, [self.entry["id"]])
