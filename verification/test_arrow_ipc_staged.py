"""Pure IPC phase/transport tests; mocked compiler/programs never execute GDAL."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('staged_arrow_ipc', Path(__file__).parent / 'driver-profile/arrow-ipc-check.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def elf(machine=183):
    result = bytearray(64)
    result[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', result, 16, 3, machine, 1)
    struct.pack_into('<H', result, 52, 64)
    return bytes(result)


class StagedArrowIpcTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='IPC staged phases ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = self.root / 'gdal4k-engine/build/engine/linux-arm64'
        self.compiler = self.root / 'toolchain with spaces/clang++'
        self.compiler.parent.mkdir()
        self.compiler.write_bytes(b'mocked compiler identity, never executed')
        self.cache = self.engine / 'cmake/CMakeCache.txt'
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text('CMAKE_CXX_COMPILER:FILEPATH=' + str(self.compiler) + '\n'
                              'CMAKE_CXX_FLAGS:STRING=--target=aarch64-unknown-linux-gnu --sysroot=' + shlex.quote(str(self.root / 'target sysroot')) +
                              ' --gcc-toolchain=' + shlex.quote(str(self.root / 'target gcc')) + '\n'
                              'CMAKE_CXX_FLAGS_RELEASE:STRING=-O3 -DNDEBUG\n'
                              'CMAKE_EXE_LINKER_FLAGS:STRING=-Wl,--as-needed\n')
        self.archive = self.engine / 'libgdal4k.a'
        self.archive.write_bytes(b'mocked current archive identity, never linked')
        self.linker = self.engine / 'native-linker-options.json'
        self.linker.write_text('["-lm", "-ldl"]')
        self.source = self.root / 'verification/driver-profile/arrow-ipc-check.cpp'
        self.source.parent.mkdir(parents=True)
        self.source.write_text('mocked current regression source, never compiled')
        self.include = self.engine / 'installed/gdal4k-linux-arm64/include'
        for name in ['arrow/api.h', 'arrow/io/memory.h', 'arrow/ipc/api.h', 'arrow/util/future.h', 'arrow/util/int_util_overflow.h']:
            path = self.include / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unit header identity: ' + name)
        self.manifest = self.engine / 'manifest.json'
        self.manifest.write_text(json.dumps(dict(target='linux-arm64', source_fingerprint='1' * 64,
            gdal='3.12.4', jxl=True, artifacts={
                'engine/linux-arm64/libgdal4k.a': self.sha(self.archive),
                'engine/linux-arm64/native-linker-options.json': self.sha(self.linker)})))
        self.program = self.root / 'verification/build/arrow-ipc-regression/arrow-ipc-check'
        self.receipt = self.root / 'verification/results/arrow-ipc-build-linux-arm64.json'
        self.result = self.root / 'verification/results/arrow-ipc-regression.json'
        self.addCleanup(patch.stopall)
        patch.object(module.platform, 'system', return_value='Linux').start()
        patch.object(module.platform, 'machine', return_value='x86_64').start()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def compiler_result(self, command, **options):
        self.assertEqual(str(self.compiler), command[0])
        self.assertIn(str(self.source), command)
        self.assertIn(str(self.archive), command)
        self.assertEqual(180, options['timeout'])
        self.assertEqual(self.root, options['cwd'])
        self.program.parent.mkdir(parents=True, exist_ok=True)
        self.program.write_bytes(elf())
        self.program.chmod(0o755)
        return subprocess.CompletedProcess(command, 0, 'compiler stdout\n', 'compiler stderr\n')

    def compile(self):
        with patch.object(module.subprocess, 'run', side_effect=self.compiler_result) as run:
            result = module.compile_staged(self.root)
            self.assertEqual(1, run.call_count)
        return result

    def runtime(self, root=None, program=None, receipt=None, completed=None):
        with patch.object(module.platform, 'machine', return_value='aarch64'), \
             patch.object(module.subprocess, 'run', return_value=completed or
                          subprocess.CompletedProcess([str(program or self.program)], 0, module.MARKER + '\n', '')) as run:
            result = module.run_staged(root or self.root, program or self.program, receipt or self.receipt)
        return result, run

    def test_compile_uses_exact_compiler_and_records_compile_only_bytes(self):
        result = self.compile()
        self.assertEqual('compiled', result['result'])
        self.assertTrue(result['compiled_only'])
        self.assertFalse(result['actual_execution'])
        self.assertEqual(self.sha(self.program), result['executableSha256'])
        self.assertEqual(self.sha(self.compiler), result['compilerSha256'])
        self.assertEqual(self.sha(self.archive), result['archiveSha256'])
        self.assertEqual(self.sha(self.manifest), result['engineManifestSha256'])
        self.assertEqual(self.sha(self.source), result['sourceSha256'])
        self.assertEqual(5, len(result['directArrowHeaderSha256']))
        self.assertIn('--gcc-toolchain=' + str(self.root / 'target gcc'), result['execution']['argv'])
        self.assertNotIn(str(self.program), result['execution']['argv'][:1])
        self.assertFalse(self.result.exists())

    def test_runtime_relocation_needs_no_compiler_cmake_or_installation(self):
        self.compile()
        moved = self.root / 'relocated source'
        files = [self.source, self.archive, self.manifest, self.program, self.receipt]
        for path in files:
            destination = moved / path.relative_to(self.root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        shutil.rmtree(self.engine / 'installed')
        shutil.rmtree(self.engine / 'cmake')
        self.compiler.unlink()
        program = moved / self.program.relative_to(self.root)
        receipt = moved / self.receipt.relative_to(self.root)
        result, run = self.runtime(moved, program, receipt)
        self.assertEqual('passed', result['result'])
        self.assertTrue(result['actual_execution'])
        self.assertFalse(result['compiled_only'])
        self.assertEqual(self.sha(receipt), result['buildReceiptSha256'])
        self.assertEqual([str(program)], run.call_args.args[0])
        self.assertEqual(120, run.call_args.kwargs['timeout'])
        self.assertEqual(1, run.call_count)
        self.assertEqual(module.MARKER + '\n', result['stdout'])

    def test_tampered_transported_inputs_never_execute(self):
        self.compile()
        for path, message in [(self.source, 'executable or source'), (self.program, 'executable or source'),
                              (self.archive, 'original target engine manifest'), (self.manifest, 'original target engine manifest')]:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                if path == self.manifest:
                    data = json.loads(original)
                    data['target'] = 'linux-amd64'
                    path.write_text(json.dumps(data))
                else:
                    path.write_bytes(original + b'tampered')
                with patch.object(module.platform, 'machine', return_value='arm64'), \
                     patch.object(module.subprocess, 'run') as run, \
                     self.assertRaisesRegex(ValueError, message):
                    module.run_staged(self.root, self.program, self.receipt)
                run.assert_not_called()
                path.write_bytes(original)
        self.assertFalse(self.result.exists())

    def test_receipt_phase_and_owned_paths_are_enforced(self):
        self.compile()
        original = self.receipt.read_bytes()
        for key, value in [('compiled_only', False), ('actual_execution', True), ('result', 'passed'),
                           ('target', 'linux-amd64'), ('sourcePath', '../unowned.cpp')]:
            with self.subTest(key=key):
                receipt = json.loads(original)
                receipt[key] = value
                self.receipt.write_text(json.dumps(receipt))
                with patch.object(module.platform, 'machine', return_value='aarch64'), \
                     patch.object(module.subprocess, 'run') as run, self.assertRaises(ValueError):
                    module.run_staged(self.root, self.program, self.receipt)
                run.assert_not_called()
        self.receipt.write_bytes(original)
        with patch.object(module.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'actual Linux ARM64'):
            module.run_staged(self.root, self.program, self.receipt)
        run.assert_not_called()

    def test_runtime_failures_and_timeout_keep_raw_logs_without_passing(self):
        self.compile()
        failures = [subprocess.CompletedProcess([], 1, module.MARKER + '\n', 'real exit failure'),
                    subprocess.CompletedProcess([], 0, 'no marker', ''),
                    subprocess.TimeoutExpired([str(self.program)], 120, output=b'partial stdout', stderr=b'partial stderr'),
                    PermissionError('not executable')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(module.platform, 'machine', return_value='aarch64'), \
                 patch.object(module.subprocess, 'run') as run:
                if isinstance(failure, BaseException):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                with self.assertRaisesRegex(RuntimeError, 'Actual staged IPC execution failed'):
                    module.run_staged(self.root, self.program, self.receipt)
            data = json.loads(self.result.read_text())
            self.assertEqual('failed', data['result'])
            self.assertEqual(not isinstance(failure, PermissionError), data['actual_execution'])
            for stream in ['stdout', 'stderr']:
                log = self.root / data['execution'][stream + 'Log']
                self.assertEqual(self.sha(log), data['execution'][stream + 'Sha256'])

    def test_compile_rejects_wrong_architecture_stale_output_and_changed_source(self):
        def wrong_arch(command, **options):
            result = self.compiler_result(command, **options)
            self.program.write_bytes(elf(62))
            return result
        with patch.object(module.subprocess, 'run', side_effect=wrong_arch), self.assertRaisesRegex(RuntimeError, 'AArch64'):
            module.compile_staged(self.root)
        self.assertEqual('failed', json.loads(self.receipt.read_text())['result'])
        self.program.write_bytes(elf())
        with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             self.assertRaisesRegex(RuntimeError, 'did not refresh'):
            module.compile_staged(self.root)
        def source_changed(command, **options):
            result = self.compiler_result(command, **options)
            self.source.write_text('changed while compiler was running')
            return result
        with patch.object(module.subprocess, 'run', side_effect=source_changed), self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            module.compile_staged(self.root)

    def test_compile_timeout_preserves_partial_bytes_and_cannot_be_run(self):
        failure = subprocess.TimeoutExpired([str(self.compiler)], 180, output=b'partial compiler', stderr=b'compiler timeout')
        with patch.object(module.subprocess, 'run', side_effect=failure), self.assertRaisesRegex(RuntimeError, 'compilation failed'):
            module.compile_staged(self.root)
        built = json.loads(self.receipt.read_text())
        self.assertEqual('failed', built['result'])
        self.assertEqual('partial compiler', built['stdout'])
        self.assertTrue(built['compiled_only'])
        self.assertFalse(built['actual_execution'])
        with patch.object(module.platform, 'machine', return_value='arm64'), patch.object(module.subprocess, 'run') as run, \
             self.assertRaisesRegex(ValueError, 'compile-only build receipt'):
            module.run_staged(self.root, self.program, self.receipt)
        run.assert_not_called()

    def test_target_sysroot_archive_and_linker_binding_fail_before_compile(self):
        original_cache = self.cache.read_text()
        self.cache.write_text(original_cache.replace('--target=aarch64-unknown-linux-gnu', '--target=x86_64-unknown-linux-gnu'))
        with patch.object(module.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'exact AArch64 engine sysroot'):
            module.compile_staged(self.root)
        run.assert_not_called()
        self.cache.write_text(original_cache)
        self.linker.write_text('["-unexpected"]')
        with patch.object(module.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'linker options differ'):
            module.compile_staged(self.root)
        run.assert_not_called()
        self.linker.write_text('["-lm", "-ldl"]')
        self.archive.write_bytes(b'archive changed after producer manifest')
        with patch.object(module.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'original target engine manifest'):
            module.compile_staged(self.root)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
