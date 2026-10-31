"""Metadata-only SDK cache counterfactuals; no compiler or installer is run.

The ABI model follows the pinned vcpkg input hashing shape using a synthetic
opaque compiler hash. It is not an observed package ABI or cache-hit benchmark.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'gdal4k-engine/tools'
sys.path.insert(0, str(TOOLS))


def no_time(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOATIME', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        return source.read()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    exec(compile(no_time(path), str(path), 'exec'), value.__dict__)
    return value


cache = module('sdk_cache_identity', TOOLS / 'sdk_cache_identity.py')
toolchains = module('toolchains', TOOLS / 'toolchains.py')
producer = module('sdk_cache_test_producer', TOOLS / 'build-engine.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pinned_abi_input_model(triplet, chain, additional, own_port=b'port', features=('core',),
                           dependencies=(), compiler='synthetic-opaque-compiler-hash', triplet_name='gdal4k-fixture'):
    triplet_info = digest(triplet) + '-' + digest(chain) + '-' + compiler
    entries = [('triplet', triplet_name), ('triplet_abi', triplet_info), ('portfile.cmake', digest(own_port)),
               ('features', ';'.join(sorted(features)))]
    entries += [('additional_file_' + str(i), digest(raw)) for i, raw in enumerate(additional)]
    entries += list(dependencies)
    return digest(''.join(key + ' ' + value + '\n' for key, value in sorted(entries)).encode())


class InstallerMustNotRun(Exception):
    pass


PROJECT_INPUTS = (
    'gdal4k-engine/tools/build-engine.py', 'gdal4k-engine/tools/toolchains.py',
    'gdal4k-engine/tools/sdk_cache_identity.py', 'gdal4k-engine/tools/versions.py',
    'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties',
    'build-logic/src/main/resources/dev/tronto/buildtools/prepare_tool.py',
    'build-logic/src/main/resources/dev/tronto/buildtools/vcpkg_archive.py',
)


class Fixture:
    def __init__(self, root, sdk, target='linux-amd64', short_vcpkg=False):
        self.root, self.sdk, self.target = root, sdk, target
        self.vcpkg = root / ('v' if short_vcpkg else 'verification/tools/vcpkg')
        self.out = root / 'gdal4k-engine/build/engine' / target
        self.out.mkdir(parents=True)
        for name in PROJECT_INPUTS:
            self.write(root / name, no_time(ROOT / name))
        self.write(root / 'gdal4k-engine/ports/gdal/portfile.cmake', b'fixture GDAL port\n')
        self.write(root / 'gdal4k-engine/ports/zlib/portfile.cmake', b'fixture zlib port\n')
        self.write(root / 'gdal4k-engine/engine/vcpkg.json', b'{"dependencies":["gdal"]}\n')
        self.write(self.vcpkg / 'scripts/toolchains/fixture.cmake', b'# pinned SDK framework\n')
        for base in producer.TARGETS.values():
            self.write(self.vcpkg / 'triplets/community' / (base + '.cmake'), b'set(FIXTURE_BASE true)\n')
        self.write(self.vcpkg / 'bootstrap-vcpkg.sh', b'# fixture bootstrap sh\n')
        self.write(self.vcpkg / 'bootstrap-vcpkg.bat', b'fixture bootstrap bat\n')
        self.write(self.vcpkg / 'ports/gdal/portfile.cmake', b'builtin GDAL port\n')
        self.git('init', '-q')
        self.commit()
        self.write(self.vcpkg / 'vcpkg', b'installer placeholder; never executed')
        self.calls, self.bootstrap_calls = [], []

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def git(self, *args):
        env = os.environ.copy()
        env.update(GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
        return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                        '-C', str(self.vcpkg), *args], env=env, stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git('add', 'scripts', 'triplets', 'ports', 'bootstrap-vcpkg.sh', 'bootstrap-vcpkg.bat')
        self.git('commit', '-q', '-m', 'SDK fixture inputs')
        self.revision = self.git('rev-parse', 'HEAD')

    def native_chain(self, target, destination):
        data = self.sdk / 'konan-data'
        for host, suffix in [('windows', '.exe'), ('linux', ''), ('macos', '')]:
            binary = data / 'dependencies' / ('llvm-21-x86_64-' + host + '-fixture') / 'bin'
            for name in ['clang', 'clang++', 'llvm-ar']:
                path = binary / (name + suffix)
                if not path.exists():
                    self.write(path, b'synthetic native tool; never executed\n')
        simulated = SimpleNamespace(name='nt' if target.startswith('windows') else 'posix',
                                    environ={'KONAN_DATA_DIR': str(data), 'PATH': ''}, pathsep=os.pathsep)
        def headers(gcc, directory):
            self.write(directory / 'bits/fs_path.h', b'patched header fixture\n')
            self.write(directory / 'intrin.h', b'patched intrinsic fixture\n')
            return directory
        simulated_subprocess = SimpleNamespace(run=lambda *a, **k: self.bootstrap_calls.append(a))
        with patch.object(toolchains, 'os', simulated), \
             patch.object(toolchains, 'subprocess', simulated_subprocess), \
             patch.object(toolchains, 'platform', SimpleNamespace(system=lambda: 'Linux', machine=lambda: 'x86_64')), \
             patch.object(toolchains, 'konan_home', return_value=data / 'owned-home'), \
             patch.object(toolchains, 'mingw_header_overlay', side_effect=headers):
            return toolchains.native_toolchain(target, destination)

    def emit(self):
        def stop(*arguments, **kwargs):
            self.calls.append(tuple(map(str, arguments)))
            if len(arguments) >= 2 and arguments[1] == 'install':
                raise InstallerMustNotRun()
            raise AssertionError('Unexpected producer command: ' + repr(arguments))
        args = SimpleNamespace(target=self.target, jobs=4, build_tools=None)
        with patch.object(producer, 'ROOT', self.root), patch.object(producer, 'BINARY', self.root / 'gdal4k-engine'), \
             patch.object(producer, 'VCPKG_REV', self.revision), patch.object(producer, 'run', side_effect=stop), \
             patch.object(producer, 'native_toolchain', side_effect=self.native_chain):
            with contextlib.redirect_stdout(__import__('io').StringIO()):
                try:
                    producer.build(args, self.vcpkg, self.out, 'fixture-fingerprint', 'cmake', 'ninja')
                except InstallerMustNotRun:
                    pass
                else:
                    raise AssertionError('Expected stop before installer execution')
        self.triplet = self.root / 'gdal4k-engine/build/triplets' / ('gdal4k-' + self.target + '.cmake')
        self.chain = self.out / 'toolchain.cmake' if self.target.startswith(('linux', 'windows')) else None
        return self

    def rebind(self, suffix=''):
        current = json.loads(no_time(self.out / 'sdk-toolchain-inputs.json'))
        template = current['tripletTemplate'] + suffix
        base = self.vcpkg / current['baseTriplet']
        with contextlib.redirect_stdout(__import__('io').StringIO()):
            text = cache.prepare_sdk_cache_inputs(self.root, self.vcpkg, self.out, base, self.chain,
                                                  self.target, template, self.revision)
        self.triplet.write_text(text)
        return self

    def identity(self):
        return (no_time(self.triplet), no_time(self.chain) if self.chain else b'',
                no_time(self.out / 'sdk-toolchain-inputs.json'))

    def abi_model(self, port='zlib', features=('core',), dependencies=(), compiler='synthetic-opaque-compiler-hash'):
        triplet, chain, extra = self.identity()
        return pinned_abi_input_model(triplet, chain, [extra],
            no_time(self.root / 'gdal4k-engine/ports' / port / 'portfile.cmake'), features, dependencies, compiler,
            'gdal4k-' + self.target)


class SdkCacheIdentityTest(unittest.TestCase):
    def fixture_pair(self, directory, target='linux-amd64'):
        return (Fixture(directory / 'original project', directory / 'sdk', target).emit(),
                Fixture(directory / 'different profile source identity', directory / 'sdk', target, True).emit())

    def test_actual_generated_identity_is_independent_of_checkout_and_fixture_roots(self):
        for target in ['windows-amd64', 'linux-amd64', 'linux-arm64', 'macos-arm64']:
            with self.subTest(target=target), tempfile.TemporaryDirectory(prefix='gdal4k-sdk-identity-') as tmp:
                first, second = self.fixture_pair(Path(tmp), target)
                self.assertEqual(first.identity(), second.identity())
                self.assertEqual(first.abi_model(), second.abi_model())
                self.assertEqual(no_time(first.out / 'sdk-source-inputs.json'), no_time(second.out / 'sdk-source-inputs.json'))
                self.assertNotIn(str(first.root).encode(), first.identity()[0] + first.identity()[2])
                self.assertNotIn(str(second.root).encode(), second.identity()[0] + second.identity()[2])
                self.assertIn('${VCPKG_ROOT_DIR}/triplets/', first.identity()[0].decode())

    def test_unrelated_product_tests_docs_and_fixture_urls_do_not_change_sdk_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            before = item.identity(), no_time(item.out / 'sdk-source-inputs.json')
            for name in ['README.md', 'gdal4k/native/runtime-md.hpp', 'verification/consumer.py', 'fixture-urls.json']:
                item.write(item.root / name, b'unrelated source or live fixture URL change\n')
            item.emit()
            self.assertEqual(before, (item.identity(), no_time(item.out / 'sdk-source-inputs.json')))

    def test_overlay_port_change_keeps_unrelated_configuration_and_port_abi_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            config, overall, gdal, zlib = item.identity(), no_time(item.out / 'sdk-source-inputs.json'), item.abi_model('gdal'), item.abi_model('zlib')
            item.write(item.root / 'gdal4k-engine/ports/gdal/portfile.cmake', b'changed GDAL patch source\n')
            item.emit()
            self.assertEqual(config, item.identity()); self.assertNotEqual(overall, no_time(item.out / 'sdk-source-inputs.json'))
            self.assertNotEqual(gdal, item.abi_model('gdal')); self.assertEqual(zlib, item.abi_model('zlib'))

    def test_features_and_dependency_abis_remain_selective_vcpkg_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            config, overall = item.identity(), no_time(item.out / 'sdk-source-inputs.json')
            item.write(item.root / 'gdal4k-engine/engine/vcpkg.json', b'{"dependencies":[{"name":"gdal","features":["arrow"]}]}\n')
            item.emit()
            self.assertEqual(config, item.identity()); self.assertNotEqual(overall, no_time(item.out / 'sdk-source-inputs.json'))
            self.assertNotEqual(item.abi_model('gdal'), item.abi_model('gdal', ('core', 'arrow')))
            self.assertNotEqual(item.abi_model('gdal', dependencies=(('fixture-sdk-package', 'before'),)),
                                item.abi_model('gdal', dependencies=(('fixture-sdk-package', 'after'),)))

    def test_revision_with_only_builtin_port_change_does_not_change_triplet_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            config, overall = item.identity(), no_time(item.out / 'sdk-source-inputs.json')
            item.write(item.vcpkg / 'ports/gdal/portfile.cmake', b'new builtin SDK port\n'); item.commit(); item.emit()
            self.assertEqual(config, item.identity()); self.assertNotEqual(overall, no_time(item.out / 'sdk-source-inputs.json'))

    def test_real_tool_pins_invalidate_configuration_without_global_port_revision_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            path = item.root / 'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties'
            before = item.identity()
            text = no_time(path).decode().replace('vcpkg.revision=3cd21e48a75f67acfef289da52893ce79144aaa1',
                'vcpkg.revision=0000000000000000000000000000000000000000')
            item.write(path, text.encode()); item.emit()
            self.assertEqual(before, item.identity())
            item.write(path, text.replace('cmake.version=4.4.4', 'cmake.version=4.4.5').encode()); item.emit()
            self.assertNotEqual(before, item.identity())

    def test_included_base_and_transitive_framework_changes_invalidate_configuration(self):
        for name in ['triplets/community/x64-linux.cmake', 'scripts/toolchains/fixture.cmake']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit(); before = item.abi_model()
                item.write(item.vcpkg / name, b'changed actual included or framework SDK bytes\n')
                with self.assertRaisesRegex(RuntimeError, 'unchanged tracked'): item.rebind()
                item.commit(); item.rebind()
                self.assertNotEqual(before, item.abi_model())

    def test_external_toolchain_and_target_flags_are_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit(); first = item.abi_model()
            item.chain.write_bytes(no_time(item.chain) + b'set(EXTRA_REAL_TOOLCHAIN_FLAG ON)\n'); item.rebind()
            self.assertNotEqual(first, item.abi_model()); second = item.abi_model()
            item.rebind('set(VCPKG_CXX_FLAGS "-freal-target-option")\n')
            self.assertNotEqual(second, item.abi_model())

    def test_generated_windows_header_overlay_is_explicitly_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk', 'windows-amd64').emit(); before = item.abi_model()
            item.write(item.out / 'compiler-headers/intrin.h', b'changed real generated header\n'); item.rebind()
            self.assertNotEqual(before, item.abi_model())

    def test_compiler_tracking_is_kept_and_local_ranlib_paths_are_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk', 'windows-amd64').emit()
            text = no_time(item.chain).decode()
            for name in ['CMAKE_RANLIB', 'CMAKE_C_COMPILER_RANLIB', 'CMAKE_CXX_COMPILER_RANLIB']:
                self.assertIn('set(' + name + ' "${CMAKE_CURRENT_LIST_DIR}/llvm-ranlib.exe")', text)
            self.assertIn('-femulated-tls', text)
            self.assertIn(r'-isystem \"${CMAKE_CURRENT_LIST_DIR}/compiler-headers\"', text)
            self.assertNotIn('VCPKG_DISABLE_COMPILER_TRACKING', no_time(item.triplet).decode())
            self.assertNotIn('VCPKG_PUBLIC_ABI_OVERRIDE', no_time(item.triplet).decode())
            self.assertNotEqual(item.abi_model(compiler='compiler A'), item.abi_model(compiler='compiler B'))

    def test_native_archiver_content_change_is_bound_and_stale_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk', 'windows-amd64').emit(); before = item.abi_model()
            path = next((item.sdk / 'konan-data/dependencies').glob('llvm-21-*-windows-*/bin/llvm-ar.exe'))
            item.write(path, b'changed archiver bytes\n')
            with self.assertRaisesRegex(RuntimeError, 'ranlib copy differs'): item.rebind()
            shutil.copy2(path, item.out / 'llvm-ranlib.exe'); item.rebind()
            self.assertNotEqual(before, item.abi_model())

    def test_selected_archiver_above_plaintext_budget_is_streamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            path = next((item.sdk / 'konan-data/dependencies').glob('llvm-21-*-linux-*/bin/llvm-ar'))
            with path.open('wb') as output:
                for _ in range(144): output.write(b'T' * 65536)
            item.rebind()
            tools = json.loads(no_time(item.out / 'sdk-toolchain-inputs.json'))['nativeAuxiliaryTools']
            self.assertEqual(9 * 1024 ** 2, tools[0]['bytes'])

    def test_existing_additional_files_are_appended_and_installer_paths_stay_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            item.rebind('list(APPEND VCPKG_HASH_ADDITIONAL_FILES "${VCPKG_ROOT_DIR}/existing.txt")\n')
            text = no_time(item.triplet).decode()
            self.assertIn('existing.txt', text); self.assertEqual(2, text.count('list(APPEND VCPKG_HASH_ADDITIONAL_FILES'))
            self.assertNotIn('set(VCPKG_HASH_ADDITIONAL_FILES', text)
            command = item.calls[-1]
            self.assertIn('--x-install-root=' + str(item.out / 'installed'), command)
            self.assertIn('--x-buildtrees-root=' + str(item.out / 'vcpkg-buildtrees'), command)
            self.assertIn('--x-packages-root=' + str(item.out / 'vcpkg-packages'), command)
            self.assertTrue(item.bootstrap_calls)

    def test_wrong_revision_links_and_oversized_sources_fail_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            with self.assertRaisesRegex(RuntimeError, 'revision differs'):
                cache.require_pinned_clean_vcpkg(item.vcpkg, '0' * 40)
            item.write(item.root / 'gdal4k-engine/ports/gdal/large', b'x' * 65)
            with patch.object(cache, 'FILE_LIMIT', 64), self.assertRaisesRegex(RuntimeError, 'bound'):
                item.rebind()
            (item.root / 'gdal4k-engine/ports/gdal/large').unlink()
            (item.root / 'gdal4k-engine/ports/gdal/linked').symlink_to(item.root / 'gdal4k-engine/engine/vcpkg.json')
            with self.assertRaisesRegex(RuntimeError, 'regular input'):
                item.rebind()

    def test_aggregate_source_budget_is_enforced_without_large_allocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            with patch.object(cache, 'SOURCE_LIMIT', 100), self.assertRaisesRegex(RuntimeError, 'source budget'):
                item.rebind()

    def test_windows_reparse_attributes_are_rejected_without_posix_metadata(self):
        value = SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ordinary'; path.write_bytes(b'ordinary')
            self.assertTrue(cache._linked(path, value))
            self.assertFalse(cache._linked(path, SimpleNamespace(st_mode=0o100644)))

    def test_unreadable_framework_enumeration_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Fixture(Path(tmp) / 'root', Path(tmp) / 'sdk').emit()
            def unreadable(*args, **kwargs):
                if kwargs.get('onerror'):
                    kwargs['onerror'](PermissionError('owned unreadable input counterfactual'))
                return iter(())
            with patch.object(cache.os, 'walk', unreadable), self.assertRaisesRegex(RuntimeError, 'Cannot enumerate'):
                item.rebind()


class StatIdentityTest(unittest.TestCase):
    def snapshot(self, **changes):
        fields = dict(st_dev=11, st_ino=22, st_mode=0o100666, st_size=8,
                      st_mtime_ns=1000, st_ctime_ns=2000, st_birthtime_ns=100)
        fields.update(changes)
        return SimpleNamespace(**fields)

    def test_documented_windows_execute_and_ctime_representations(self):
        for suffix in ('.exe', '.BAT', '.cmd', '.COM'):
            for mode in (0o100666, 0o100444):
                path = self.snapshot(st_mode=mode | 0o111, st_ctime_ns=100)
                opened = self.snapshot(st_mode=mode)
                with self.subTest(suffix=suffix, mode=mode), patch.object(cache, '_WINDOWS_STAT', True):
                    self.assertEqual([], cache._path_handle_changes(Path('input' + suffix), path, opened))
                self.assertIn('st_ctime_ns', cache._changed_fields(path, opened))

    def test_windows_join_keeps_device_inode_size_mtime_and_birthtime(self):
        path = self.snapshot(st_mode=0o100777, st_ctime_ns=100)
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            changed = self.snapshot(**{field: getattr(self.snapshot(), field) + 1})
            with self.subTest(field=field), patch.object(cache, '_WINDOWS_STAT', True):
                self.assertIn(field, cache._path_handle_changes(Path('input.cmd'), path, changed))
        with patch.object(cache, '_WINDOWS_STAT', True):
            self.assertIn('st_mode', cache._path_handle_changes(Path('input.cmd'), path, self.snapshot(st_mode=0o100444)))

    def test_execute_normalization_is_windows_and_suffix_specific(self):
        path = self.snapshot(st_mode=0o100777)
        with patch.object(cache, '_WINDOWS_STAT', False):
            self.assertIn('st_mode', cache._path_handle_changes(Path('input.exe'), path, self.snapshot()))
        with patch.object(cache, '_WINDOWS_STAT', True):
            self.assertIn('st_mode', cache._path_handle_changes(Path('input.txt'), path, self.snapshot()))
            self.assertIn('st_mode', cache._path_handle_changes(Path('input.exe'), self.snapshot(st_mode=0o100766), self.snapshot()))

    def test_ctime_normalization_requires_matching_explicit_birthtime(self):
        path = self.snapshot(st_ctime_ns=100)
        for opened in (self.snapshot(st_birthtime_ns=101), self.snapshot(st_birthtime_ns=None)):
            with patch.object(cache, '_WINDOWS_STAT', True):
                self.assertIn('st_ctime_ns', cache._path_handle_changes(Path('input.txt'), path, opened))
        with patch.object(cache, '_WINDOWS_STAT', True):
            self.assertIn('st_ctime_ns', cache._path_handle_changes(Path('input.txt'), self.snapshot(st_ctime_ns=101), self.snapshot()))
        self.assertIn('st_ctime_ns', cache._changed_fields(self.snapshot(), self.snapshot(st_ctime_ns=2001)))
        self.assertIn('st_birthtime_ns', cache._changed_fields(self.snapshot(), self.snapshot(st_birthtime_ns=101)))

    def test_hash_stream_accepts_documented_windows_cross_api_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.CMD'; path.write_bytes(b'original')
            actual = path.lstat()
            fields = {key: getattr(actual, key, None) for key in cache._FIELDS}
            before = SimpleNamespace(**{**fields, 'st_mode': actual.st_mode | 0o111, 'st_ctime_ns': 100, 'st_birthtime_ns': 100})
            opened = SimpleNamespace(**{**fields, 'st_ctime_ns': 200, 'st_birthtime_ns': 100})
            with patch.object(cache, '_WINDOWS_STAT', True), patch.object(Path, 'lstat', return_value=before), patch.object(cache.os, 'fstat', return_value=opened):
                self.assertEqual({'bytes': 8, 'sha256': digest(b'original')}, cache._hash_file(path, 8, 'vcpkg/scripts/input.CMD'))

    def test_tool_pins_accepts_documented_windows_ctime_representation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'versions.properties'
            path.write_bytes(b'cmake.version=3.31.6\nvcpkg.revision=source\n')
            fields = {key: getattr(path.lstat(), key, None) for key in cache._FIELDS}
            before = SimpleNamespace(**{**fields, 'st_ctime_ns': 100, 'st_birthtime_ns': 100})
            opened = SimpleNamespace(**{**fields, 'st_ctime_ns': 200, 'st_birthtime_ns': 100})
            with patch.object(cache, '_WINDOWS_STAT', True), patch.object(Path, 'lstat', return_value=before), patch.object(cache.os, 'fstat', return_value=opened):
                self.assertEqual({'cmake.version': '3.31.6'}, cache._tool_pins(path))

    def test_tool_pins_rejects_descriptor_change_with_unchanged_path_birthtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'versions.properties'; path.write_bytes(b'pin=old\n')
            fields = {key: getattr(path.lstat(), key, None) for key in cache._FIELDS}
            before = SimpleNamespace(**{**fields, 'st_ctime_ns': 100, 'st_birthtime_ns': 100})
            opened = SimpleNamespace(**{**fields, 'st_ctime_ns': 200, 'st_birthtime_ns': 100})
            after = SimpleNamespace(**{**fields, 'st_ctime_ns': 201, 'st_birthtime_ns': 100})
            with patch.object(cache, '_WINDOWS_STAT', True), patch.object(Path, 'lstat', return_value=before), patch.object(cache.os, 'fstat', side_effect=[opened, after]), self.assertRaisesRegex(RuntimeError, r'at descriptor.*st_ctime_ns'):
                cache._tool_pins(path)

    def assert_stream_change_rejected(self, replace, tool_pins=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'; path.write_bytes(b'pin=old\n' if tool_pins else b'original')
            before = path.stat(); original_open = cache.os.fdopen
            @contextlib.contextmanager
            def changed_open(fd, mode):
                with original_open(fd, mode) as stream:
                    class Changing:
                        changed = False
                        def fileno(self): return stream.fileno()
                        def read(self, size):
                            data = stream.read(size)
                            if not self.changed:
                                self.changed = True
                                if replace:
                                    replacement = path.with_name('replacement'); replacement.write_bytes(b'changed!')
                                    os.replace(replacement, path)
                                else:
                                    path.write_bytes(b'pin=new\n' if tool_pins else b'changed!')
                                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
                            return data
                    yield Changing()
            with patch.object(cache.os, 'fdopen', changed_open), self.assertRaisesRegex(RuntimeError, r'input .*st_(ino|mtime_ns|ctime_ns)'):
                if tool_pins: cache._tool_pins(path)
                else: cache._hash_file(path, 8, 'vcpkg/scripts/input')

    def test_content_mutation_during_stream_is_rejected(self):
        self.assert_stream_change_rejected(False)

    def test_path_replacement_during_stream_is_rejected(self):
        self.assert_stream_change_rejected(True)

    def test_tool_pins_content_mutation_is_rejected(self):
        self.assert_stream_change_rejected(False, tool_pins=True)


if __name__ == '__main__':
    unittest.main()
