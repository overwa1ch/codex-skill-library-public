"""Current declared files, withdrawn workflows and inactive plugins stay distinct."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import configure
from library import Library, LibraryError
from test_acceptance_round01 import StdioClient

ROOT = Path(__file__).resolve().parents[1]


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lib = Library(self.root / 'registry.json')
        self.lib.init()
        self.one = self.fixture('one')
        self.two = self.fixture('two', implicit=False)
        self.three = self.fixture('three', enabled=False)
        self.plugin = {'id': 'example@fixture', 'installed': True, 'enabled': True,
                       'version': '2.0.0', 'skills_complete': True,
                       'manifest_sha256': 'a' * 64,
                       'sources': [self.one['source'], self.two['source'], self.three['source']]}

    def fixture(self, name, implicit=True, enabled=True):
        path = self.root / name / 'SKILL.md'
        path.parent.mkdir()
        path.write_text('---\nname: ' + name + '\ndescription: Fixture.\n---\nOriginal\n')
        if not implicit:
            policy = path.parent / 'agents/openai.yaml'
            policy.parent.mkdir()
            policy.write_text('policy:\n  allow_implicit_invocation: false\n')
        return self.lib.register(path, 'plugin:fixture/example/skills/' + name + '/SKILL.md',
                                 scope='plugin', enabled=enabled, tags=['saved'])

    def plan(self, entry, target, plugin=None, **evidence_changes):
        evidence = {'version': 1, 'complete': True, 'errors': [], 'fallback': False,
                    'plugins': [plugin or self.plugin]}
        evidence.update(evidence_changes)
        path = self.root / 'inventory.json'
        path.write_text(json.dumps(evidence))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return {'version': 1, 'registry_sha256': hashlib.sha256(self.lib.path.read_bytes()).hexdigest(),
                'evidence': str(path), 'evidence_sha256': sha,
                'changes': [{'id': entry['id'], 'before': entry.get('source_state'),
                             'after': {'state': target, 'reason': 'Exact reviewed inventory.',
                                       'evidence_sha256': sha}}]}

    def current(self, entry):
        return self.lib.entry(self.lib.read(), entry['id'])

    def test_removed_source_preserves_preference_but_blocks_all_old_routes(self):
        client = StdioClient(self.lib.path)
        self.addCleanup(client.close)
        plugin = dict(self.plugin, sources=[self.one['source'], self.three['source']])
        self.lib.apply_source_plan(self.plan(self.two, 'removed_from_current_plugin', plugin))
        after = self.current(self.two)
        self.assertEqual({k: v for k, v in after.items() if k != 'source_state'}, self.two)
        self.assertTrue(Path(self.two['path']).is_file())  # Residual cache must not bypass the state.
        for operation in (lambda: self.lib.load(self.two['id']),
                          lambda: self.lib.load_explicit('two', self.two['source'])):
            with self.assertRaisesRegex(LibraryError, 'removed_from_current_plugin'):
                operation()
        result = client.call('tools/call', {'name': 'load_' + self.two['id'], 'arguments': {}})['result']
        self.assertTrue(result['isError'])
        self.assertEqual([e['id'] for e in self.lib.listing()], [self.one['id']])
        self.assertIn(self.two['id'], [e['id'] for e in self.lib.listing(include_inactive=True)])
        status = self.lib.status()
        self.assertEqual(status['state'], 'healthy')
        self.assertEqual(status['unavailable'], [])
        self.assertEqual(status['inactive_sources'][0]['id'], self.two['id'])
        fresh = Library(self.lib.path)
        with self.assertRaises(LibraryError):
            fresh.load(self.two['id'])

    def test_missing_current_file_stays_error_while_explicit_removal_is_historical(self):
        Path(self.two['path']).unlink()
        self.assertEqual(self.lib.status()['state'], 'degraded')
        with self.assertRaisesRegex(LibraryError, 'contradicts'):
            self.lib.apply_source_plan(self.plan(self.two, 'removed_from_current_plugin'))
        plugin = dict(self.plugin, sources=[self.one['source'], self.three['source']])
        self.lib.apply_source_plan(self.plan(self.two, 'removed_from_current_plugin', plugin))
        self.assertEqual(self.lib.status()['unavailable'], [])
        self.lib.apply_source_plan(self.plan(self.current(self.two), 'current'))
        self.assertEqual(self.lib.status()['state'], 'degraded')

    def test_incomplete_failed_and_fallback_inventory_cannot_change_any_state(self):
        for extra in ({'complete': False}, {'errors': ['remote failed']}, {'fallback': True},
                      {'fallback': None}, {'plugins': []}):
            before = self.lib.path.read_bytes()
            with self.subTest(extra=extra), self.assertRaises(LibraryError):
                self.lib.apply_source_plan(self.plan(self.one, 'plugin_inactive', **extra))
            self.assertEqual(self.lib.path.read_bytes(), before)

    def test_inactive_plugin_requires_explicit_installation_evidence(self):
        with self.assertRaisesRegex(LibraryError, 'cannot be classified inactive'):
            self.lib.apply_source_plan(self.plan(self.one, 'plugin_inactive'))
        self.lib.apply_source_plan(self.plan(self.one, 'plugin_inactive',
                                             dict(self.plugin, installed=False, enabled=False)))
        self.assertNotIn('load_' + self.one['id'], [t['name'] for t in self.lib.mcp_tools()])
        with self.assertRaisesRegex(LibraryError, 'plugin_inactive'):
            self.lib.load(self.one['id'], tool=True)

    def test_reinstall_and_repeated_import_do_not_restore_user_disabled_preferences(self):
        self.lib.apply_source_plan(self.plan(self.three, 'plugin_inactive',
                                             dict(self.plugin, enabled=False)))
        self.lib.apply_source_plan(self.plan(self.current(self.three), 'current'))
        self.lib.register(self.three['path'], self.three['source'], scope='plugin', enabled=True)
        self.assertFalse(self.current(self.three)['enabled'])
        self.assertEqual(self.current(self.three)['tags'], ['saved'])
        with self.assertRaisesRegex(LibraryError, 'disabled'):
            self.lib.load(self.three['id'])

    def test_source_plan_rejects_conflict_evidence_tampering_and_invalid_batch_atomically(self):
        plan = self.plan(self.one, 'plugin_inactive', dict(self.plugin, enabled=False))
        self.lib.edit(self.one['id'], tags=['concurrent'])
        before = self.lib.path.read_bytes()
        with self.assertRaisesRegex(LibraryError, 'Registry changed'):
            self.lib.apply_source_plan(plan)
        self.assertEqual(self.lib.path.read_bytes(), before)
        plan = self.plan(self.current(self.one), 'plugin_inactive', dict(self.plugin, enabled=False))
        Path(plan['evidence']).write_text('{}')
        with self.assertRaisesRegex(LibraryError, 'evidence changed'):
            self.lib.apply_source_plan(plan)
        plan = self.plan(self.current(self.one), 'plugin_inactive', dict(self.plugin, enabled=False))
        plan['changes'].append({'id': self.two['id'], 'before': None, 'after': {'state': 'fake'}})
        with self.assertRaises(LibraryError):
            self.lib.apply_source_plan(plan)
        self.assertEqual(self.lib.path.read_bytes(), before)

    def test_unknown_source_is_an_error_not_a_successful_historical_classification(self):
        self.lib.apply_source_plan(self.plan(self.one, 'unknown'))
        self.assertEqual(self.lib.status()['state'], 'degraded')
        self.assertEqual(self.lib.status()['unavailable'][0]['id'], self.one['id'])
        self.assertEqual(self.lib.status()['inactive_sources'], [])
        with self.assertRaisesRegex(LibraryError, 'unknown'):
            self.lib.load(self.one['id'])

    def test_configuration_binds_history_and_preserves_unrelated_changes_on_rollback(self):
        plugin = dict(self.plugin, sources=[self.one['source'], self.three['source']])
        self.lib.apply_source_plan(self.plan(self.two, 'removed_from_current_plugin', plugin))
        config = self.root / 'config.toml'
        original = b'model = "fixture"\n'
        config.write_bytes(original)
        plan = configure.make_plan(config, self.lib.path, sys.executable, ROOT / 'library.py')
        self.assertEqual([e['id'] for e in plan['sources']], [self.one['id']])
        self.assertEqual([e['id'] for e in plan['inactive_sources']], [self.two['id']])
        modified = copy.deepcopy(plan)
        modified['inactive_sources'] = []
        with self.assertRaisesRegex(LibraryError, 'historical'):
            configure.apply(modified, self.root / 'transactions')
        self.assertEqual(config.read_bytes(), original)
        applied = configure.apply(plan, self.root / 'transactions')
        config.write_bytes(b'unrelated = "preserved"\n' + config.read_bytes())
        configure.rollback(applied['transaction'])
        self.assertEqual(configure.parse(config.read_bytes()).unwrap(),
                         {'unrelated': 'preserved', 'model': 'fixture'})
        self.assertIn(b'unrelated = "preserved"\n', config.read_bytes())

    def test_partial_manifest_and_other_plugin_identity_do_not_prove_withdrawal(self):
        for plugin in (dict(self.plugin, skills_complete=False),
                       dict(self.plugin, id='example@other-marketplace'),
                       dict(self.plugin, manifest_sha256='invalid'),
                       dict(self.plugin, sources=[{}]),
                       dict(self.plugin, sources=['plugin:other/example/skills/one/SKILL.md'])):
            with self.subTest(plugin=plugin), self.assertRaises(LibraryError):
                self.lib.apply_source_plan(self.plan(self.one, 'removed_from_current_plugin', plugin))

    def test_malformed_state_and_unhashable_change_id_are_rejected(self):
        state = self.lib.read()
        state['skills'][0]['source_state'] = None
        with self.assertRaises(LibraryError):
            self.lib.validate(state)
        plan = self.plan(self.one, 'unknown')
        plan['changes'][0]['id'] = []
        with self.assertRaises(LibraryError):
            self.lib.apply_source_plan(plan)


if __name__ == '__main__':
    unittest.main()
