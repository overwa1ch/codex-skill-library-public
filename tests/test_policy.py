"""Preserve explicit invocation policy without changing enabled/placement."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from configure import apply, make_plan, rollback
from library import Library, LibraryError
from test_acceptance_round01 import StdioClient


ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = Library(self.root / "registry.json")
        self.lib.init()
        self.path = self.root / "one" / "SKILL.md"
        self.path.parent.mkdir()
        self.path.write_text("---\nname: named-workflow\ndescription: Fixture workflow.\n---\nOriginal body\n")
        self.entry = self.lib.register(self.path, "fixture:one")
        self.policy = self.path.parent / "agents" / "openai.yaml"
        self.policy.parent.mkdir()

    def explicit(self):
        self.policy.write_text("policy:\n  allow_implicit_invocation: false\n")

    def test_explicit_only_has_generic_entry_without_individual_metadata(self):
        self.explicit()
        tools = self.lib.tools()
        self.assertEqual([t["name"] for t in tools], ["load_explicit_skill"])
        serialized = json.dumps(tools)
        self.assertNotIn("named-workflow", serialized)
        self.assertNotIn(self.entry["id"], serialized)
        self.assertNotIn(str(self.path), serialized)
        loaded = self.lib.load_explicit("named-workflow")
        self.assertEqual(loaded["text"], self.path.read_text())
        self.assertTrue(loaded["enabled"])
        self.assertFalse(loaded["allow_implicit_invocation"])
        with self.assertRaisesRegex(LibraryError, "explicit request"):
            self.lib.load(self.entry["id"], tool=True)

    def test_running_server_observes_policy_change_and_rejects_stale_tool(self):
        client = StdioClient(self.lib.path)
        self.addCleanup(client.close)
        old = client.call("tools/list")["result"]["tools"][0]["name"]
        self.explicit()
        self.assertEqual(client.receive()["method"], "notifications/tools/list_changed")
        denied = client.call("tools/call", {"name": old, "arguments": {}})["result"]
        self.assertTrue(denied["isError"])
        loaded = client.call("tools/call", {"name": "load_explicit_skill",
                                          "arguments": {"name": "named-workflow"}})["result"]
        self.assertFalse(loaded["isError"])
        self.lib.edit(self.entry["id"], enabled=False)
        denied = client.call("tools/call", {"name": "load_explicit_skill",
                                          "arguments": {"name": "named-workflow"}})["result"]
        self.assertTrue(denied["isError"])
        self.assertEqual(client.call("tools/list")["result"]["tools"], [])
        restarted = StdioClient(self.lib.path)
        self.addCleanup(restarted.close)
        denied = restarted.call("tools/call", {"name": "load_explicit_skill",
                                              "arguments": {"name": "named-workflow"}})["result"]
        self.assertTrue(denied["isError"])

    def test_named_selection_rejects_unknown_and_ambiguous_sources(self):
        self.explicit()
        other = self.root / "two" / "SKILL.md"
        other.parent.mkdir()
        other.write_bytes(self.path.read_bytes())
        (other.parent / "agents").mkdir()
        (other.parent / "agents/openai.yaml").write_bytes(self.policy.read_bytes())
        second = self.lib.register(other, "fixture:two")
        with self.assertRaisesRegex(LibraryError, "Ambiguous"):
            self.lib.load_explicit("named-workflow")
        self.assertEqual(self.lib.load_explicit("named-workflow", "fixture:two")["id"], second["id"])
        for name, source in (("unknown", None), ("named-workflow", "unknown"), ("", None), ([], None)):
            with self.subTest(name=name, source=source), self.assertRaises(LibraryError):
                self.lib.load_explicit(name, source)

    def test_invalid_policy_fails_closed_disabled_source_remains_manageable(self):
        for content in ("policy: [false]\n", "policy:\n  allow_implicit_invocation: 'false'\n",
                        "[broken", "", "policy: null\n"):
            with self.subTest(content=content):
                self.policy.write_text(content)
                with self.assertRaises(LibraryError):
                    self.lib.tools()
                with self.assertRaises(LibraryError):
                    self.lib.load(self.entry["id"], tool=True)
        self.policy.unlink()
        self.policy.symlink_to(self.root / "missing-policy")
        with self.assertRaises(LibraryError):
            self.lib.tools()
        self.lib.edit(self.entry["id"], enabled=False)
        self.assertEqual(self.lib.tools(), [])
        self.assertFalse(self.lib.listing(include_disabled=True)[0]["enabled"])

    def test_default_and_global_policy_do_not_add_explicit_entry(self):
        self.assertEqual(len(self.lib.tools()), 1)
        self.policy.write_text("interface:\n  display_name: Example\n")
        self.assertTrue(self.lib.load(self.entry["id"])["allow_implicit_invocation"])
        self.explicit()
        self.lib.edit(self.entry["id"], placement="global")
        self.assertEqual(self.lib.tools(), [])
        with self.assertRaises(LibraryError):
            self.lib.load_explicit("named-workflow")

    def test_policy_changes_invalidate_plan_and_old_transaction_rolls_back(self):
        config = self.root / "config.toml"
        config.write_bytes(b'model = "fixture"\n')
        original = config.read_bytes()
        def plan():
            return make_plan(config, self.lib.path, sys.executable, ROOT / "library.py")
        before_add = plan()
        self.explicit()
        with self.assertRaisesRegex(LibraryError, "policy changed"):
            apply(before_add, self.root / "transactions")
        before_remove = plan()
        self.policy.unlink()
        with self.assertRaisesRegex(LibraryError, "policy changed"):
            apply(before_remove, self.root / "transactions")
        self.assertEqual(config.read_bytes(), original)
        result = apply(plan(), self.root / "transactions")
        record = Path(result["transaction"]) / "transaction.json"
        doc = json.loads(record.read_text())
        for source in doc["sources"]:
            source.pop("policy_sha256")
        record.write_text(json.dumps(doc))
        rollback(result["transaction"])
        self.assertEqual(config.read_bytes(), original)

    def test_explicit_tool_rejects_malformed_arguments_without_stopping_server(self):
        self.explicit()
        client = StdioClient(self.lib.path)
        self.addCleanup(client.close)
        for args in ({}, [], None, {"name": 1}, {"name": "named-workflow", "source": []},
                     {"name": "named-workflow", "extra": "unrecognized"}):
            with self.subTest(args=args):
                result = client.call("tools/call", {"name": "load_explicit_skill",
                                                   "arguments": args})["result"]
                self.assertTrue(result["isError"])
        self.assertEqual(client.call("ping")["result"], {})


if __name__ == "__main__":
    unittest.main()
