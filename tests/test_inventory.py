import json
from pathlib import Path
import tempfile
import unittest
import tomlkit

from library import Library, LibraryError
from tools.inventory_manifest import convert, source_identity


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = Library(self.root / "registry.json")
        self.lib.init()

    def entry(self, name, scope="user", enabled=True):
        path = self.root / name / "SKILL.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("---\nname: same-name\ndescription: Example workflow.\n---\nBody\n")
        return {"path": str(path), "source": scope + ":" + name, "scope": scope,
                "enabled": enabled, "tags": []}

    def test_bulk_atomic_and_manual_state_survives(self):
        one, two = self.entry("one", "system"), self.entry("two")
        imported = self.lib.import_entries([one, two])
        self.assertEqual(imported[0]["placement"], "global")
        self.lib.edit(imported[1]["id"], enabled=False, tags=["chosen"])
        again = self.lib.import_entries([one, two])
        self.assertEqual(again[1]["id"], imported[1]["id"])
        self.assertFalse(again[1]["enabled"])
        self.assertEqual(again[1]["tags"], ["chosen"])
        before = self.lib.path.read_bytes()
        third = self.entry("three")
        conflict = self.entry("new-one")
        conflict["source"] = one["source"]
        with self.assertRaisesRegex(LibraryError, "rebind"):
            self.lib.import_entries([third, conflict])
        self.assertEqual(self.lib.path.read_bytes(), before)

    def test_disabled_missing_tombstone_and_invalid_batch(self):
        disabled = self.entry("removed", enabled=False)
        Path(disabled["path"]).unlink()
        self.lib.import_entries([disabled])
        self.assertEqual(self.lib.tools(), [])
        before = self.lib.path.read_bytes()
        invalid = self.entry("invalid")
        invalid["enabled"] = "false"
        with self.assertRaises(LibraryError):
            self.lib.import_entries([invalid])
        self.assertEqual(self.lib.path.read_bytes(), before)

    def test_host_disabled_precedence_and_incomplete_desktop_label(self):
        one = self.entry("one")
        doc = tomlkit.parse("")
        doc["skills"] = {"config": [{"path": one["path"], "enabled": False},
                                    {"path": str(self.root / "missing/SKILL.md"), "enabled": False}]}
        snapshot = {"surface": "standalone-app-server-not-desktop",
                    "skills": {"data": [{"cwd": str(self.root), "skills": [one], "errors": []}]}}
        manifest = convert(snapshot, doc, "test-hash")
        self.assertFalse(manifest["skills"][0]["enabled"])
        self.assertEqual(len(manifest["unresolved_disabled_paths"]), 1)
        self.assertEqual(manifest["desktop_completeness"], "not-established")
        snapshot["skills"]["data"][0]["errors"] = [{"message": "partial scan"}]
        with self.assertRaises(LibraryError):
            convert(snapshot, doc, "hash")

    def test_plugin_version_not_in_identity_and_unknown_layout_refused(self):
        a = Path("/example/.codex/plugins/cache/vendor/plugin/1.0/skills/edit/SKILL.md")
        b = Path(str(a).replace("/1.0/", "/2.0/"))
        self.assertEqual(source_identity(a, "plugin"), source_identity(b, "plugin"))
        with self.assertRaises(LibraryError):
            source_identity(Path("/unknown/SKILL.md"), "plugin")
