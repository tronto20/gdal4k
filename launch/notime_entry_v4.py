"""Outer execution envelope; no validator behavior or approved source is modified."""
import hashlib
import importlib.abc
import importlib.util
import json
import linecache
import os
import stat
import sys
import types
import traceback


def cached_unhandled_exception(exc_type, exc_value, exc_traceback):
    # CPython calls this only for unhandled exceptions, not SystemExit.
    # Frozen source lines are already cached with mtime=None by this envelope.
    try:
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)
    except BaseException:
        # Never fall back to CPython's C source reader if stderr rendering fails.
        try:
            os.write(2, b'NoTime unhandled-exception rendering failed; original process failure retained.\n')
        except OSError:
            pass


sys.excepthook = cached_unhandled_exception

FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
          'st_size', 'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks')
MAX_SOURCE = 2 * 1048576
MAX_CLOSURE = 2 * 1048576


def require(value, message):
    if not value:
        raise RuntimeError(message)


def full(value):
    return {key: getattr(value, key) for key in FIELDS}


def unique(items):
    result = {}
    for key, value in items:
        require(key not in result, 'Duplicate envelope JSON key')
        result[key] = value
    return result


def canonical_path(path):
    require(isinstance(path, str) and path.startswith('/') and
            os.path.normpath(path) == path and not path.startswith('//'),
            'Envelope source path must be canonical and absolute')
    return path


def read_verified(reference, expected_full11=None):
    require(set(reference) == {'path', 'bytes', 'sha256'} and
            type(reference['bytes']) is int and 0 < reference['bytes'] <= MAX_SOURCE and
            isinstance(reference['sha256'], str) and len(reference['sha256']) == 64 and
            all(char in '0123456789abcdef' for char in reference['sha256']),
            'Invalid envelope source reference')
    path = canonical_path(reference['path'])
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = path.split('/')[1:]
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME |
                     os.O_NONBLOCK, dir_fd=directory)
        try:
            before = full(os.fstat(fd))
            require(stat.S_ISREG(before['st_mode']) and before['st_uid'] == os.getuid() and
                    before['st_nlink'] == 1 and stat.S_IMODE(before['st_mode']) == 0o400 and
                    before['st_size'] == reference['bytes'], 'Envelope source seal differs')
            if expected_full11 is not None:
                require(before == expected_full11, 'Envelope source full11 baseline differs')
            chunks, size = [], 0
            while True:
                data = os.read(fd, min(65536, MAX_SOURCE + 1 - size))
                if not data:
                    break
                chunks.append(data)
                size += len(data)
                require(size <= reference['bytes'], 'Envelope source grew')
            raw = b''.join(chunks)
            require(size == reference['bytes'] and hashlib.sha256(raw).hexdigest() ==
                    reference['sha256'], 'Envelope source whole bytes/SHA differs')
            require(full(os.fstat(fd)) == before == full(os.lstat(path)),
                    'Envelope source full11 changed during NoTime read')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def cache_source(path, raw):
    # mtime=None prevents linecache.checkcache() from stat/read fallback.
    linecache.cache[path] = (len(raw), None, raw.decode('utf-8').splitlines(True), path)


class PinnedLoader(importlib.abc.Loader):
    def __init__(self, record, raw):
        self.record, self.raw = record, raw

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = self.record['reference']['path']
        read_verified(self.record['reference'], self.record['full11'])
        module.__file__ = path
        module.__cached__ = None
        cache_source(path, self.raw)
        exec(compile(self.raw, path, 'exec'), module.__dict__)


class PinnedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, modules, source_root):
        self.modules, self.source_root = modules, source_root

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.modules:
            record, raw = self.modules[fullname]
            filename = record['reference']['path']
            return importlib.util.spec_from_file_location(
                fullname, filename, loader=PinnedLoader(record, raw))
        # Reject unpinned files, packages and namespace roots before PathFinder.
        candidate = self.source_root + '/' + fullname.replace('.', '/')
        if os.path.lexists(candidate + '.py') or os.path.lexists(candidate):
            raise ImportError('Unpinned source-root import is forbidden: ' + fullname)
        return None


def run(policy_reference, original_argv):
    require(original_argv and isinstance(original_argv, list), 'Missing original script argv')
    policy_raw = read_verified(policy_reference)
    policy = json.loads(policy_raw, object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite envelope JSON')))
    require(set(policy) == {'schemaVersion', 'kind', 'purpose', 'sourceRoot', 'entryScripts',
                            'sources', 'realInterpreter', 'interpreterFull11'} and
            policy['schemaVersion'] == 1 and
            policy['kind'] == 'C21-pinned-NoTime-outer-launch-policy-v1',
            'Unexpected outer execution policy')
    root = canonical_path(policy['sourceRoot'])
    real = canonical_path(policy['realInterpreter'])
    require(os.path.realpath(sys.executable) == real and
            full(os.lstat(real)) == policy['interpreterFull11'],
            'Absolute interpreter identity differs')
    require(sys.flags.dont_write_bytecode and sys.dont_write_bytecode,
            'The actual interpreter must receive -B')
    script = canonical_path(original_argv[0])
    require(isinstance(policy['entryScripts'], list) and script in policy['entryScripts'],
            'Script is not an exact approved envelope entry path')
    sources = policy['sources']
    require(isinstance(sources, list) and 1 <= len(sources) <= 64,
            'Invalid envelope closure count')
    modules, records, total = {}, {}, 0
    for record in sources:
        require(set(record) == {'reference', 'full11'}, 'Unexpected source closure row')
        reference = record['reference']
        name = reference['path']
        require(name.startswith(root + '/') and name not in records,
                'Envelope sources must be unique pinned paths within source root')
        raw = read_verified(reference, record['full11'])
        total += len(raw)
        require(total <= MAX_CLOSURE, 'Envelope closure bytes exceed bound')
        records[name] = (record, raw)
        cache_source(name, raw)
        # Nested frozen records are guarded/cached, but are not flat module imports.
        if name.endswith('.py') and '/' not in name[len(root) + 1:]:
            module_name = os.path.basename(name)[:-3]
            require(module_name.isidentifier() and module_name not in modules and
                    module_name not in sys.modules, 'Source module name collides or is preloaded')
            modules[module_name] = (record, raw)
    require(all(path in records and path.endswith('.py') for path in policy['entryScripts']),
            'Envelope entry is missing from the pinned source closure')
    require(script in records, 'Main source is unpinned')
    finder = PinnedFinder(modules, root)
    sys.meta_path.insert(0, finder)
    sys.path[0] = root
    sys.argv = list(original_argv)
    main = types.ModuleType('__main__')
    main.__file__, main.__package__, main.__spec__, main.__cached__ = script, None, None, None
    main.__loader__ = PinnedLoader(*records[script])
    sys.modules['__main__'] = main
    try:
        exec(compile(records[script][1], script, 'exec'), main.__dict__)
    finally:
        # Preserve failures and metadata; never restore timestamps or rewrite a source.
        for record, raw in records.values():
            read_verified(record['reference'], record['full11'])
