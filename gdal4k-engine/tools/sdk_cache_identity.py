"""Stable SDK configuration identity; producer work and install paths stay private.

The SDK inventory is evidence, not a global port ABI stamp. vcpkg continues to
hash each port, resolved features and dependency ABIs independently. Only the
shared configuration/include/toolchain closure is an additional global ABI input.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

SOURCE_LIMIT = 8 * 1024 ** 2
FILE_LIMIT = 1024 ** 2
MANIFEST_LIMIT = 2 * 1024 ** 2
TOOL_LIMIT = 128 * 1024 ** 2
MAX_FILES = 10000
_FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_birthtime_ns')
_WINDOWS_STAT = os.name == 'nt'


def _need(value, message):
    if not value:
        raise RuntimeError(message)


def _state(value):
    return tuple(getattr(value, key, None) for key in _FIELDS)


def _linked(path, value=None):
    value = Path(path).lstat() if value is None else value
    return (stat.S_ISLNK(value.st_mode) or bool(getattr(value, 'st_file_attributes', 0) & 0x400) or
            getattr(Path(path), 'is_junction', lambda: False)())


def _changed_fields(left, right):
    return [key for key in _FIELDS if getattr(left, key, None) != getattr(right, key, None)]


def _path_handle_changes(path, before, opened):
    changed = _changed_fields(before, opened)
    if _WINDOWS_STAT:
        # CPython 3.14.10 path stat adds 0111 for these suffixes; fstat has
        # no filename. Keep every other permission/file-type bit exact.
        execute = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if (Path(path).suffix.lower() in ('.exe', '.bat', '.cmd', '.com') and
                before.st_mode & execute == execute and opened.st_mode & execute == 0 and
                before.st_mode & ~execute == opened.st_mode):
            changed = [key for key in changed if key != 'st_mode']
        # Windows path stat exposes birthtime as ctime, while fstat exposes
        # ChangeTime. Join explicit birthtime and compare each API's raw
        # ctime against its own snapshot after reading; never erase changes.
        birth = getattr(before, 'st_birthtime_ns', None)
        if (type(birth) is int and before.st_ctime_ns == birth and
                getattr(opened, 'st_birthtime_ns', None) == birth):
            changed = [key for key in changed if key != 'st_ctime_ns']
    return changed


def _unchanged(changed, phase, label):
    safe = str(label).encode('unicode_escape').decode('ascii')[:240]
    _need(not changed, 'SDK identity input changed ' + phase + ': ' + safe +
          ' (' + ','.join(changed) + ')')


def _hash_file(path, maximum, label=None):
    path = Path(path)
    label = path.name if label is None else label
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and not _linked(path, before), 'SDK identity requires a regular input: ' + str(path))
    _need(0 <= before.st_size <= maximum, 'SDK identity input exceeds its bound: ' + str(path))
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as source:
        opened = os.fstat(source.fileno())
        _unchanged(_path_handle_changes(path, before, opened), 'before read', label)
        size, digest = 0, hashlib.sha256()
        while data := source.read(65536):
            size += len(data)
            _need(size <= maximum, 'SDK identity input grew past its bound')
            digest.update(data)
        _need(size == before.st_size, 'SDK identity input size changed during read: ' + str(label))
        _unchanged(_changed_fields(before, path.lstat()), 'at path during read', label)
        _unchanged(_changed_fields(opened, os.fstat(source.fileno())), 'at descriptor during read', label)
    return {'bytes': size, 'sha256': digest.hexdigest()}


class _Inventory:
    def __init__(self):
        self.bytes = 0
        self.files = 0

    def file(self, path, name):
        _need(type(name) is str and name and '\\' not in name and not name.startswith('/') and
              '..' not in name.split('/'), 'SDK identity name is not canonical')
        value = _hash_file(path, FILE_LIMIT, name)
        self.bytes += value['bytes']
        self.files += 1
        _need(self.bytes <= SOURCE_LIMIT and self.files <= MAX_FILES, 'SDK identity source budget exceeded')
        return {'path': name, **value}

    def tree(self, directory, prefix):
        directory = Path(directory)
        _need(directory.is_dir() and not _linked(directory), 'SDK identity source directory is absent or linked')
        result = []
        def failed_enumeration(error):
            raise RuntimeError('Cannot enumerate SDK identity sources: ' + str(directory)) from error
        for parent, directories, files in os.walk(directory, followlinks=False, onerror=failed_enumeration):
            for name in directories:
                _need(not _linked(Path(parent) / name), 'SDK identity source directory contains a link')
            for name in files:
                path = Path(parent) / name
                result.append(self.file(path, prefix + '/' + path.relative_to(directory).as_posix()))
        return sorted(result, key=lambda value: value['path'])


def _canonical(value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    _need(len(data) <= MANIFEST_LIMIT, 'SDK identity manifest exceeds its bound')
    return data


def _tool_pins(path):
    """Compiler/bootstrap configuration without a global source-port revision."""
    path = Path(path)
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and not _linked(path, before) and
          before.st_size <= FILE_LIMIT, 'SDK tool pins must be a bounded regular file')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        opened = os.fstat(source.fileno())
        _unchanged(_path_handle_changes(path, before, opened), 'before tool-pin read', path.name)
        data = source.read(FILE_LIMIT + 1)
        _need(len(data) == before.st_size, 'SDK tool pins size changed during read')
        _unchanged(_changed_fields(before, path.lstat()), 'at path during tool-pin read', path.name)
        _unchanged(_changed_fields(opened, os.fstat(source.fileno())), 'at descriptor during tool-pin read', path.name)
    result = {}
    for line in data.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        _need('=' in line, 'Invalid SDK tool pin property')
        name, value = line.split('=', 1)
        _need(name not in result, 'Duplicate SDK tool pin property')
        result[name] = value
    # Source revisions are recorded in the overall inventory. vcpkg hashes its
    # own ports/features/dependencies; changing one port must not invalidate all.
    return {name: value for name, value in sorted(result.items())
            if name not in ('vcpkg.version', 'vcpkg.revision')}


def _write(path, data):
    """Replace only a generated producer manifest under the existing build lock."""
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def require_pinned_clean_vcpkg(vcpkg, revision):
    """A revision can cover transitive source only when tracked bytes are clean."""
    _need(type(revision) is str and re.fullmatch('[0-9a-f]{40}', revision), 'Invalid pinned vcpkg revision')
    result = subprocess.run(['git', '-C', str(vcpkg), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True, check=True)
    _need(result.stdout.strip() == revision, 'SDK identity vcpkg revision differs from its pin')
    result = subprocess.run(['git', '-C', str(vcpkg), 'diff', '--quiet', 'HEAD', '--'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    _need(result.returncode == 0, 'SDK identity requires an unchanged tracked pinned vcpkg source')


def _native_auxiliary_inputs(out, toolchain):
    """Bind renamed ranlib contents while keeping its producer path relocatable."""
    if toolchain is None:
        return []
    path = Path(toolchain)
    _need(path.parent == Path(out), 'Native toolchain must be inside its private output directory')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOATIME', 0)
    with os.fdopen(os.open(path, flags), 'r', encoding='utf-8') as source:
        raw = source.read(FILE_LIMIT + 1)
    _need(len(raw.encode('utf-8')) <= FILE_LIMIT, 'Native toolchain text exceeds its bound')
    matches = re.findall(r'(?m)^set\(CMAKE_AR "([^"]+)"\)$', raw)
    _need(len(matches) == 1, 'Native toolchain lacks one exact archiver source')
    ar = Path(matches[0])
    _need(ar.is_absolute(), 'Native archiver source must be absolute')
    ar_state = _state(ar.lstat())
    resolved_ar = ar.resolve(strict=True)
    expected = _hash_file(resolved_ar, TOOL_LIMIT)
    _need(_state(ar.lstat()) == ar_state, 'Native archiver source changed')
    name = 'llvm-ranlib.exe' if matches[0].lower().endswith('.exe') else 'llvm-ranlib'
    ranlib = Path(out) / name
    link = ranlib.lstat()
    if stat.S_ISLNK(link.st_mode):
        _need(ranlib.resolve(strict=True) == resolved_ar, 'Native ranlib link does not select its archiver')
        _need(_state(ranlib.lstat()) == _state(link), 'Native ranlib link changed')
    else:
        _need(_hash_file(ranlib, TOOL_LIMIT) == expected, 'Native ranlib copy differs from its archiver')
    return [{'path': 'native-auxiliary/' + name, **expected}]


def prepare_sdk_cache_inputs(root, vcpkg, out, base_file, toolchain, target, triplet_template, revision):
    """Return an ABI wrapper with path-independent, explicitly bound includes.

    Source, install and CMake directories remain private. Source inventories use
    canonical logical names and hashes, never checkout paths or fixture URLs.
    Port and feature changes update overall SDK evidence; vcpkg's own selective
    port/features/dependency ABI logic remains authoritative for those changes.
    """
    root, vcpkg, out, base_file = map(Path, (root, vcpkg, out, base_file))
    require_pinned_clean_vcpkg(vcpkg, revision)
    _need(re.fullmatch('[a-z0-9][a-z0-9-]*', target), 'Invalid SDK target')
    _need(type(triplet_template) is str and '\x00' not in triplet_template, 'Invalid SDK triplet template')
    base_relative = base_file.relative_to(vcpkg).as_posix()
    _need(base_relative.startswith('triplets/') and '..' not in base_relative.split('/'), 'SDK base triplet is outside pinned triplets')
    inventory = _Inventory()
    framework = inventory.tree(vcpkg / 'scripts', 'vcpkg/scripts')
    framework += inventory.tree(vcpkg / 'triplets', 'vcpkg/triplets')
    generators = [inventory.file(root / name, 'project/' + name) for name in
                  ('gdal4k-engine/tools/toolchains.py', 'gdal4k-engine/tools/sdk_cache_identity.py')]
    chain = None if toolchain is None else inventory.file(toolchain, 'generated/toolchain.cmake')
    headers = inventory.tree(out / 'compiler-headers', 'generated/compiler-headers') if (out / 'compiler-headers').exists() else []
    configuration = {'schemaVersion': 1, 'kind': 'gdal4k-sdk-toolchain-inputs', 'target': target,
                     'baseTriplet': base_relative, 'tripletTemplate': triplet_template,
                     'frameworkSources': framework, 'sourceGenerators': generators,
                     'toolBootstrapPins': _tool_pins(root / 'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties'),
                     'nativeToolchain': chain, 'nativeHeaderOverlay': headers,
                     'nativeAuxiliaryTools': _native_auxiliary_inputs(out, toolchain)}
    config_data = _canonical(configuration)
    config_sha = hashlib.sha256(config_data).hexdigest()
    ports = inventory.tree(root / 'gdal4k-engine/ports', 'project/gdal4k-engine/ports')
    engine = inventory.tree(root / 'gdal4k-engine/engine', 'project/gdal4k-engine/engine')
    bootstrap = [inventory.file(root / name, 'project/' + name) for name in
                 ('gdal4k-engine/tools/build-engine.py', 'gdal4k-engine/tools/versions.py',
                  'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties',
                  'build-logic/src/main/resources/dev/tronto/buildtools/prepare_tool.py',
                  'build-logic/src/main/resources/dev/tronto/buildtools/vcpkg_archive.py')]
    bootstrap += [inventory.file(vcpkg / name, 'vcpkg/' + name) for name in
                  ('bootstrap-vcpkg.sh', 'bootstrap-vcpkg.bat')]
    overall = {'schemaVersion': 1, 'kind': 'gdal4k-sdk-source-inputs', 'target': target,
               'vcpkgRevision': revision, 'trackedVcpkgSourceClean': True,
               'toolchainInputsSha256': config_sha, 'overlayPortSources': ports,
               'featureAndConfigurationSources': engine, 'bootstrapAndBuilderSources': bootstrap,
               'selectivePortAbi': 'Pinned vcpkg hashes each own port, features and dependency ABIs; this overall inventory is evidence only.'}
    overall_data = _canonical(overall)
    _write(out / 'sdk-toolchain-inputs.json', config_data)
    _write(out / 'sdk-source-inputs.json', overall_data)
    print(json.dumps({'sdkCacheInputs': {'toolchainSha256': config_sha,
          'sdkSourceSha256': hashlib.sha256(overall_data).hexdigest(),
          'sourceFiles': inventory.files, 'sourceBytes': inventory.bytes,
          'actualVcpkgAbiOrCacheHitClaimed': False}}), flush=True)
    # The literal wrapper and external toolchain are already hashed by vcpkg.
    # Additional-file hashing binds included framework/base/header/tool content;
    # vcpkg hashes contents, not this expanded producer-local filename.
    return (triplet_template + '\n# gdal4k SDK toolchain inputs: ' + config_sha + '\n' +
            'list(APPEND VCPKG_HASH_ADDITIONAL_FILES\n' +
            '  "${CMAKE_CURRENT_LIST_DIR}/../engine/' + target + '/sdk-toolchain-inputs.json")\n')
