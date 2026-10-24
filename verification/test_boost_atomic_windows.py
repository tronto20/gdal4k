"""Pinned Boost CMake usage-requirement regression, without an SDK or network.

Compiler-ID and platform cases below are synthetic CMake configuration inputs.
Actual target PUBLIC/interface and transitive consumer properties are evaluated
by CMake, without compiling Boost or establishing Windows runtime acceptance.
Git, CMake and the host's ordinary C++ compiler are required.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / 'gdal4k-engine/ports/boost-atomic'
FIXTURE = ROOT / 'verification/fixtures/boost-atomic-1.92.0'
ARCHIVE_SHA512 = ('6fd6ab907f6e1195bae0343d73cf192d353be85e89137b98c6cee38e1f73db9145'
                  '3290fb05d63e3194440c5f4cf50b1f5c29e332edceda26e2689005b2a14296')


def windows_block(source):
    """Extract the pinned library's complete, balanced Windows requirement block."""
    start = re.search(r'(?m)^if \(WIN32\)\n    target_link_libraries\(boost_atomic\n', source)
    if start is None:
        raise AssertionError('Pinned Boost.Atomic Windows target block is missing')
    lines = source[start.start():].splitlines(keepends=True)
    depth = 0
    for index, line in enumerate(lines):
        if re.match(r'\s*if\s*\(', line):
            depth += 1
        elif re.match(r'\s*endif\s*\(', line):
            depth -= 1
            if depth == 0:
                return ''.join(lines[:index + 1])
    raise AssertionError('Unbalanced pinned Boost.Atomic Windows target block')


class BoostAtomicWindowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = shutil.which('git')
        cls.cmake = shutil.which('cmake')
        cls.cxx = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
        if not all((cls.git, cls.cmake, cls.cxx)):
            raise RuntimeError('Git, CMake and a host C++ compiler are required')
        cls.original = (FIXTURE / 'CMakeLists.txt').read_text()
        with tempfile.TemporaryDirectory(prefix='gdal4k-atomic-patch-') as temporary:
            directory = Path(temporary)
            shutil.copyfile(FIXTURE / 'CMakeLists.txt', directory / 'CMakeLists.txt')
            # Apply the retained upstream vcpkg patch first, in actual port order.
            for patch in ['fix-include.patch', 'clang-mingw-winnt.patch']:
                subprocess.run([cls.git, 'apply', '--check', str(PORT / patch)],
                               cwd=directory, check=True, capture_output=True)
                subprocess.run([cls.git, 'apply', str(PORT / patch)],
                               cwd=directory, check=True, capture_output=True)
            cls.patched = (directory / 'CMakeLists.txt').read_text()
        if windows_block(cls.original) == windows_block(cls.patched):
            raise AssertionError('Git did not apply the Windows target patch')

    def test_fixtures_match_the_original_pinned_archive_and_port(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual('1.92.0', provenance['version'])
        self.assertEqual(ARCHIVE_SHA512, provenance['archive']['sha512'])
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, (PORT / 'portfile.cmake').read_text())
        self.assertEqual({'CMakeLists.txt'},
                         {member['path'] for member in provenance['members']})
        for member in provenance['members']:
            with self.subTest(member=member['path']):
                data = (FIXTURE / member['path']).read_bytes()
                self.assertEqual(member['bytes'], len(data))
                self.assertEqual(member['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual('atomic-boost-1.92.0/' + member['path'], member['archiveMember'])
        license_record = provenance['license']
        license_bytes = (FIXTURE / license_record['path']).read_bytes()
        self.assertEqual(license_record['bytes'], len(license_bytes))
        self.assertEqual(license_record['sha256'], hashlib.sha256(license_bytes).hexdigest())
        self.assertEqual('https://raw.githubusercontent.com/boostorg/boost/boost-1.92.0/LICENSE_1_0.txt',
                         license_record['url'])

    def test_overlay_changes_only_the_mingw_patch_selection_and_port_revision(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())['originalVcpkg']
        port = (PORT / 'portfile.cmake').read_text()
        prefix, original = port.split('# Automatically generated', 1)
        self.assertRegex(prefix, r'^set\((\w+)\)\s*if\(VCPKG_TARGET_IS_MINGW\)\s*'
                                r'list\(APPEND \1 clang-mingw-winnt\.patch\)\s*endif\(\)\s*$')
        variable = re.match(r'set\((\w+)\)', prefix).group(1)
        self.assertEqual(1, original.count('        ${' + variable + '}\n'))
        restored = '# Automatically generated' + original.replace('        ${' + variable + '}\n', '')
        self.assertEqual(provenance['portfileSha256'], hashlib.sha256(restored.encode()).hexdigest())
        self.assertEqual(provenance['fixIncludeSha256'],
                         hashlib.sha256((PORT / 'fix-include.patch').read_bytes()).hexdigest())
        manifest = json.loads((PORT / 'vcpkg.json').read_text())
        self.assertEqual(1, manifest.pop('port-version'))
        self.assertEqual(provenance['manifestCanonicalSha256'],
                         hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def test_actual_cmake_public_requirements_reach_the_clang_mingw_consumer(self):
        # One configure evaluates the real original/patched blocks against real
        # CMake targets. Only target names change to keep all cases in one graph.
        # Compiler/platform variables are synthetic, not cross-compiler claims.
        cases = [
            ('gnu_windows', True, True, 'GNU', True, True),
            ('clang_mingw', True, True, 'Clang', False, True),
            ('clang_nonmingw', True, False, 'Clang', False, False),
            ('msvc_windows', True, False, 'MSVC', False, False),
            ('gnu_posix', False, False, 'GNU', False, False),
            ('clang_posix', False, False, 'Clang', False, False),
        ]
        with tempfile.TemporaryDirectory(prefix='gdal4k-atomic-graph-') as temporary:
            directory = Path(temporary)
            (directory / 'empty.cpp').write_text('int atomic_graph_probe;\n')
            project = ['cmake_minimum_required(VERSION 3.20)\nproject(AtomicGraph LANGUAGES CXX)\n'
                       'add_library(Boost::winapi INTERFACE IMPORTED)\n']
            for variant, source in [('original', self.original), ('patched', self.patched)]:
                for label, windows, mingw, compiler, _, _ in cases:
                    name = variant + '_' + label
                    atomic, consumer = 'atomic_' + name, 'consumer_' + name
                    block = windows_block(source).replace('boost_atomic', atomic)
                    project.append('function(case_' + name + ')\n'
                                   f'set(WIN32 {"TRUE" if windows else "FALSE"})\n'
                                   f'set(MINGW {"TRUE" if mingw else "FALSE"})\n'
                                   f'set(CMAKE_CXX_COMPILER_ID "{compiler}")\n'
                                   f'add_library({atomic} STATIC empty.cpp)\n' + block +
                                   f'add_library({consumer} STATIC empty.cpp)\n'
                                   f'target_link_libraries({consumer} PRIVATE {atomic})\n'
                                   f'file(GENERATE OUTPUT "${{CMAKE_BINARY_DIR}}/{name}.txt" CONTENT '
                                   f'"$<TARGET_PROPERTY:{atomic},COMPILE_DEFINITIONS>\\n'
                                   f'$<TARGET_PROPERTY:{atomic},INTERFACE_COMPILE_DEFINITIONS>\\n'
                                   f'$<TARGET_PROPERTY:{consumer},COMPILE_DEFINITIONS>\\n'
                                   f'$<TARGET_PROPERTY:{atomic},INTERFACE_LINK_LIBRARIES>\\n")\n'
                                   'endfunction()\ncase_' + name + '()\n')
            (directory / 'CMakeLists.txt').write_text(''.join(project))
            build = directory / 'build'
            subprocess.run([self.cmake, '-S', str(directory), '-B', str(build),
                            '-DCMAKE_CXX_COMPILER=' + self.cxx], check=True, capture_output=True)
            for variant in ['original', 'patched']:
                for label, windows, _, _, before, after in cases:
                    with self.subTest(variant=variant, case=label):
                        lines = (build / (variant + '_' + label + '.txt')).read_text().splitlines()
                        self.assertEqual(4, len(lines))
                        expected = '_WIN32_WINNT=0x0A00' if (before if variant == 'original' else after) else ''
                        self.assertEqual([expected, expected, expected], lines[:3])
                        self.assertEqual('Boost::winapi;synchronization' if windows else '', lines[3])


if __name__ == '__main__':
    unittest.main()
