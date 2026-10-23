"""Pure receipt and source-binding regressions; synthetic ELF bytes are never run.

The small schemas below exercise the report boundary only. They are not GDAL
producer, Native execution, database or platform-coverage evidence.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load('arm64_profile_report_unit', 'verification/driver-profile/report.py')
arm = load('arm64_profile_runtime_unit', 'verification/driver-profile/arm64-profile.py')
fixture = load('arm64_profile_fixture_unit', 'verification/driver-profile/runtime-fixture.py')
combined = load('arm64_profile_memory_unit', 'verification/combined-driver-memory-report.py')


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding='utf-8')
    return path


def save(root, name, value):
    return write(root, name, json.dumps(value, sort_keys=True, indent=2) + '\n')


def elf_fixture():
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, 2, 183, 1)
    return bytes(data)


def configuration(snapshot):
    return dict(schemaVersion=1, profile='combined', target='linux-arm64', runId='arm64_unit_' + 'a' * 32,
                postgresqlDsn='PG:host=127.0.0.1 port=16432 dbname=owned user=owned',
                httpUrls=dict(http='http://127.0.0.1:16001', https='https://127.0.0.1:16002',
                              untrusted='https://127.0.0.1:16003', **{'wrong-name': 'https://127.0.0.1:16004'}),
                hdf5Path=str((snapshot / 'verification/build/driver-profile.h5').resolve()))


def marker(config, key):
    return fixture.MARKER + json.dumps(dict(configuration=config, contextKey=key)) + '\n'


def native_stdout(namespace, config, key):
    names = [namespace + '.DriverProfileApiTest.requiredDriversAndRealDataRoundTrip']
    http_source = (ROOT / 'verification/driver-profile/NativeHttpProfileTest.kt').read_text(encoding='utf-8')
    names += [namespace + '.NativeHttpProfileTest.' + name for name in re.findall(r'@Test\s+fun\s+(\w+)\(', http_source)]
    names += [namespace + '.DatasetApiTest.pixelRoundTrip']
    lines = [marker(config, key).rstrip('\n'), f'[==========] Running {len(names)} tests from 3 test cases.']
    for name in names:
        lines += ['[ RUN      ] ' + name, '[       OK ] ' + name + ' (1 ms)']
    lines += [f'[==========] {len(names)} tests from 3 test cases ran. (12 ms total)', f'[  PASSED  ] {len(names)} tests.']
    return '\n'.join(lines) + '\n'


class NativeReportBindingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.snapshot = self.project / 'snapshot'
        self.snapshot.mkdir()
        self.configuration = configuration(self.snapshot)
        save(self.project, 'inputs.json', dict(version='0.1.0-combined-SNAPSHOT'))
        write(self.snapshot, 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt',
              (ROOT / 'verification/driver-profile/NativeHttpProfileTest.kt').read_bytes())
        self.compiled = dict(result='passed', target='linuxArm64', version='0.1.0-combined-SNAPSHOT', compiled_only=True,
                             producer_paths_hidden=True, producer_paths_hidden_count=3, producer_paths_hidden_names=[
                                 'gdal4k-engine/build/engine/linux-arm64',
                                 'gdal4k-engine/build/classes/kotlin/linuxArm64/main/cinterop',
                                 'gdal4k-runtime/build/classes/kotlin/linuxArm64/main/cinterop'])
        self.compile_path = save(self.snapshot, 'verification/results/published-consumer-linuxArm64/result.json', self.compiled)
        self.restored = dict(project=self.project, transfer=dict(files={
            self.compile_path.relative_to(self.project).as_posix(): dict(sha256=report.sha(self.compile_path))}))
        self.values = {}
        for label, namespace, binary_name in [
            ('source', 'dev.gdal4k', 'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'),
            ('consumer', 'consumer', 'verification/results/published-consumer-linuxArm64/bin/linuxArm64/debugTest/test.kexe'),
        ]:
            binary = write(self.snapshot, binary_name, elf_fixture())
            transferred = binary.relative_to(self.project).as_posix()
            self.restored['transfer'][label + 'Binary'] = transferred
            self.restored['transfer']['files'][transferred] = dict(sha256=report.sha(binary))
            log = write(self.snapshot, f'verification/results/arm64-commands/{label}.log',
                        native_stdout(namespace, self.configuration, 'gdal4k-' + label))
            command = save(self.snapshot, f'verification/results/arm64-commands/{label}.json',
                           dict(command=[str(binary)], cwd=str(self.snapshot), log=log.name,
                                status='passed', outputComplete=True, returncode=0, pid=1234,
                                timeoutSeconds=1800, elapsedSeconds=0.25,
                                startedAtUtc='2026-10-23T00:00:00+00:00', completedAtUtc='2026-10-23T00:00:01+00:00'))
            self.values[label] = dict(result='passed', actual_execution=True, compiled_only=False, target='linux-arm64',
                                     host=dict(system='Linux', machine='aarch64'), executable=binary_name,
                                     executableSha256=report.sha(binary), command=command.relative_to(self.snapshot).as_posix(),
                                     commandSha256=report.sha(command), log=log.relative_to(self.snapshot).as_posix(),
                                     logSha256=report.sha(log), counts=arm.native_cases(log.read_text(encoding='utf-8')),
                                     runtimeFixture=arm.fixture_markers(log.read_text(encoding='utf-8'), self.configuration),
                                     compilationReceipt=(dict(path=self.compile_path.relative_to(self.snapshot).as_posix(),
                                                              sha256=report.sha(self.compile_path)) if label == 'consumer' else None))
            self.save_value(label)

    def save_value(self, label):
        save(self.snapshot, f'verification/results/arm64-native-{label}.json', self.values[label])

    def verify(self, label='consumer'):
        return report.arm64_native_bindings(self.snapshot, label, self.configuration, self.restored)

    def test_separate_execution_binds_binary_stdout_command_and_preserves_compilation_receipt(self):
        original = self.compile_path.read_bytes()
        for label in ('source', 'consumer'):
            actual = self.verify(label)
            self.assertEqual(12, actual['counts']['tests'])
            self.assertEqual(self.values[label]['logSha256'], actual['logSha256'])
        self.assertEqual(original, self.compile_path.read_bytes())
        self.assertIs(True, json.loads(original)['compiled_only'])

    def test_actual_execution_cannot_replace_the_compile_only_consumer_or_hidden_three_paths(self):
        for field, changed in [('compiled_only', False), ('producer_paths_hidden', False),
                               ('producer_paths_hidden_count', 2), ('producer_paths_hidden_names', ['one/path']),
                               ('version', 'older-profile-version')]:
            with self.subTest(field=field):
                value = dict(self.compiled, **{field: changed})
                save(self.snapshot, self.compile_path.relative_to(self.snapshot).as_posix(), value)
                # Refresh the receipt hash deliberately: the semantic provenance check must reject it.
                self.values['consumer']['compilationReceipt']['sha256'] = report.sha(self.compile_path)
                self.save_value('consumer')
                with self.assertRaises(AssertionError):
                    self.verify()

    def test_nonexecuted_wrong_host_or_fabricated_counts_are_rejected(self):
        original = copy.deepcopy(self.values['consumer'])
        for field, changed in [('actual_execution', False), ('compiled_only', True),
                               ('host', dict(system='Linux', machine='x86_64')),
                               ('counts', dict(original['counts'], tests=13))]:
            with self.subTest(field=field):
                self.values['consumer'] = dict(original, **{field: changed})
                self.save_value('consumer')
                with self.assertRaises((AssertionError, ValueError)):
                    self.verify()

    def test_changed_binary_raw_output_configuration_and_receipt_hash_are_rejected(self):
        value = self.values['consumer']
        binary = self.snapshot / value['executable']
        original_binary = binary.read_bytes()
        binary.write_bytes(original_binary + b'changed')
        with self.assertRaises(AssertionError): self.verify()
        binary.write_bytes(original_binary)
        log = self.snapshot / value['log']
        original_log = log.read_bytes()
        log.write_bytes(original_log + b'Uncaught Kotlin exception\n')
        value['logSha256'] = report.sha(log); self.save_value('consumer')
        with self.assertRaises(ValueError): self.verify()
        log.write_bytes(original_log); value['logSha256'] = report.sha(log); self.save_value('consumer')
        changed_configuration = dict(self.configuration, hdf5Path='/stale/fixture.h5')
        with self.assertRaises(ValueError):
            report.arm64_native_bindings(self.snapshot, 'consumer', changed_configuration, self.restored)
        value['commandSha256'] = '0' * 64; self.save_value('consumer')
        with self.assertRaises(AssertionError): self.verify()

    def test_command_semantics_reject_wrong_program_missing_pid_partial_output_and_failure(self):
        path = self.snapshot / self.values['consumer']['command']
        original = json.loads(path.read_text(encoding='utf-8'))
        for field, changed in [('command', ['/unrelated/test.kexe']), ('cwd', '/stale/source'), ('pid', 0),
                               ('outputComplete', False), ('returncode', 9), ('status', 'timed_out')]:
            with self.subTest(field=field):
                save(self.snapshot, path.relative_to(self.snapshot).as_posix(), dict(original, **{field: changed}))
                self.values['consumer']['commandSha256'] = report.sha(path)
                self.save_value('consumer')
                with self.assertRaises(AssertionError): self.verify()


class RuntimeFixtureSourceReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot = Path(self.temporary.name) / 'snapshot'
        self.snapshot.mkdir()
        self.prepared = {}
        for suffix, source_set in [('', 'commonTest'), ('Jvm', 'jvmTest'), ('Native', 'nativeTest')]:
            name = 'DriverProfileRuntimeFixture' + suffix + '.kt'
            template = (ROOT / 'verification/driver-profile' / name).read_text(encoding='utf-8')
            write(self.snapshot, 'verification/driver-profile/' + name, template)
            generated = template.replace('@RUNTIME_FIXTURES_REQUIRED@', 'true')
            for key in ['@HDF5_PATH@', '@POSTGRESQL_DSN@', '@HTTP_BASE@', '@HTTPS_BASE@', '@UNTRUSTED_BASE@', '@WRONG_NAME_BASE@']:
                generated = generated.replace(key, '')
            destination = write(self.snapshot, f'gdal4k-runtime/src/{source_set}/kotlin/dev/gdal4k/' + name, generated)
            self.prepared[destination.relative_to(self.snapshot).as_posix()] = report.sha(destination)
        save(self.snapshot.parent, 'prepared.json', self.prepared)
        for target in ('jvm', 'linuxArm64'):
            consumer = self.snapshot.parent / ('consumer-' + target)
            receipts = fixture.copy_to_consumer(self.snapshot, consumer, target)
            evidence = self.snapshot / ('verification/results/published-consumer-' + target)
            fixture.preserve_consumer_sources(consumer, evidence, receipts)
            save(evidence, 'result.json', dict(profile_runtime_fixture_sources=receipts))

    def verify(self):
        return report.runtime_fixture_source_bindings(self.snapshot)

    def test_all_three_generated_sources_and_independent_platform_actual_bytes_are_bound(self):
        actual = self.verify()
        self.assertEqual(self.prepared, actual['generatedSourceSha256'])
        self.assertEqual({'jvm', 'linuxArm64'}, set(actual['independentConsumerSources']))
        for target, result in actual['independentConsumerSources'].items():
            self.assertEqual(2, len(result['sources']))
            self.assertTrue(any(('Jvm' if target == 'jvm' else 'Native') + '.kt' in item['source']
                                for item in result['sources']))

    def test_generated_source_tamper_or_fixed_host_fallback_is_rejected_even_with_refreshed_hash(self):
        path = self.snapshot / next(name for name in self.prepared if '/commonTest/' in name)
        path.write_text(path.read_text(encoding='utf-8').replace('if (!true)', 'if (!false)'), encoding='utf-8')
        self.prepared[path.relative_to(self.snapshot).as_posix()] = report.sha(path)
        save(self.snapshot.parent, 'prepared.json', self.prepared)
        with self.assertRaisesRegex(AssertionError, 'must require runtime input'):
            self.verify()

    def test_missing_generated_native_actual_or_changed_preserved_consumer_is_rejected(self):
        native = self.snapshot / next(name for name in self.prepared if '/nativeTest/' in name)
        original = native.read_bytes(); native.unlink()
        with self.assertRaises(AssertionError): self.verify()
        native.write_bytes(original)
        result_path = self.snapshot / 'verification/results/published-consumer-linuxArm64/result.json'
        result = json.loads(result_path.read_text(encoding='utf-8'))
        item = result['profile_runtime_fixture_sources'][1]
        preserved = result_path.parent / item['preservedSource']
        preserved.write_bytes(preserved.read_bytes() + b'// stale consumer\n')
        item['consumerSha256'] = report.sha(preserved)
        save(result_path.parent, result_path.name, result)
        with self.assertRaises(AssertionError): self.verify()

    def test_diagnostic_class_selection_includes_common_actual_and_lazy_fixture_classes(self):
        names = ['CombinedDriverMemoryProbe.class', 'DriverProfileApiTestKt.class', 'DriverProfileFixtures.class',
                 'DriverProfileRuntimeFixture.class', 'DriverProfileRuntimeFixtureKt.class',
                 'DriverProfileRuntimeFixtureJvmKt.class', 'DriverProfileRuntimeFixtureKt$driverProfileRuntimeFixture$2.class']
        classes = self.snapshot / 'classes'
        for name in names + ['Unrelated.class']:
            write(classes, name, b'unit class inventory only; never executable')
        self.assertEqual(set(names), {p.name for p in combined.compiled_probe_classes(classes)})


class IpcExecutionReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot = Path(self.temporary.name)
        self.program = write(self.snapshot, 'verification/build/arrow-ipc-regression/arrow-ipc-check', elf_fixture())
        self.manifest = save(self.snapshot, 'gdal4k-engine/build/engine/linux-arm64/manifest.json',
                             dict(source_fingerprint='a' * 64))
        self.built = dict(result='compiled', compiled_only=True, actual_execution=False, target='linux-arm64',
                          compilerHost=dict(system='Linux', architecture='x86_64'), sourceSha256='b' * 64,
                          archiveSha256='c' * 64, engineManifestSha256=report.sha(self.manifest),
                          engineSourceFingerprint='a' * 64, executableSha256=report.sha(self.program),
                          executablePath=self.program.relative_to(self.snapshot).as_posix())
        self.build_path = save(self.snapshot, 'verification/results/arrow-ipc-build-linux-arm64.json', self.built)
        stdout = write(self.snapshot, 'verification/results/ipc.stdout.log', 'GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK\n')
        stderr = write(self.snapshot, 'verification/results/ipc.stderr.log', '')
        self.ipc = {key: self.built[key] for key in ['sourceSha256', 'archiveSha256', 'engineManifestSha256',
                                                   'engineSourceFingerprint', 'executableSha256']}
        self.ipc.update(compiled_only=False, actual_execution=True, host=dict(system='Linux', architecture='aarch64'),
                        buildReceiptSha256=report.sha(self.build_path), stdout=stdout.read_text(encoding='utf-8'), stderr='',
                        execution=dict(argv=[str(self.program)], cwd=str(self.snapshot), processStarted=True,
                                       timedOut=False, error=None, returncode=0, timeoutSeconds=120,
                                       startedAtUtc='2026-10-23T00:00:00Z', completedAtUtc='2026-10-23T00:00:01Z',
                                       stdoutLog=stdout.relative_to(self.snapshot).as_posix(), stdoutSha256=report.sha(stdout),
                                       stderrLog=stderr.relative_to(self.snapshot).as_posix(), stderrSha256=report.sha(stderr)))

    def test_actual_execution_binds_unchanged_compile_only_receipt_and_raw_output(self):
        original = self.build_path.read_bytes()
        actual = report.arm64_ipc_bindings(self.snapshot, self.ipc)
        self.assertEqual(report.sha(self.build_path), actual['buildReceiptSha256'])
        self.assertEqual(original, self.build_path.read_bytes())
        self.assertIs(True, json.loads(original)['compiled_only'])

    def test_compile_host_or_nonexecution_or_different_build_identity_cannot_pass(self):
        for field, changed in [('actual_execution', False), ('compiled_only', True),
                               ('host', dict(system='Linux', architecture='x86_64')),
                               ('buildReceiptSha256', '0' * 64), ('executableSha256', '1' * 64)]:
            value = dict(self.ipc, **{field: changed})
            with self.subTest(field=field), self.assertRaises(AssertionError):
                report.arm64_ipc_bindings(self.snapshot, value)

    def test_actual_failed_command_or_mutated_raw_output_is_rejected(self):
        for field, changed in [('returncode', 1), ('timedOut', True), ('processStarted', False),
                               ('argv', ['/other/program'])]:
            value = dict(self.ipc, execution=dict(self.ipc['execution'], **{field: changed}))
            with self.subTest(field=field), self.assertRaises(AssertionError):
                report.arm64_ipc_bindings(self.snapshot, value)
        stdout = self.snapshot / self.ipc['execution']['stdoutLog']
        stdout.write_text('changed output\n', encoding='utf-8')
        with self.assertRaises(AssertionError): report.arm64_ipc_bindings(self.snapshot, self.ipc)


class RuntimeMarkerReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.snapshot = Path(self.temporary.name)
        self.configuration = configuration(self.snapshot)
        save(self.snapshot, 'verification/results/arm64-runtime-configuration.json', self.configuration)
        save(self.snapshot, 'verification/results/native-http-profile/fixture.json', dict(urls=self.configuration['httpUrls']))
        self.keys = {'gdal4k-source-jvm', 'gdal4k-consumer-jvm', 'gdal4k-source-native', 'gdal4k-consumer-native'}
        for label, folder in [('source', 'gdal4k-runtime/build/test-results/jvmTest'),
                              ('consumer', 'verification/results/published-consumer-jvm')]:
            xml = ET.Element('testsuite')
            ET.SubElement(xml, 'system-out').text = marker(self.configuration, 'gdal4k-' + label + '-jvm')
            write(self.snapshot, folder + '/TEST-unit.xml', ET.tostring(xml))
        self.native = {}
        for label in ['source', 'consumer']:
            log = write(self.snapshot, 'verification/results/' + label + '.log',
                        marker(self.configuration, 'gdal4k-' + label + '-native'))
            self.native[label] = dict(log=log.relative_to(self.snapshot).as_posix(), logSha256=report.sha(log))
        self.diagnostics = dict(records=[])
        for trial in range(3):
            key = 'gdal4k-probe-' + str(trial)
            log = write(self.snapshot, f'verification/results/combined-driver-memory/{trial}.log', marker(self.configuration, key))
            self.diagnostics['records'].append(dict(trial=trial, contextKey=key,
                                                    log=log.relative_to(self.snapshot).as_posix(), logSha256=report.sha(log)))

    def verify(self, keys=None):
        # Source transfer has separate byte-level tests above; isolate marker matching here.
        with patch.object(report, 'runtime_fixture_source_bindings', return_value={'unit': 'separate boundary'}):
            return report.arm64_runtime_fixture_bindings(self.snapshot, self.diagnostics,
                                                         self.keys if keys is None else keys, self.native)

    def test_seven_distinct_processes_match_four_policy_and_three_probe_contexts(self):
        actual = self.verify()
        self.assertEqual(7, len(actual['processMarkers']))
        self.assertEqual(self.configuration, actual['configuration'])
        canonical = fixture.canonical_json(self.configuration).encode('utf-8')
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), actual['canonicalConfigurationSha256'])

    def test_stale_endpoint_wrong_context_or_duplicate_marker_cannot_pass(self):
        with self.assertRaises(AssertionError): self.verify((self.keys - {'gdal4k-source-native'}) | {'gdal4k-stale'})
        record = self.diagnostics['records'][0]
        log = self.snapshot / record['log']
        original = log.read_text(encoding='utf-8')
        stale = copy.deepcopy(self.configuration); stale['httpUrls']['http'] = 'http://127.0.0.1:19000'
        log.write_text(marker(stale, record['contextKey']), encoding='utf-8')
        record['logSha256'] = report.sha(log)
        with self.assertRaises(ValueError): self.verify()
        log.write_text(original + original, encoding='utf-8'); record['logSha256'] = report.sha(log)
        with self.assertRaises(ValueError): self.verify()


if __name__ == '__main__':
    unittest.main()
