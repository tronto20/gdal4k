"""Use the exact engine compiler when a Linux toolchain omits its CMake cache entry."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('arrow_ipc',Path(__file__).parent/'driver-profile/arrow-ipc-check.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ArrowIpcToolchainTest(unittest.TestCase):
    def test_all_framework_pairs_reach_compiler_in_order(self):
        self.assertEqual(module.compiler_linker_options([
            '-lresolv', '-framework Security', '-lm',
            '-framework CoreFoundation', '-framework CoreServices',
        ]), [
            '-lresolv', '-framework', 'Security', '-lm',
            '-framework', 'CoreFoundation', '-framework', 'CoreServices',
        ])

    def test_non_framework_arguments_are_preserved_verbatim(self):
        options = ['-L/toolchain with spaces/lib', '/archive with spaces/libarrow.a',
                   '-Wl,-rpath,/path with spaces', 'literal;$(not-a-shell-command)', '-lpthread']
        self.assertEqual(module.compiler_linker_options(options), options)
        self.assertEqual(module.compiler_linker_options([]), [])

    def test_invalid_metadata_is_rejected(self):
        for options in [None, '-lm', {'-lm': True}, [None], [''], ['-lm\0other']]:
            with self.subTest(options=options), self.assertRaisesRegex(AssertionError, 'Invalid Native'):
                module.compiler_linker_options(options)

    def test_malformed_grouped_framework_is_rejected(self):
        for option in ['-framework', '-framework ', '-framework  Security',
                       '-framework Security -lm', '-framework ../Security', '-framework Security\n']:
            with self.subTest(option=option), self.assertRaisesRegex(AssertionError, 'Invalid grouped'):
                module.compiler_linker_options([option])

    def test_main_passes_expanded_framework_argv_without_splitting_paths(self):
        with tempfile.TemporaryDirectory(prefix='IPC toolchain ') as temporary:
            root = Path(temporary)
            compiler = root / 'compiler with spaces'; compiler.touch()
            engine = root / 'gdal4k-engine/build/engine/macos-arm64'
            cache = engine / 'cmake/CMakeCache.txt'; cache.parent.mkdir(parents=True)
            cache.write_text('CMAKE_CXX_COMPILER:FILEPATH=' + str(compiler) + '\n')
            archive = engine / 'libgdal4k.a'; archive.write_bytes(b'test archive')
            header = engine / 'installed/gdal4k-macos-arm64/include/arrow/ipc/api.h'
            header.parent.mkdir(parents=True); header.touch()
            source = root / 'verification/driver-profile/arrow-ipc-check.cpp'
            source.parent.mkdir(parents=True); source.write_text('test source')
            options = engine / 'native-linker-options.json'
            options.write_text('["-lm", "-framework Security", "-framework CoreFoundation", "-framework CoreServices"]')
            completed = Mock(stdout='GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK\n', stderr='')
            with patch.object(module.sys, 'argv', ['arrow-ipc-check.py', str(root), 'macos-arm64']), \
                 patch.object(module.subprocess, 'run', return_value=completed) as run, \
                 patch('builtins.print'):
                module.main()
            command = run.call_args_list[0].args[0]
            self.assertEqual(command[0], str(compiler))
            self.assertIn(str(source), command)
            self.assertIn(str(archive), command)
            start = command.index('-lm')
            self.assertEqual(command[start:start + 7], [
                '-lm', '-framework', 'Security', '-framework', 'CoreFoundation', '-framework', 'CoreServices'])
            self.assertEqual(run.call_args_list[0].kwargs, {'check': True, 'timeout': 180})
            self.assertEqual(run.call_count, 2)

    def test_linux_normal_variable_uses_compiled_metadata_with_matching_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);compiler=root/'compiler with spaces';compiler.touch()
            metadata=root/'CMakeFiles/4.4.4/CMakeCXXCompiler.cmake';metadata.parent.mkdir(parents=True)
            metadata.write_text('set(CMAKE_CXX_COMPILER "'+str(compiler).replace('\\','/')+'")\n')
            other=root/'CMakeFiles/3.31.0/CMakeCXXCompiler.cmake';other.parent.mkdir(parents=True);other.write_text('old metadata must not be selected')
            cache={'CMAKE_CACHE_MAJOR_VERSION':'4','CMAKE_CACHE_MINOR_VERSION':'4','CMAKE_CACHE_PATCH_VERSION':'4'}
            self.assertEqual(Path(module.compiler_from_cmake(cache,root)),compiler)

    def test_cached_compiler_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);compiler=root/'clang++';compiler.touch()
            self.assertEqual(Path(module.compiler_from_cmake({'CMAKE_CXX_COMPILER':str(compiler)},root)),compiler)

    def test_missing_or_conflicting_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaisesRegex(AssertionError,'Missing exact'):module.compiler_from_cmake({},root)
            compiler=root/'clang++';compiler.touch();other=root/'other';other.touch()
            metadata=root/'CMakeFiles/4.4.4/CMakeCXXCompiler.cmake';metadata.parent.mkdir(parents=True)
            metadata.write_text('set(CMAKE_CXX_COMPILER "'+str(other).replace('\\','/')+'")\n')
            with self.assertRaisesRegex(AssertionError,'mismatch'):module.compiler_from_cmake({'CMAKE_CXX_COMPILER':str(compiler)},root)
