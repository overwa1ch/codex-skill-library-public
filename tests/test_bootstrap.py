import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
from library import LibraryError


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / 'AGENTS.md'
        self.original = b'## Existing instructions\r\n\r\nPreserve this exactly.'
        self.path.write_bytes(self.original)
        self.path.chmod(0o640)

    def apply(self):
        return bootstrap.apply(bootstrap.make_plan(self.path), self.root / 'transactions')

    def test_byte_exact_roundtrip_modes_and_idempotence(self):
        result = self.apply()
        self.assertIn(bootstrap.BLOCK.encode(), self.path.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.apply()['changed'])
        bootstrap.rollback(result['transaction'])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(bootstrap.rollback(result['transaction'])['status'], 'already_rolled_back')

    def test_missing_original_removed_only_without_new_content(self):
        self.path.unlink()
        first = self.apply()
        bootstrap.rollback(first['transaction'])
        self.assertFalse(self.path.exists())
        second = self.apply()
        self.path.write_bytes(self.path.read_bytes() + b'New unrelated instructions.\r\n')
        bootstrap.rollback(second['transaction'])
        self.assertEqual(self.path.read_bytes(), b'New unrelated instructions.\r\n')

    def test_outside_concurrent_edits_preserved(self):
        result = self.apply()
        now = b'Preface\r\n' + self.path.read_bytes().replace(b'Preserve this exactly.', b'Edited externally.') + b'\nTail'
        self.path.write_bytes(now)
        bootstrap.rollback(result['transaction'])
        expected = b'Preface\r\n' + self.original.replace(b'Preserve this exactly.', b'Edited externally.') + b'\nTail'
        self.assertEqual(self.path.read_bytes(), expected)

    def test_inside_conflict_does_not_overwrite(self):
        result = self.apply()
        now = self.path.read_bytes().replace(b'Skip this check only', b'External replacement')
        self.path.write_bytes(now)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            bootstrap.rollback(result['transaction'])
        self.assertEqual(self.path.read_bytes(), now)

    def test_bad_markers_and_foreign_block_rejected(self):
        for text in (bootstrap.BEGIN, bootstrap.END, bootstrap.BLOCK * 2,
                     bootstrap.END + bootstrap.BEGIN,
                     bootstrap.BEGIN + '\nUnrecognized content\n' + bootstrap.END):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(LibraryError):
                    bootstrap.make_plan(self.path)
                self.assertEqual(self.path.read_text(), text)

    def test_stale_plan_refuses_all_writes(self):
        plan = bootstrap.make_plan(self.path)
        current = self.original + b'\nExternal addition'
        self.path.write_bytes(current)
        with self.assertRaisesRegex(LibraryError, 'changed after plan'):
            bootstrap.apply(plan, self.root / 'transactions')
        self.assertEqual(self.path.read_bytes(), current)

    def test_symlink_and_override_refused(self):
        plan = bootstrap.make_plan(self.path)
        outside = self.root / 'other.md'
        outside.write_bytes(self.original)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(LibraryError):
            bootstrap.apply(plan, self.root / 'transactions')
        self.assertEqual(outside.read_bytes(), self.original)
        self.path.unlink()
        self.path.write_bytes(self.original)
        (self.root / 'AGENTS.override.md').write_text('Overrides')
        with self.assertRaisesRegex(LibraryError, 'shadows'):
            bootstrap.apply(plan, self.root / 'transactions')

    def test_parent_symlink_replacement_refused(self):
        folder = self.root / 'folder'
        folder.mkdir()
        path = folder / 'AGENTS.md'
        path.write_bytes(self.original)
        plan = bootstrap.make_plan(path)
        folder.rename(self.root / 'moved')
        folder.symlink_to(self.root / 'moved', target_is_directory=True)
        with self.assertRaisesRegex(LibraryError, 'symlink'):
            bootstrap.apply(plan, self.root / 'transactions')

    def test_tampered_plan_and_transaction_refused(self):
        plan = bootstrap.make_plan(self.path)
        plan['after'] += 'Unowned mutation'
        with self.assertRaisesRegex(LibraryError, 'checksum'):
            bootstrap.apply(plan, self.root / 'transactions')
        result = self.apply()
        transaction = Path(result['transaction']) / 'transaction.json'
        record = json.loads(transaction.read_text())
        record['before'] = 'Changed backup'
        transaction.write_text(json.dumps(record))
        current = self.path.read_bytes()
        with self.assertRaisesRegex(LibraryError, 'checksum'):
            bootstrap.rollback(result['transaction'])
        self.assertEqual(self.path.read_bytes(), current)

    def test_prepared_transactions_recover_before_and_after_target_write(self):
        with patch.object(bootstrap, 'write_target', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                self.apply()
        transaction = next((self.root / 'transactions').iterdir())
        self.assertEqual(json.loads((transaction / 'transaction.json').read_text())['status'], 'prepared')
        bootstrap.rollback(transaction)
        self.assertEqual(self.path.read_bytes(), self.original)
        result = self.apply()
        transaction = Path(result['transaction']) / 'transaction.json'
        record = json.loads(transaction.read_text())
        record['status'] = 'prepared'
        transaction.write_text(json.dumps(record))
        bootstrap.rollback(transaction.parent)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_old_transaction_cannot_remove_revised_rule(self):
        result = self.apply()
        current = self.path.read_bytes().replace(b'## Skill Library workflow check', b'## Skill Library revised routing')
        self.path.write_bytes(current)
        with self.assertRaisesRegex(LibraryError, 'conflict'):
            bootstrap.rollback(result['transaction'])
        self.assertEqual(self.path.read_bytes(), current)
