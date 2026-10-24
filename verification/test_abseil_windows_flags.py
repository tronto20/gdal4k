"""Actual CMake option/compile regression, without a Windows SDK or network.

Target selection is synthetic; the selected versioned port supplies the actual
options and hook. Host C++ objects require inherited toolchain include/define
sentinels. This is not an Abseil build or Windows runtime acceptance.
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
PORT = ROOT / 'gdal4k-engine/ports/abseil'
FIXTURE = ROOT / 'verification/fixtures/abseil-20260107.1'
MACRO = '____FIReference_1_boolean_INTERFACE_DEFINED__'
LEGACY_OPTION = '-DCMAKE_CXX_FLAGS=-D' + MACRO
HOOK_OPTION = '-DCMAKE_PROJECT_absl_INCLUDE=${CMAKE_CURRENT_LIST_DIR}/mingw-project-include.cmake'
ARCHIVE_SHA512 = ('f5012885d6b6844a9cf5ed92ad5468b8757db33dfe1364bfb232fff928e06c550c'
                  '7eb4557f45186a8ac4d18b178df9be267681abab4a6de40823b574afbe9960')


class AbseilWindowsFlagsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake = shutil.which('cmake')
        cls.cxx = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
        if not cls.cmake or not cls.cxx:
            raise RuntimeError('CMake and a host C++ compiler are required')
        source = (FIXTURE / 'CMakeLists.txt').read_text(encoding='utf-8')
        declarations = re.findall(r'(?m)^project\(absl LANGUAGES CXX VERSION [0-9]+\)$', source)
        if len(declarations) != 1:
            raise AssertionError('Exact upstream project(absl) declaration is required')
        cls.project_declaration = declarations[0]

    def selected_options(self, directory, mingw):
        """Execute the selected port, replacing only vcpkg side-effect commands."""
        capture = directory / 'capture.cmake'
        options_file = directory / 'options.txt'
        capture.write_text('cmake_minimum_required(VERSION 3.20)\n'
                           'set(VCPKG_TARGET_IS_WINDOWS ' + ('TRUE' if mingw else 'FALSE') + ')\n'
                           'set(VCPKG_TARGET_IS_MINGW ' + ('TRUE' if mingw else 'FALSE') + ')\n'
                           'set(VCPKG_CRT_LINKAGE dynamic)\nset(VCPKG_LIBRARY_LINKAGE static)\n'
                           'set(CURRENT_PACKAGES_DIR "' + (directory / 'packages').as_posix() + '")\n'
                           'function(vcpkg_from_github)\n'
                           '  set(SOURCE_PATH "unused-offline-source" PARENT_SCOPE)\nendfunction()\n'
                           'function(vcpkg_cmake_configure)\n'
                           '  cmake_parse_arguments(P "" "SOURCE_PATH" "OPTIONS" ${ARGN})\n'
                           '  string(REPLACE ";" "\\n" options "${P_OPTIONS}")\n'
                           '  file(WRITE "' + options_file.as_posix() + '" "${options}\\n")\n'
                           'endfunction()\n'
                           'foreach(command vcpkg_check_linkage vcpkg_cmake_install '
                           'vcpkg_cmake_config_fixup vcpkg_fixup_pkgconfig vcpkg_copy_pdbs '
                           'vcpkg_install_copyright)\n'
                           '  function(${command})\n  endfunction()\nendforeach()\n'
                           'include("' + (PORT / 'portfile.cmake').as_posix() + '")\n', encoding='utf-8')
        subprocess.run([self.cmake, '-P', str(capture)], check=True, capture_output=True)
        return options_file.read_text(encoding='utf-8').splitlines()

    def compile_case(self, directory, options, project='absl', expect_fi=True):
        source = directory / 'source'
        source.mkdir()
        (source / 'child').mkdir()
        headers = directory / 'toolchain-headers'
        headers.mkdir()
        (headers / 'toolchain-required.h').write_text('#define TOOLCHAIN_HEADER_REQUIRED 23\n', encoding='utf-8')
        (source / 'probe.cpp').write_text('''#include "toolchain-required.h"
#if GDAL4K_ABSL_TOOLCHAIN_REQUIRED != 17 || TOOLCHAIN_HEADER_REQUIRED != 23
#error Toolchain definition and include must reach this object
#endif
#ifndef GDAL4K_ABSL_COMMON_HOOK_REQUIRED
#error Existing common project hook must remain active
#endif
#if EXPECT_FI
#ifndef ____FIReference_1_boolean_INTERFACE_DEFINED__
#error Selected MinGW Abseil workaround must reach this object
#endif
#else
#ifdef ____FIReference_1_boolean_INTERFACE_DEFINED__
#error Abseil MinGW workaround escaped its selected project
#endif
#endif
int abseil_port_flags_probe;
''', encoding='utf-8')
        declaration = self.project_declaration.replace('project(absl ', 'project(' + project + ' ')
        (source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\n' + declaration + '\n'
                                             'add_compile_definitions(EXPECT_FI=' + str(int(expect_fi)) + ')\n'
                                             'add_library(root_probe STATIC probe.cpp)\n'
                                             'add_subdirectory(child)\n'
                                             'file(WRITE "${CMAKE_BINARY_DIR}/flags.txt" "${CMAKE_CXX_FLAGS}")\n',
                                             encoding='utf-8')
        (source / 'child/CMakeLists.txt').write_text('add_library(child_probe STATIC ../probe.cpp)\n', encoding='utf-8')
        toolchain = directory / 'toolchain.cmake'
        toolchain.write_text('set(CMAKE_CXX_FLAGS_INIT "-DGDAL4K_ABSL_TOOLCHAIN_REQUIRED=17 -I' +
                             headers.as_posix() + '")\n', encoding='utf-8')
        common = directory / 'common-project-include.cmake'
        common.write_text('add_compile_definitions(GDAL4K_ABSL_COMMON_HOOK_REQUIRED)\n', encoding='utf-8')
        build = directory / 'build'
        subprocess.run([self.cmake, '-S', str(source), '-B', str(build),
                        '-DCMAKE_CXX_COMPILER=' + self.cxx, '-DCMAKE_TOOLCHAIN_FILE=' + str(toolchain),
                        '-DCMAKE_PROJECT_INCLUDE=' + str(common), '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', *options],
                       check=True, capture_output=True)
        result = subprocess.run([self.cmake, '--build', str(build), '--parallel', '2'], capture_output=True)
        commands = json.loads((build / 'compile_commands.json').read_text(encoding='utf-8'))
        self.assertEqual(2, len(commands))
        for entry in commands:
            command = entry['command']
            self.assertIn('-DGDAL4K_ABSL_COMMON_HOOK_REQUIRED', command)
            self.assertEqual(expect_fi, '-D' + MACRO in command)
        return result, (build / 'flags.txt').read_text(encoding='utf-8')

    def test_exact_official_fixtures_and_lf_normalized_original_port_inputs(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual('20260107.1', provenance['version'])
        self.assertEqual(ARCHIVE_SHA512, provenance['archive']['sha512'])
        for member in provenance['members']:
            data = (FIXTURE / member['path']).read_bytes()
            self.assertEqual(member['bytes'], len(data))
            self.assertEqual(member['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual('abseil-cpp-20260107.1/' + member['path'], member['archiveMember'])
        baseline = provenance['originalVcpkg']
        self.assertEqual('3cd21e48a75f67acfef289da52893ce79144aaa1', baseline['revision'])
        port = (PORT / 'portfile.cmake').read_text(encoding='utf-8')
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, port)
        self.assertEqual(1, port.count(HOOK_OPTION))
        restored = port.replace(HOOK_OPTION, LEGACY_OPTION)
        self.assertEqual(baseline['files']['portfile.cmake']['normalizedLfSha256'],
                         hashlib.sha256(restored.encode()).hexdigest())
        for name, record in baseline['files'].items():
            self.assertRegex(record['rawGitBlobSha1'], r'^[0-9a-f]{40}$')
            if name not in ['portfile.cmake', 'vcpkg.json']:
                self.assertEqual(record['normalizedLfSha256'], hashlib.sha256((PORT / name).read_bytes()).hexdigest())
        manifest = json.loads((PORT / 'vcpkg.json').read_text(encoding='utf-8'))
        self.assertEqual(4, manifest['port-version'])
        manifest['port-version'] = baseline['portVersion']
        original = json.dumps(manifest, indent=2) + '\n'
        self.assertEqual(baseline['files']['vcpkg.json']['normalizedLfSha256'], hashlib.sha256(original.encode()).hexdigest())

    def test_selected_mingw_options_preserve_required_toolchain_and_common_hook(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-abseil-flags-') as temporary:
            directory = Path(temporary)
            options = self.selected_options(directory, mingw=True)
            hook = '-DCMAKE_PROJECT_absl_INCLUDE=' + str(PORT / 'mingw-project-include.cmake')
            self.assertIn(hook, options)
            self.assertIn('-DLIBRT=LIBRT-NOTFOUND', options)
            self.assertFalse(any(option.startswith('-DCMAKE_CXX_FLAGS=') for option in options))
            fixed = directory / 'fixed'
            fixed.mkdir()
            result, flags = self.compile_case(fixed, options)
            self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertIn('-DGDAL4K_ABSL_TOOLCHAIN_REQUIRED=17', flags)
            self.assertIn('-I', flags)
            original = directory / 'original'
            original.mkdir()
            result, flags = self.compile_case(original, [LEGACY_OPTION if option == hook else option for option in options])
            self.assertNotEqual(0, result.returncode, 'Original clobber must fail the required include/define compile')
            self.assertNotIn('GDAL4K_ABSL_TOOLCHAIN_REQUIRED', flags)
            self.assertIn('toolchain-required.h', (result.stdout + result.stderr).decode(errors='replace'))

    def test_hook_is_limited_to_selected_mingw_and_upstream_absl_project(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-abseil-scope-') as temporary:
            directory = Path(temporary)
            options = self.selected_options(directory, mingw=False)
            self.assertFalse(any('CMAKE_PROJECT_absl_INCLUDE' in option for option in options))
            plain = directory / 'plain'
            plain.mkdir()
            result, _ = self.compile_case(plain, options, expect_fi=False)
            self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode(errors='replace'))
            options = self.selected_options(directory, mingw=True)
            other = directory / 'other'
            other.mkdir()
            result, _ = self.compile_case(other, options, project='other_project', expect_fi=False)
            self.assertEqual(0, result.returncode, (result.stdout + result.stderr).decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
