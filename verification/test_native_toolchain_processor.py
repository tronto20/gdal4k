"""Generated toolchain -> actual CMake -> official Arrow CPU-selection regression.

Only Kotlin/Native bootstrap and SDK files/headers are synthetic. CMake loads the
real generated toolchain and executes an unchanged balanced upstream prefix.
LANGUAGES NONE requires no SDK/compiler/network and proves configuration only,
not compiler feature/SIMD checks, an Arrow build or Windows runtime acceptance.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'verification/fixtures/arrow-21.0.0-processor'
spec = importlib.util.spec_from_file_location('processor_toolchains', ROOT / 'gdal4k-engine/tools/toolchains.py')
toolchains = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolchains)
ARCHIVE_SHA512 = ('89da6de7eb2513c797d6671e1addf40b8b156215b481cf2511fa69faa16547c52'
                  'd8220727626eeda499e4384d276e03880cd920aaab41c3d15106743d51a90a6')


def processor_prefix(source):
    """Keep the complete original CPU selector, its includes and line numbers."""
    marker = '# Check architecture specific compiler flags\n'
    if source.count(marker) != 1:
        raise AssertionError('Pinned Arrow processor-prefix boundary changed')
    prefix = source.split(marker, 1)[0]
    depth = 0
    for line in prefix.splitlines():
        if re.match(r'\s*if\s*\(', line):
            depth += 1
        elif re.match(r'\s*endif\s*\(', line):
            depth -= 1
            if depth < 0:
                raise AssertionError('Unbalanced pinned Arrow CPU selector')
    if depth or prefix.count('message(FATAL_ERROR "Unknown system processor")') != 1:
        raise AssertionError('Pinned Arrow CPU selector is incomplete')
    return prefix


class NativeToolchainProcessorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake = shutil.which('cmake')
        if not cls.cmake:
            raise RuntimeError('CMake is required')
        cls.prefix = processor_prefix((FIXTURE / 'SetupCxxFlags.cmake').read_text(encoding='utf-8'))

    def generated_chain(self, directory, target):
        data = directory / 'konan-data'
        dependencies = data / 'dependencies'
        for host, suffix in [('windows', '.exe'), ('linux', '')]:
            binary = dependencies / ('llvm-21-x86_64-' + host + '-fixture') / 'bin'
            binary.mkdir(parents=True)
            for name in ['clang', 'clang++', 'llvm-ar']:
                (binary / (name + suffix)).write_bytes(b'unit SDK placeholder; never executed\n')
        environment = {'KONAN_DATA_DIR': str(data), 'PATH': ''}
        simulated = SimpleNamespace(name='nt' if target.startswith('windows') else 'posix',
                                    environ=environment, pathsep=os.pathsep)
        def headers(gcc, destination):
            destination.mkdir(parents=True)
            return destination
        destination = directory / 'toolchain.cmake'
        with patch.object(toolchains, 'os', simulated), \
                patch.object(toolchains, 'konan_home', return_value=data / 'owned-home'), \
                patch.object(toolchains.subprocess, 'run') as bootstrap, \
                patch.object(toolchains, 'mingw_header_overlay', side_effect=headers), \
                patch.object(toolchains.platform, 'system', return_value='Linux'), \
                patch.object(toolchains.platform, 'machine', return_value='x86_64'):
            result = toolchains.native_toolchain(target, destination)
            bootstrap.assert_called_once()
            self.assertIn('-Xcheck-dependencies', bootstrap.call_args.args[0])
            self.assertEqual(destination, result)
        return destination

    def configure(self, directory, chain):
        source = directory / 'project'
        source.mkdir()
        (source / 'SetupCxxFlags.cmake').write_text(self.prefix, encoding='utf-8')
        (source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\n'
                                             'project(ArrowProcessorRegression LANGUAGES NONE)\n'
                                             'include(SetupCxxFlags.cmake)\n'
                                             'file(WRITE "${CMAKE_BINARY_DIR}/observed.txt" '
                                             '"${CMAKE_SYSTEM_NAME}\\n${CMAKE_SYSTEM_PROCESSOR}\\n${ARROW_CPU_FLAG}\\n")\n',
                                             encoding='utf-8')
        build = directory / 'build'
        result = subprocess.run([self.cmake, '-S', str(source), '-B', str(build),
                                 '-DCMAKE_TOOLCHAIN_FILE=' + str(chain)], capture_output=True, text=True)
        observed = (build / 'observed.txt').read_text().splitlines() if (build / 'observed.txt').exists() else None
        return result, observed

    def test_fixtures_are_exact_official_overlay_archive_inputs(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual('21.0.0', provenance['version'])
        self.assertEqual(ARCHIVE_SHA512, provenance['archive']['sha512'])
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, (ROOT / 'gdal4k-engine/ports/arrow/portfile.cmake').read_text())
        self.assertEqual({'SetupCxxFlags.cmake', 'LICENSE.txt', 'NOTICE.txt'},
                         {member['path'] for member in provenance['members']})
        for member in provenance['members']:
            data = (FIXTURE / member['path']).read_bytes()
            self.assertEqual(member['bytes'], len(data))
            self.assertEqual(member['sha256'], hashlib.sha256(data).hexdigest())
        prefix = self.prefix.encode()
        self.assertEqual(provenance['processorPrefix']['bytes'], len(prefix))
        self.assertEqual(provenance['processorPrefix']['sha256'], hashlib.sha256(prefix).hexdigest())

    def test_actual_windows_chain_reaches_arrow_x86_and_original_blank_fails(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-windows-processor-') as temporary:
            directory = Path(temporary)
            current = directory / 'current'
            current.mkdir()
            chain = self.generated_chain(current, 'windows-amd64')
            result, observed = self.configure(current, chain)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(['Windows', 'AMD64', 'x86'], observed)
            original = directory / 'original'
            original.mkdir()
            old_chain = original / 'toolchain.cmake'
            text, removed = re.subn(r'(?m)^set\(CMAKE_SYSTEM_PROCESSOR [^\n)]*\)\n', '',
                                   chain.read_text(encoding='utf-8'))
            self.assertEqual(1, removed)
            old_chain.write_text(text, encoding='utf-8')
            result, observed = self.configure(original, old_chain)
            self.assertNotEqual(0, result.returncode)
            self.assertIsNone(observed)
            self.assertIn('System processor: \n', result.stdout)
            self.assertIn('SetupCxxFlags.cmake:44', result.stderr)
            self.assertIn('Unknown system processor', result.stderr)

    def test_existing_generated_linux_processors_keep_their_arrow_cpu_selection(self):
        for target, processor, cpu in [('linux-amd64', 'x86_64', 'x86'),
                                        ('linux-arm64', 'aarch64', 'aarch64')]:
            with self.subTest(target=target), tempfile.TemporaryDirectory(prefix='gdal4k-linux-processor-') as temporary:
                directory = Path(temporary)
                chain = self.generated_chain(directory, target)
                result, observed = self.configure(directory, chain)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(['Linux', processor, cpu], observed)


if __name__ == '__main__':
    unittest.main()
