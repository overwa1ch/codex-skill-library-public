"""A missing plugin version must not hide healthy UUID loading routes."""
import json
from pathlib import Path
import tempfile
import unittest

from library import Library, LibraryError
from test_acceptance_round01 import StdioClient

ROOT = Path(__file__).resolve().parents[1]


class CatalogAvailabilityTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="catalog-availability-", dir=ROOT / "tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = Library(self.root / "registry.json")
        self.lib.init()
        self.good, self.good_path = self.fixture("good", implicit=True)
        self.explicit, self.explicit_path = self.fixture("explicit", implicit=False)
        self.other, self.other_path = self.fixture("other", implicit=False)
        self.client = StdioClient(self.lib.path)
        self.addCleanup(self.client.close)

    def fixture(self, name, implicit):
        path = self.root / name / "SKILL.md"
        path.parent.mkdir()
        path.write_text(f"---\nname: {name}\ndescription: Original workflow.\n---\n{name} body\n")
        if not implicit:
            policy = path.parent / "agents/openai.yaml"
            policy.parent.mkdir()
            policy.write_text("policy:\n  allow_implicit_invocation: false\n")
        return self.lib.register(path, "fixture:" + name), path

    def names(self):
        return [x["name"] for x in self.client.call("tools/list")["result"]["tools"]]

    def call(self, name, args=None):
        return self.client.call("tools/call", {"name": name, "arguments": args or {}})["result"]

    def test_missing_source_keeps_healthy_uuid_routes_and_reports_live_error(self):
        self.explicit_path.unlink()
        self.assertEqual(self.names(), ["load_" + self.good["id"], "load_explicit_skill", "library_status"])
        result = self.call("load_" + self.good["id"])
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["text"], self.good_path.read_text())
        status = json.loads(self.call("library_status")["content"][0]["text"])
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["unavailable"][0]["id"], self.explicit["id"])
        self.assertEqual(status["unavailable"][0]["path"], str(self.explicit_path))
        self.assertTrue(status["explicit_loading_available"])
        self.assertTrue(status["explicit_source_required"])
        self.assertTrue(self.call("load_" + self.explicit["id"])["isError"])
        # The unreadable entry might have a colliding alias; fail closed by name.
        self.assertTrue(self.call("load_explicit_skill", {"name": "other"})["isError"])
        narrowed = self.call("load_explicit_skill", {"name": "other", "source": self.other["source"]})
        self.assertFalse(narrowed["isError"])
        self.assertTrue(self.call("load_explicit_skill", {
            "name": "explicit", "source": self.explicit["source"],
        })["isError"])
        with self.assertRaises(LibraryError):
            self.lib.tools()  # Strict administrative inventory still refuses gaps.

    def test_live_rebind_recovers_same_id_and_explicit_route_without_restart(self):
        self.assertIn("load_explicit_skill", self.names())
        self.explicit_path.unlink()
        self.assertEqual(self.client.receive()["method"], "notifications/tools/list_changed")
        self.assertIn("library_status", self.names())
        new = self.root / "new-version/SKILL.md"
        new.parent.mkdir()
        new.write_text("---\nname: explicit\ndescription: Updated workflow.\n---\nnew original body\n")
        policy = new.parent / "agents/openai.yaml"
        policy.parent.mkdir()
        policy.write_text("policy:\n  allow_implicit_invocation: false\n")
        self.lib.edit(self.explicit["id"], path=new)
        self.assertEqual(self.client.receive()["method"], "notifications/tools/list_changed")
        self.assertEqual(self.names(), ["load_" + self.good["id"], "load_explicit_skill"])
        loaded = json.loads(self.call("load_explicit_skill", {"name": "explicit"})["content"][0]["text"])
        self.assertEqual(loaded["id"], self.explicit["id"])
        self.assertEqual(loaded["path"], str(new))
        self.assertEqual(loaded["text"], new.read_text())

    def test_invalid_policy_does_not_reenable_implicit_or_name_selection(self):
        (self.explicit_path.parent / "agents/openai.yaml").write_text("policy: [invalid]\n")
        self.assertEqual(self.names(), ["load_" + self.good["id"], "load_explicit_skill", "library_status"])
        self.assertTrue(self.call("load_" + self.explicit["id"])["isError"])
        self.assertTrue(self.call("load_explicit_skill", {"name": "other"})["isError"])
        self.assertEqual(self.client.call("ping")["result"], {})

    def test_disabled_and_global_failures_do_not_degrade_library(self):
        self.lib.edit(self.explicit["id"], enabled=False)
        self.lib.edit(self.other["id"], placement="global")
        self.explicit_path.unlink()
        self.other_path.unlink()
        self.assertEqual(self.names(), ["load_" + self.good["id"]])
        self.assertEqual(self.lib.status()["unavailable"], [])

    def test_corrupt_registry_and_all_unavailable_remain_explicit_errors(self):
        for path in (self.good_path, self.explicit_path, self.other_path):
            path.unlink()
        self.assertIn("error", self.client.call("tools/list"))
        self.assertEqual(self.lib.status()["state"], "degraded")
        self.lib.path.write_text("{broken")
        self.assertIn("error", self.client.call("tools/list"))
        self.assertTrue(self.call("library_status")["isError"])
        self.assertEqual(self.client.call("ping")["result"], {})

    def test_status_has_no_parameters_and_does_not_execute_workflows(self):
        self.explicit_path.unlink()
        before = self.good_path.read_bytes()
        self.assertTrue(self.call("library_status", {"repair": True})["isError"])
        self.assertFalse(self.call("library_status")["isError"])
        self.assertEqual(self.good_path.read_bytes(), before)
