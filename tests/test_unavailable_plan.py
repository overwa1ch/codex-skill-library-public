"""Unavailable-source exceptions must be explicit, bound, and reversible."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

import configure
from library import Library, LibraryError

ROOT = Path(__file__).resolve().parents[1]


class UnavailablePlanTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="unavailable-plan-", dir=ROOT / "tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = Library(self.root / "registry.json")
        self.lib.init()
        self.good, self.good_path = self.fixture("good")
        self.lost, self.lost_path = self.fixture("lost")
        self.lost_path.unlink()
        self.config = self.root / "config.toml"
        self.original = b'model = "fixture"\n[plugins.example]\nenabled = true\n'
        self.config.write_bytes(self.original)

    def fixture(self, name):
        path = self.root / name / "SKILL.md"
        path.parent.mkdir()
        path.write_text(f"---\nname: {name}\ndescription: Fixture.\n---\noriginal\n")
        return self.lib.register(path, "fixture:" + name), path

    def plan(self, **kwargs):
        return configure.make_plan(self.config, self.lib.path, sys.executable,
                                   ROOT / "library.py", **kwargs)

    def test_default_partial_selection_still_rejects_unavailable_served_source(self):
        for ids in (None, [self.good["id"]]):
            with self.assertRaises(LibraryError):
                self.plan(ids=ids)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_explicit_exception_preserves_enabled_state_and_rollback(self):
        before = self.lib.path.read_bytes()
        plan = self.plan(unavailable_ids=[self.lost["id"]])
        self.assertEqual(len(plan["unavailable_sources"]), 1)
        self.assertEqual(plan["unavailable_sources"][0]["id"], self.lost["id"])
        result = configure.apply(plan, self.root / "transactions")
        self.assertTrue(result["changed"])
        self.assertEqual(self.lib.path.read_bytes(), before)
        self.assertTrue(self.lib.entry(self.lib.read(), self.lost["id"])["enabled"])
        configure.rollback(result["transaction"])
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_recovery_after_plan_refuses_before_configuration_write(self):
        plan = self.plan(unavailable_ids=[self.lost["id"]])
        self.lost_path.write_text("---\nname: lost\ndescription: Restored.\n---\n")
        with self.assertRaisesRegex(LibraryError, "recovered"):
            configure.apply(plan, self.root / "transactions")
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_changed_failure_after_plan_is_not_an_implicit_new_exception(self):
        plan = self.plan(unavailable_ids=[self.lost["id"]])
        self.lost_path.write_text("invalid frontmatter")
        with self.assertRaisesRegex(LibraryError, "Unavailable source changed"):
            configure.apply(plan, self.root / "transactions")
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_readable_disabled_global_unknown_and_empty_healthy_set_refused(self):
        for item in (self.good["id"], "0" * 32):
            with self.subTest(item=item), self.assertRaises(LibraryError):
                self.plan(unavailable_ids=[item])
        for kwargs in ({"enabled": False}, {"enabled": True, "placement": "global"}):
            self.lib.edit(self.lost["id"], **kwargs)
            with self.assertRaises(LibraryError):
                self.plan(unavailable_ids=[self.lost["id"]])
        self.lib.edit(self.lost["id"], placement="library")
        self.good_path.unlink()
        with self.assertRaisesRegex(LibraryError, "readable Library"):
            self.plan(unavailable_ids=[self.good["id"], self.lost["id"]])

    def test_invalid_duplicate_and_overlapping_approvals_are_rejected(self):
        plan = self.plan(unavailable_ids=[self.lost["id"]])
        for change in ("duplicate", "overlap", "error", "shape"):
            tampered = copy.deepcopy(plan)
            if change == "duplicate":
                tampered["unavailable_sources"] *= 2
            elif change == "overlap":
                tampered["unavailable_sources"][0]["id"] = self.good["id"]
            elif change == "error":
                tampered["unavailable_sources"][0]["error"] = "different error"
            else:
                tampered["unavailable_sources"] = {}
            with self.subTest(change=change), self.assertRaises(LibraryError):
                configure.apply(tampered, self.root / "transactions")
        self.assertEqual(self.config.read_bytes(), self.original)
