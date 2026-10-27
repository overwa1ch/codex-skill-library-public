"""Idle work stays cheap while source and policy changes still notify clients."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from library import Library
from test_acceptance_round01 import StdioClient

ROOT = Path(__file__).resolve().parents[1]


class CountingClient(StdioClient):
    def __init__(self, registry, counter, wrapper):
        self.proc = subprocess.Popen([sys.executable, '-B', str(wrapper), str(ROOT),
                                      str(registry), str(counter)], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buffer = b''
        self.serial = 0
        self.notifications = []
        self.started = time.monotonic()
        self.call('initialize', {'protocolVersion': '2025-06-18'})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})


class CatalogWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lib = Library(self.root / 'registry.json')
        self.lib.init()
        self.one, self.path = self.skill('one')
        self.two, _ = self.skill('two')  # Keep diagnostics available when one source fails.
        self.counter = self.root / 'catalog-calls.txt'
        wrapper = self.root / 'counting-server.py'
        wrapper.write_text('''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from library import Library, serve
class Counted(Library):
    count = 0
    def mcp_tools(self):
        self.count += 1
        Path(sys.argv[3]).write_text(str(self.count))
        return super().mcp_tools()
serve(Counted(sys.argv[2]))
''')
        self.wrapper = wrapper
        self.client = None

    def skill(self, name):
        path = self.root / name / 'SKILL.md'
        path.parent.mkdir()
        path.write_text('---\nname: ' + name + '\ndescription: Alpha workflow.\n---\nOriginal body.\n')
        return self.lib.register(path, 'fixture:' + name), path

    def start(self):
        self.client = CountingClient(self.lib.path, self.counter, self.wrapper)
        self.addCleanup(self.client.close)
        self.listed()

    def listed(self):
        return self.client.call('tools/list')['result']['tools']

    def notified(self):
        self.assertEqual(self.client.receive(timeout=4)['method'], 'notifications/tools/list_changed')

    def loaded(self):
        result = self.client.call('tools/call', {'name': 'load_' + self.one['id'], 'arguments': {}})['result']
        self.assertFalse(result['isError'])
        return json.loads(result['content'][0]['text'])

    def stabilize_idle_metadata(self):
        # This one test's premise is stable metadata, including absent policies.
        # Capture real stat/lstat results at child startup; do not replace the
        # watcher signature, registry reads, skill reads, or loading requests.
        # The other notification tests continue using the real filesystem.
        stable_metadata = '''import json, os
_real_stat, _real_lstat = os.stat, os.lstat
_registry = Path(sys.argv[2])
_paths = {_registry}
for _entry in json.loads(_registry.read_text())['skills']:
    _body = Path(_entry['path'])
    _policy = _body.resolve().parent / 'agents' / 'openai.yaml'
    _paths.update((_body, _body.parent, _policy.parent, _policy))
_metadata = {}
for _path in _paths:
    for _follow, _read_stat in ((True, _real_stat), (False, _real_lstat)):
        try:
            _metadata[(str(_path), _follow)] = (True, _read_stat(_path))
        except OSError as _error:
            _metadata[(str(_path), _follow)] = (False, (_error.errno, _error.strerror))
def _stable_stat(path, *, dir_fd=None, follow_symlinks=True):
    if dir_fd is None and not isinstance(path, int):
        key = (os.path.abspath(os.fsdecode(path)), follow_symlinks)
        if key in _metadata:
            present, value = _metadata[key]
            if present:
                return value
            raise OSError(value[0], value[1], os.fspath(path))
    return _real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
def _stable_lstat(path, *, dir_fd=None):
    return _stable_stat(path, dir_fd=dir_fd, follow_symlinks=False)
os.stat, os.lstat = _stable_stat, _stable_lstat
'''
        code = self.wrapper.read_text()
        self.wrapper.write_text(code.replace('class Counted(Library):',
                                             stable_metadata + '\nclass Counted(Library):'))

    def test_unchanged_idle_does_not_reparse_catalog_but_requests_still_read_original(self):
        self.stabilize_idle_metadata()
        self.start()
        initial = int(self.counter.read_text())
        time.sleep(2.25)  # More than two real polling intervals.
        self.client.call('ping')
        self.assertEqual(int(self.counter.read_text()), initial)
        self.path.write_text(self.path.read_text().replace('Original body.', 'Changed body!'))
        # No need to wait for a notification; load must read the changed body now.
        self.assertIn('Changed body!', self.loaded()['text'])

    def test_same_length_rewrite_with_restored_mtime_notifies(self):
        self.start()
        original = self.path.stat()
        self.path.write_text(self.path.read_text().replace('Alpha', 'Bravo'))
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.notified()
        tool = next(t for t in self.listed() if t['name'] == 'load_' + self.one['id'])
        self.assertIn('Bravo', tool['description'])
        self.assertEqual(self.loaded()['text'], self.path.read_text())

    def test_atomic_replacement_deletion_and_recovery_notify(self):
        self.start()
        original = self.path.stat()
        replacement = self.path.with_name('replacement')
        replacement.write_text(self.path.read_text().replace('Alpha', 'Gamma'))
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, self.path)
        self.notified()
        self.assertIn('Gamma', next(t for t in self.listed() if t['name'] == 'load_' + self.one['id'])['description'])
        data = self.path.read_text()
        self.path.unlink()
        self.notified()
        self.assertNotIn('load_' + self.one['id'], [t['name'] for t in self.listed()])
        self.path.write_text(data)
        self.notified()
        self.assertIn('load_' + self.one['id'], [t['name'] for t in self.listed()])

    def test_policy_creation_invalidity_deletion_and_broken_directory_link_notify(self):
        self.start()
        agents = self.path.parent / 'agents'
        agents.mkdir()
        policy = agents / 'openai.yaml'
        policy.write_text('policy:\n  allow_implicit_invocation: false\n')
        self.notified()
        self.assertIn('load_explicit_skill', [t['name'] for t in self.listed()])
        policy.write_text('policy: [invalid]\n')
        self.notified()
        self.assertIn('library_status', [t['name'] for t in self.listed()])
        policy.unlink()
        self.notified()
        self.assertIn('load_' + self.one['id'], [t['name'] for t in self.listed()])
        agents.rmdir()
        agents.symlink_to(self.root / 'missing-agents', target_is_directory=True)
        self.notified()
        self.assertIn('library_status', [t['name'] for t in self.listed()])
        agents.unlink()
        self.notified()
        self.assertIn('load_' + self.one['id'], [t['name'] for t in self.listed()])

    def test_directory_symlink_switch_with_shared_body_inode_uses_new_policy(self):
        alternate = self.root / 'alternate'
        alternate.mkdir()
        os.link(self.path, alternate / 'SKILL.md')
        (alternate / 'agents').mkdir()
        (alternate / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        self.start()
        original_root = self.path.parent
        original_root.rename(self.root / 'archived')
        original_root.symlink_to(alternate, target_is_directory=True)
        self.notified()
        tools = [t['name'] for t in self.listed()]
        self.assertNotIn('load_' + self.one['id'], tools)
        self.assertIn('load_explicit_skill', tools)

    def test_final_skill_symlink_watches_target_policy_root(self):
        alternate = self.root / 'target'
        alternate.mkdir()
        target = alternate / 'SKILL.md'
        target.write_text(self.path.read_text())
        self.path.unlink()
        self.path.symlink_to(target)
        self.start()
        (alternate / 'agents').mkdir()
        (alternate / 'agents/openai.yaml').write_text('policy:\n  allow_implicit_invocation: false\n')
        self.notified()
        self.assertIn('load_explicit_skill', [t['name'] for t in self.listed()])

    def test_registry_corruption_and_recovery_notify_without_stale_catalog(self):
        self.start()
        original = self.lib.path.read_bytes()
        self.lib.path.write_text('{')
        self.notified()
        self.assertIn('error', self.client.call('tools/list'))
        self.lib.path.write_bytes(original)
        self.notified()
        self.assertIn('load_' + self.one['id'], [t['name'] for t in self.listed()])


if __name__ == '__main__':
    unittest.main()
