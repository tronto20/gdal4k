"""Use the exact engine compiler when a Linux toolchain omits its CMake cache entry."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from verification.test_windows_pe import pe_image

spec=importlib.util.spec_from_file_location('arrow_ipc',Path(__file__).parent/'driver-profile/arrow-ipc-check.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ArrowIpcToolchainTest(unittest.TestCase):
    def test_windows_main_reuses_gnu_target_compiler_and_executes_exe(self):
        with tempfile.TemporaryDirectory(prefix='IPC Windows toolchain ') as temporary:
            root = Path(temporary)
            compiler = root / 'owned compiler/clang++.exe'; compiler.parent.mkdir(); compiler.touch()
            engine = root / 'gdal4k-engine/build/engine/windows-amd64'
            cache = engine / 'cmake/CMakeCache.txt'; cache.parent.mkdir(parents=True)
            cache.write_text('CMAKE_CXX_COMPILER:FILEPATH=' + str(compiler) + '\n'
                             'CMAKE_CXX_FLAGS:STRING=--target=x86_64-w64-windows-gnu --sysroot="C:/owned toolchain/sysroot" -femulated-tls\n'
                             'CMAKE_CXX_FLAGS_RELEASE:STRING=-O3 -DNDEBUG\n'
                             'CMAKE_EXE_LINKER_FLAGS:STRING=--ld-path="C:/owned toolchain/ld.lld.exe" -pthread\n', encoding='utf-8')
            archive = engine / 'libgdal4k.a'; archive.write_bytes(b'unit test archive')
            header = engine / 'installed/gdal4k-windows-amd64/include/arrow/ipc/api.h'
            header.parent.mkdir(parents=True); header.touch()
            source = root / 'verification/driver-profile/arrow-ipc-check.cpp'
            source.parent.mkdir(parents=True); source.write_text('// UTF-8 © test source\n', encoding='utf-8')
            options = ['-L/path with spaces/lib', '-lws2_32', '-lcrypt32']
            (engine / 'native-linker-options.json').write_text(json.dumps(options), encoding='utf-8')
            inspector = root / 'verification/windows_pe.py'
            inspector.write_bytes((Path(__file__).parent / 'windows_pe.py').read_bytes())
            program = root / 'verification/build/arrow-ipc-regression/arrow-ipc-check.exe'
            completed = Mock(stdout=module.MARKER + '\n', stderr='')
            def compile_or_run(command, **kwargs):
                if command[0] == str(compiler): program.write_bytes(pe_image())
                return completed
            with patch.object(module.sys, 'argv', ['arrow-ipc-check.py', str(root), 'windows-amd64']), \
                 patch.object(module.subprocess, 'run', side_effect=compile_or_run) as run, patch('builtins.print'):
                module.main()
            command = run.call_args_list[0].args[0]
            self.assertEqual(command[0], str(compiler))
            self.assertIn('--target=x86_64-w64-windows-gnu', command)
            self.assertIn('--sysroot=C:/owned toolchain/sysroot', command)
            self.assertIn('--ld-path=C:/owned toolchain/ld.lld.exe', command)
            self.assertIn('-femulated-tls', command)
            self.assertIn(options[0], command)
            self.assertEqual(command[-7:-2], ['-static-libgcc', '-static-libstdc++', '-Wl,-Bstatic', '-lwinpthread', '-Wl,-Bdynamic'])
            self.assertNotIn('-arch', command)
            self.assertEqual(command[-2:], ['-o', str(program)])
            self.assertEqual(run.call_args_list[1].args[0], [str(program)])
            proof = json.loads((root / 'verification/results/arrow-ipc-regression.json').read_text(encoding='utf-8'))
            self.assertEqual('windows-amd64', proof['target'])
            self.assertEqual(module.MARKER + '\n', proof['stdout'])
            receipt = json.loads((root / proof['windowsDependencyAudit']['path']).read_text(encoding='utf-8'))
            self.assertEqual('passed', receipt['result'])
            self.assertEqual(['kernel32.dll'], receipt['pe']['imports']['regular'])
            (root / proof['windowsDependencyAudit']['path']).unlink()
            def compile_unbundled(command, **kwargs):
                program.write_bytes(pe_image(delay=['libwinpthread-1.dll']))
                return completed
            with patch.object(module.sys, 'argv', ['arrow-ipc-check.py', str(root), 'windows-amd64']), \
                 patch.object(module.subprocess, 'run', side_effect=compile_unbundled) as run, \
                 self.assertRaisesRegex(ValueError, 'Unbundled dependencies'):
                module.main()
            self.assertEqual(1, run.call_count, 'An executable with a forbidden delay import must never run')
            cache.write_text('CMAKE_CXX_COMPILER:FILEPATH=' + str(compiler) + '\nCMAKE_CXX_FLAGS:STRING=--sysroot=C:/owned\n', encoding='utf-8')
            with patch.object(module.sys, 'argv', ['arrow-ipc-check.py', str(root), 'windows-amd64']), \
                 patch.object(module.subprocess, 'run') as run, \
                 self.assertRaisesRegex(AssertionError, 'Windows engine compiler target'):
                module.main()
            run.assert_not_called()

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
