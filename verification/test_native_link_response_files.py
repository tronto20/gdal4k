"""Read the actual CMake/Ninja static closure after response files are removed."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / 'gdal4k-engine/tools'
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location('response_file_producer', TOOLS / 'build-engine.py')
producer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(producer)


class NativeLinkResponseFilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake, cls.ninja = shutil.which('cmake'), shutil.which('ninja')
        if not cls.cmake or not cls.ninja:
            raise RuntimeError('CMake and Ninja are required')

    def command(self, *args):
        result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_built_link_closure_survives_removed_response_files(self):
        for forced in [False, True]:
            with self.subTest(forced=forced), tempfile.TemporaryDirectory(prefix='gdal4k link closure ') as temporary:
                root = Path(temporary)
                source, build, installed = root / 'source', root / 'build', root / 'installed'
                source.mkdir()
                (source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.22)
project(ResponseFileClosure C)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
foreach(name gdal proj jxl transitive irrelevant)
  add_library(${name} STATIC ${name}.c)
  set_target_properties(${name} PROPERTIES ARCHIVE_OUTPUT_DIRECTORY "${INSTALL_ROOT}/lib with spaces")
endforeach()
target_link_libraries(gdal PUBLIC proj jxl transitive)
add_library(gdal4k SHARED bridge.c)
set_target_properties(gdal4k PROPERTIES PREFIX "" SUFFIX ".dll")
target_link_libraries(gdal4k PRIVATE gdal)
if(UNIX)
  target_link_libraries(gdal4k PRIVATE m)
endif()
''', encoding='utf-8')
                for name, body in {
                    'gdal': 'int proj(void); int jxl(void); int transitive(void); int gdal(void) { return proj()+jxl()+transitive(); }',
                    'proj': 'int proj(void) { return 1; }',
                    'jxl': 'int jxl(void) { return 2; }',
                    'transitive': 'int transitive(void) { return 3; }',
                    'irrelevant': 'int irrelevant(void) { return 100; }',
                    'bridge': 'int gdal(void); int bridge(void) { return gdal(); }',
                }.items():
                    (source / (name + '.c')).write_text(body + '\n', encoding='utf-8')
                options = ['-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON'] if forced else []
                self.command(self.cmake, '-S', source, '-B', build, '-G', 'Ninja',
                             '-DCMAKE_MAKE_PROGRAM=' + self.ninja, '-DINSTALL_ROOT=' + str(installed), *options)
                self.command(self.cmake, '--build', build)
                self.assertTrue((build / 'gdal4k.dll').is_file())
                self.assertFalse(list(build.rglob('*.rsp')))
                old_commands = self.command(self.ninja, '-C', build, '-t', 'commands', 'gdal4k')
                old_line = next(line for line in reversed(old_commands.splitlines()) if ' -o gdal4k.dll ' in line)
                old_arguments = producer.link_arguments(old_line)
                arguments = producer.ninja_link_arguments(self.ninja, build, 'gdal4k.dll')
                def closure(tokens):
                    paths = [Path(token) if Path(token).is_absolute() else build / token
                             for token in tokens if token.endswith('.a')]
                    return {path for path in paths if path.is_relative_to(installed)}
                expected = {installed / 'lib with spaces' / ('lib' + name + '.a')
                            for name in ['gdal', 'proj', 'jxl', 'transitive']}
                self.assertEqual(expected, closure(arguments))
                self.assertTrue(all(path.is_file() for path in expected))
                self.assertTrue((installed / 'lib with spaces/libirrelevant.a').is_file())
                if sys.platform != 'win32':
                    self.assertIn('-lm', arguments)
                if forced:
                    self.assertTrue(any(token.startswith('@') for token in old_arguments))
                    self.assertEqual(set(), closure(old_arguments))
                else:
                    self.assertEqual(expected, closure(old_arguments))

    def test_exact_output_and_directory_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            entry = {'directory': str(build), 'output': 'libgdal4k.so', 'command': 'cc -o libgdal4k.so -lm'}
            for other in [dict(entry, output='libgdal4k.so.debug'),
                          dict(entry, directory=str(build / 'other'))]:
                with self.subTest(other=other), patch.object(producer.subprocess, 'check_output', return_value=json.dumps([other])):
                    with self.assertRaisesRegex(RuntimeError, 'found 0'):
                        producer.ninja_link_arguments('ninja', build, 'libgdal4k.so')
            with patch.object(producer.subprocess, 'check_output', return_value=json.dumps([entry, entry])):
                with self.assertRaisesRegex(RuntimeError, 'found 2'):
                    producer.ninja_link_arguments('ninja', build, 'libgdal4k.so')

    def test_unexpanded_response_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            entry = {'directory': str(build), 'output': 'libgdal4k.dylib',
                     'command': 'cc -o libgdal4k.dylib @CMakeFiles/gdal4k.rsp'}
            with patch.object(producer.subprocess, 'check_output', return_value=json.dumps([entry])):
                with self.assertRaisesRegex(RuntimeError, 'Unexpanded Ninja response file'):
                    producer.ninja_link_arguments('ninja', build, 'libgdal4k.dylib')

    def test_windows_shell_wrapper_keeps_quoted_archives_and_system_options(self):
        arguments = producer.link_arguments('cmd.exe /C "cd . && clang++ -shared -o gdal4k.dll '
                                             '"C:\\producer dir\\installed\\libgdal.a" -lws2_32 && cd ."')
        self.assertIn('C:/producer dir/installed/libgdal.a', arguments)
        self.assertIn('-lws2_32', arguments)

    def test_apple_loader_paths_and_frameworks_are_not_response_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            command = ('clang++ -dynamiclib -o libgdal4k.dylib -install_name @rpath/libgdal4k.dylib '
                       '-rpath @loader_path/ -rpath @executable_path/ -framework Security -framework CoreFoundation -lm')
            entry = {'directory': str(build), 'output': 'libgdal4k.dylib', 'command': command}
            with patch.object(producer.subprocess, 'check_output', return_value=json.dumps([entry])):
                self.assertEqual(producer.link_arguments(command),
                                 producer.ninja_link_arguments('ninja', build, 'libgdal4k.dylib'))


if __name__ == '__main__':
    unittest.main()
