#!/usr/bin/env python3
"""Install a small, owned Skill Library routing rule without replacing user instructions."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

from configure import atomic, sha
from library import LibraryError

BEGIN = '<!-- codex-skill-library:begin -->'
END = '<!-- codex-skill-library:end -->'
BLOCK = (BEGIN + '\n## Skill Library workflow check\n\n'
    'When Skill Library is available, before research, web, notes, writing, code, data or media work, '
    'discover matching workflows and load applicable ones before answering or acting. '
    'This includes short replies and tasks without file writes. Use tool discovery; '
    'if it is unavailable, inspect ALL_TOOLS metadata. Skip this check only for arithmetic, '
    'translation and punctuation-only edits. Load explicit-only workflows only when the user names them. '
    'If the Library is unavailable, use the normal skill workflow.\n' + END + '\n')


def write_target(path, data, expected, expected_exists, remove=False):
    """Anchor replacement in a verified parent; never follow a replaced directory."""
    check_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    temp = None
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        try:
            target = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            current, exists, mode = b'', False, 0o600
        else:
            with os.fdopen(target, 'rb') as stream:
                current = stream.read()
                mode = os.fstat(stream.fileno()).st_mode & 0o777
            exists = True
        if current != expected or exists != expected_exists:
            raise LibraryError('Global instructions changed before replacement')
        check_path(path)
        linked, opened = os.stat(path.parent), os.fstat(parent)
        if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
            raise LibraryError('Bootstrap parent changed during replacement')
        if remove:
            if exists:
                os.unlink(path.name, dir_fd=parent)
        else:
            temp = '.skill-library-' + uuid.uuid4().hex
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            temp = None
        os.fsync(parent)
        check_path(path)
        linked, opened = os.stat(path.parent), os.fstat(parent)
        if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
            raise LibraryError('Bootstrap parent changed during replacement')
    finally:
        if temp is not None:
            os.unlink(temp, dir_fd=parent)
        os.close(parent)


def check_path(path):
    path = Path(path).expanduser().absolute()
    if path.name != 'AGENTS.md' or path.is_symlink():
        raise LibraryError('Bootstrap requires a regular AGENTS.md path')
    if path.exists() and not path.is_file():
        raise LibraryError('Bootstrap target is not a file')
    if any(parent.is_symlink() for parent in path.parents):
        raise LibraryError('Bootstrap parent path changed to a symlink')
    if (path.parent / 'AGENTS.override.md').exists():
        raise LibraryError('AGENTS.override.md shadows bootstrap; resolve it explicitly')
    return path


def span(text):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise LibraryError('Conflicting bootstrap markers')
    if BEGIN not in text:
        return None
    start, end = text.index(BEGIN), text.index(END) + len(END)
    if end <= start:
        raise LibraryError('Conflicting bootstrap marker order')
    if text[end:end + 1] == '\n':
        end += 1
    return start, end


def desired(text):
    owned = span(text)
    if owned:
        if text[owned[0]:owned[1]] != BLOCK:
            raise LibraryError('Existing owned rule differs; roll back its transaction before replacing it')
        return text[:owned[0]] + BLOCK + text[owned[1]:]
    separator = '' if not text else ('\n' if text.endswith('\n') else '\n\n')
    return text + separator + BLOCK


def make_plan(path):
    raw = Path(path).expanduser().absolute()
    if raw.is_symlink():
        raise LibraryError('Bootstrap requires a regular AGENTS.md path')
    path = raw.parent.resolve() / raw.name
    path = check_path(path)
    before = path.read_bytes().decode('utf-8') if path.exists() else ''
    after = desired(before)
    return {'version': 1, 'path': str(path), 'existed': path.exists(),
            'before': before, 'after': after, 'before_sha256': sha(before.encode()),
            'after_sha256': sha(after.encode())}


def validate(record):
    if (not isinstance(record, dict) or record.get('version') != 1
            or type(record.get('existed')) is not bool):
        raise LibraryError('Invalid bootstrap record')
    if not isinstance(record.get('path'), str) or not Path(record['path']).is_absolute():
        raise LibraryError('Invalid bootstrap path')
    for key in ('before', 'after'):
        if (not isinstance(record.get(key), str)
                or not re.fullmatch('[0-9a-f]{64}', record.get(key + '_sha256', ''))
                or sha(record[key].encode()) != record[key + '_sha256']):
            raise LibraryError('Bootstrap checksum mismatch')
    old, new = span(record['before']), span(record['after'])
    if not new:
        raise LibraryError('Bootstrap record lacks owned block')
    block = record['after'][new[0]:new[1]]
    if old:
        expected = record['before'][:old[0]] + block + record['before'][old[1]:]
    else:
        before = record['before']
        separator = '' if not before else ('\n' if before.endswith('\n') else '\n\n')
        expected = before + separator + block
    if record['after'] != expected:
        raise LibraryError('Bootstrap record modifies unowned instructions')
    if record.get('status') == 'rolling_back':
        if (not isinstance(record.get('rollback_result'), str)
                or sha(record['rollback_result'].encode()) != record.get('rollback_result_sha256')):
            raise LibraryError('Bootstrap rollback checkpoint checksum mismatch')


def apply(plan, transactions):
    validate(plan)
    path = check_path(plan['path'])
    if desired(plan['before']) != plan['after']:
        raise LibraryError('Bootstrap plan does not match current routing rule')
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.skill-library.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes() if path.exists() else b''
        if sha(before) != plan['before_sha256'] or path.exists() != plan['existed']:
            raise LibraryError('Global instructions changed after plan')
        if plan['before'] == plan['after']:
            return {'changed': False, 'transaction': None}
        txn = Path(transactions).absolute() / uuid.uuid4().hex
        txn.mkdir(parents=True, mode=0o700)
        record = dict(plan, status='prepared')
        atomic(txn / 'transaction.json', (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
        if (path.read_bytes() if path.exists() else b'') != before:
            raise LibraryError('Global instructions changed during apply')
        write_target(path, plan['after'].encode(), before, plan['existed'])
        record['status'] = 'applied'
        atomic(txn / 'transaction.json', (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
        return {'changed': True, 'transaction': str(txn), 'after_sha256': plan['after_sha256']}


def rollback(transaction):
    txn = Path(transaction) / 'transaction.json'
    record = json.loads(txn.read_text())
    validate(record)
    if record.get('status') not in ('prepared', 'applied', 'rolling_back', 'rolled_back'):
        raise LibraryError('Invalid bootstrap transaction status')
    path = check_path(record['path'])
    if record.get('status') == 'rolled_back':
        return {'changed': False, 'status': 'already_rolled_back'}
    with open(str(path) + '.skill-library.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = path.read_bytes().decode('utf-8') if path.exists() else ''
        restored = (record.get('status') == 'rolling_back'
                    and (current == record['rollback_result']
                         or (span(current) is None and span(record['rollback_result']) is None)))
        if restored:
            result = current
        elif sha(current.encode()) == record['after_sha256']:
            result = record['before']
        elif current == record['before']:
            result = current
        else:
            now, old = span(current), span(record['before'])
            installed = span(record['after'])
            if not now or current[now[0]:now[1]] != record['after'][installed[0]:installed[1]]:
                raise LibraryError('Bootstrap rollback conflict: owned rule changed')
            original = record['before'][old[0]:old[1]] if old else ''
            result = current[:now[0]] + original + current[now[1]:]
            if not old:
                separator = record['after'][len(record['before']):installed[0]]
                if separator and result[max(0, now[0] - len(separator)):now[0]] == separator:
                    result = result[:now[0] - len(separator)] + result[now[0]:]
        if (path.read_bytes().decode('utf-8') if path.exists() else '') != current:
            raise LibraryError('Global instructions changed during rollback')
        if not restored:
            record.update(status='rolling_back', rollback_result=result,
                          rollback_result_sha256=sha(result.encode()))
            atomic(txn, (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
            write_target(path, result.encode(), current.encode(), path.exists(),
                         remove=not record['existed'] and not result)
        record['status'] = 'rolled_back'
        atomic(txn, (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode())
        return {'changed': result != current, 'status': 'rolled_back'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--path', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('apply')
    p.add_argument('--plan', required=True)
    p.add_argument('--transactions', required=True)
    p = sub.add_parser('rollback')
    p.add_argument('transaction')
    args = parser.parse_args()
    if args.command == 'plan':
        result = make_plan(args.path)
        atomic(Path(args.output), (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode())
        print(json.dumps({'plan': args.output, 'added_bytes': len(result['after'].encode()) - len(result['before'].encode())}))
    elif args.command == 'apply':
        print(json.dumps(apply(json.loads(Path(args.plan).read_text()), args.transactions)))
    else:
        print(json.dumps(rollback(args.transaction)))


if __name__ == '__main__':
    main()
