"""Real CMake parser checks on generated text; no compiler, header or SDK build.

The parser scratch contains exact upstream triplet text and generated CMake/JSON
only. No placeholder SDK executable or header is copied there or executed. SDK
counterfactual metadata used to generate the text is tested separately.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from test_sdk_cache_identity import Fixture, ROOT, no_time

FIXTURES = ROOT / 'verification/fixtures/vcpkg-sdk-triplets'


class SdkCacheCmakeParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake = os.environ.get('CMAKE_COMMAND') or shutil.which('cmake')
        if not cls.cmake:
            raise RuntimeError('A real CMake executable is required for SDK identity parser checks')
        cls.provenance = json.loads(no_time(FIXTURES / 'provenance.json'))

    def real_bases(self, directory, target):
        item = Fixture(directory / 'metadata source with spaces', directory / 'metadata-sdk', target)
        # These are newly owned synthetic SDK metadata, not retained SDK inputs.
        for path in (item.vcpkg / 'triplets').rglob('*.cmake'):
            path.unlink()
        for row in self.provenance['members']:
            item.write(item.vcpkg / row['path'], no_time(FIXTURES / row['path']))
        item.commit()
        return item.emit()

    def parse_copy(self, directory, item, extra=None):
        parser = directory / 'parser source with spaces'
        sdk = parser / 'pinned-vcpkg'
        for row in self.provenance['members']:
            item.write(sdk / row['path'], no_time(FIXTURES / row['path']))
        triplet = parser / 'gdal4k-engine/build/triplets' / item.triplet.name
        out = parser / 'gdal4k-engine/build/engine' / item.target
        item.write(triplet, no_time(item.triplet))
        item.write(out / 'sdk-toolchain-inputs.json', no_time(item.out / 'sdk-toolchain-inputs.json'))
        if item.chain:
            item.write(out / 'toolchain.cmake', no_time(item.chain))
        if extra is not None:
            item.write(sdk / 'existing.txt', extra)
        script = parser / 'observe.cmake'
        observed = parser / 'observed.txt'
        quote = lambda p: str(p).replace('\\', '/')
        script.write_text('set(VCPKG_ROOT_DIR "' + quote(sdk) + '")\n'
            'set(PORT gdal)\ninclude("' + quote(triplet) + '")\n'
            'if(VCPKG_CHAINLOAD_TOOLCHAIN_FILE)\n include("${VCPKG_CHAINLOAD_TOOLCHAIN_FILE}")\nendif()\n'
            'foreach(input IN LISTS VCPKG_HASH_ADDITIONAL_FILES)\n'
            ' if(NOT IS_ABSOLUTE "${input}" OR NOT EXISTS "${input}")\n'
            '  message(FATAL_ERROR "Additional SDK input is not an existing absolute file")\n endif()\n'
            ' file(SHA256 "${input}" input_hash)\n list(APPEND input_hashes "${input_hash}")\nendforeach()\n'
            'file(WRITE "' + quote(observed) + '" '
            '"${VCPKG_BUILD_TYPE}\\n${VCPKG_LIBRARY_LINKAGE}\\n${VCPKG_TARGET_ARCHITECTURE}\\n'
            '${VCPKG_CMAKE_SYSTEM_NAME}\\n${CMAKE_SYSTEM_NAME}\\n${CMAKE_SYSTEM_PROCESSOR}\\n'
            '${CMAKE_CXX_FLAGS_INIT}\\n${CMAKE_RANLIB}\\n${VCPKG_CHAINLOAD_TOOLCHAIN_FILE}\\n'
            '${VCPKG_HASH_ADDITIONAL_FILES}\\n${input_hashes}\\n${VCPKG_CMAKE_CONFIGURE_OPTIONS}\\n'
            '${VCPKG_OSX_DEPLOYMENT_TARGET}\\n")\n', encoding='utf-8')
        result = subprocess.run([self.cmake, '-P', str(script)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        rows = observed.read_text().splitlines()
        self.assertEqual(13, len(rows))
        self.assertFalse((out / 'compiler-headers').exists())
        self.assertFalse((out / 'llvm-ranlib').exists())
        self.assertFalse((out / 'llvm-ranlib.exe').exists())
        return rows, out, sdk

    def test_base_fixtures_are_exact_pinned_git_blobs(self):
        self.assertEqual('3cd21e48a75f67acfef289da52893ce79144aaa1', self.provenance['revision'])
        self.assertEqual(4, len(self.provenance['members']))
        for row in self.provenance['members']:
            raw = no_time(FIXTURES / row['path'])
            self.assertEqual(row['bytes'], len(raw))
            self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row['gitBlob'], hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest())

    def test_real_parser_resolves_includes_flags_and_auxiliary_paths_for_four_targets(self):
        for target, arch, system, processor in [('windows-amd64', 'x64', 'Windows', 'AMD64'),
                ('linux-amd64', 'x64', 'Linux', 'x86_64'), ('linux-arm64', 'arm64', 'Linux', 'aarch64'),
                ('macos-arm64', 'arm64', '', '')]:
            with self.subTest(target=target), tempfile.TemporaryDirectory(prefix='gdal4k-sdk-cmake-') as tmp:
                directory = Path(tmp);item = self.real_bases(directory, target)
                rows, out, sdk = self.parse_copy(directory, item)
                self.assertEqual(['release', 'static', arch], rows[:3])
                self.assertEqual([system, processor], rows[4:6])
                self.assertEqual(hashlib.sha256(no_time(out / 'sdk-toolchain-inputs.json')).hexdigest(), rows[10])
                self.assertEqual((out / 'sdk-toolchain-inputs.json').resolve(), Path(rows[9]).resolve())
                self.assertIn('-DCMAKE_POSITION_INDEPENDENT_CODE=ON', rows[11])
                if target.startswith('windows'):
                    self.assertEqual(out / 'llvm-ranlib.exe', Path(rows[7]))
                    flags = shlex.split(rows[6])
                    self.assertEqual(str(out / 'compiler-headers'), flags[flags.index('-isystem') + 1])
                    self.assertIn('-femulated-tls', flags)
                elif target.startswith('linux'):
                    self.assertEqual(out / 'llvm-ranlib', Path(rows[7]))
                    self.assertIn('-DGDAL_OBJECT_LIBRARIES_POSITION_INDEPENDENT_CODE=ON', rows[11])
                else:
                    self.assertEqual('Darwin', rows[3]);self.assertEqual('11.0', rows[12])

    def test_real_parser_preserves_existing_additional_inputs_and_hashes_all_contents(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-sdk-cmake-append-') as tmp:
            directory = Path(tmp);item = self.real_bases(directory, 'macos-arm64')
            item.rebind('list(APPEND VCPKG_HASH_ADDITIONAL_FILES "${VCPKG_ROOT_DIR}/existing.txt")\n')
            existing = b'owned metadata input that predates our manifest\n'
            rows, out, sdk = self.parse_copy(directory, item, existing)
            self.assertEqual([(sdk / 'existing.txt').resolve(), (out / 'sdk-toolchain-inputs.json').resolve()],
                             [Path(value).resolve() for value in rows[9].split(';')])
            self.assertEqual([hashlib.sha256(existing).hexdigest(), hashlib.sha256(no_time(out / 'sdk-toolchain-inputs.json')).hexdigest()], rows[10].split(';'))


if __name__ == '__main__':
    unittest.main()
