"""Pure host/unit and mocked-child evidence checks; never launch a JVM or time."""
import importlib.util
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load('combined_driver_memory_report', 'combined-driver-memory-report.py')
rss = load('combined_driver_memory_rss', 'window-memory-report.py')
binding_spec = importlib.util.spec_from_file_location(
    'combined_profile_bindings', Path(__file__).parent / 'driver-profile/report.py')
bindings = importlib.util.module_from_spec(binding_spec)
binding_spec.loader.exec_module(bindings)


class CombinedHostTest(unittest.TestCase):
    def test_host_manifest_library_and_real_rss_units_agree(self):
        examples = [('Linux', 'x86_64', 'linux-amd64', 'libgdal4k.so', '-v', 'KiB',
                     'Maximum resident set size (kbytes): 1024\n'),
                    ('Darwin', 'arm64', 'macos-arm64', 'libgdal4k.dylib', '-l', 'bytes',
                     '    1048576  maximum resident set size\n')]
        for system, machine, target, library, flag, unit, raw in examples:
            with self.subTest(system=system):
                host = report.host_settings({'target': target}, system, machine)
                self.assertEqual((target, library, flag, unit),
                                 (host['target'], host['library'], host['flag'], host['reportedRssUnit']))
                self.assertEqual(1048576, rss.peak_rss_bytes(raw, system))
                self.assertEqual(1048576, (1024 if system == 'Linux' else 1048576) *
                                 host['rssMultiplierToBytes'])

    def test_other_architectures_and_mismatched_manifest_are_rejected(self):
        for system, machine, target in [('Linux', 'aarch64', 'linux-arm64'),
                                       ('Windows', 'AMD64', 'windows-amd64'),
                                       ('Darwin', 'x86_64', 'macos-arm64'),
                                       ('Darwin', 'arm64', 'linux-amd64'),
                                       ('Linux', 'x86_64', 'macos-arm64')]:
            with self.subTest(system=system, machine=machine, target=target):
                with self.assertRaises(ValueError):
                    report.host_settings({'target': target}, system, machine)


class CombinedCommandEvidenceTest(unittest.TestCase):
    def command(self, root):
        return ['/usr/bin/time', '-l', '-o', str(root / 'rss.txt'), '/jdk with spaces/bin/java',
                '-cp', '/classes:/jar with spaces.jar', 'dev.gdal4k.CombinedDriverMemoryProbe', '/engine.dylib']

    def child(self, command, returncode):
        process = MagicMock()
        process.args, process.pid, process.returncode = command, 4321, returncode
        popen = MagicMock()
        popen.return_value.__enter__.return_value = process
        return process, popen

    def test_success_retains_exact_argv_owned_wrapper_pid_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = self.command(root)
            process, popen = self.child(command, 0)
            evidence_path = root / 'command.json'
            with patch.object(report.subprocess, 'Popen', popen), \
                    patch.object(report.os, 'getpgid', return_value=process.pid):
                actual = report.run_timed(command, root / 'stdout.log', evidence_path, cwd=root)
            self.assertEqual(actual, json.loads(evidence_path.read_text(encoding='utf-8')))
            self.assertEqual(command, actual['command'])
            self.assertEqual((4321, 4321, 0, 'passed'),
                             (actual['pid'], actual['observedProcessGroupId'], actual['returncode'], actual['status']))
            self.assertIn('JVM PID is not observed', actual['pidRole'])
            self.assertNotIn('jvmPid', actual)
            self.assertEqual(root, popen.call_args.kwargs['cwd'])
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual('C', popen.call_args.kwargs['env']['LC_ALL'])
            process.wait.assert_called_once_with(timeout=600)

    def test_failed_child_keeps_real_nonzero_exit_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = self.command(root)
            process, popen = self.child(command, 17)
            log = root / 'stdout.log'
            process.wait.side_effect = lambda **kwargs: log.write_text('partial failure\n', encoding='utf-8')
            with patch.object(report.subprocess, 'Popen', popen), \
                    patch.object(report.os, 'getpgid', return_value=process.pid):
                actual = report.run_timed(command, log, root / 'command.json', cwd=root)
            self.assertEqual((17, 'failed'), (actual['returncode'], actual['status']))
            self.assertEqual('partial failure\n', log.read_text(encoding='utf-8'))

    def test_timeout_retains_receipt_before_terminating_only_owned_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = self.command(root)
            process, popen = self.child(command, -signal.SIGKILL)
            process.wait.side_effect = [subprocess.TimeoutExpired(command, 600), None]
            evidence_path = root / 'command.json'

            def observe_cleanup(pid, action):
                self.assertEqual(4321, pid)
                self.assertEqual('timed_out', json.loads(evidence_path.read_text(encoding='utf-8'))['status'])

            with patch.object(report.subprocess, 'Popen', popen), \
                    patch.object(report.os, 'getpgid', return_value=process.pid), \
                    patch.object(report.os, 'killpg', side_effect=observe_cleanup) as kill, \
                    patch.object(report.time, 'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'timed out'):
                    report.run_timed(command, root / 'stdout.log', evidence_path, cwd=root)
            self.assertEqual([(4321, signal.SIGTERM), (4321, signal.SIGKILL)],
                             [call.args for call in kill.call_args_list])
            sleep.assert_called_once_with(5)
            actual = json.loads(evidence_path.read_text(encoding='utf-8'))
            self.assertEqual(('timed_out', -signal.SIGKILL), (actual['status'], actual['returncode']))
            self.assertTrue((root / 'stdout.log').exists())

    def test_launch_error_keeps_command_without_inventing_pid_or_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / 'command.json'
            with patch.object(report.subprocess, 'Popen', side_effect=OSError('missing executable')):
                with self.assertRaises(OSError):
                    report.run_timed(self.command(root), root / 'stdout.log', evidence_path, cwd=root)
            actual = json.loads(evidence_path.read_text(encoding='utf-8'))
            self.assertEqual(('failed', 'OSError'), (actual['status'], actual['exception']))
            self.assertNotIn('pid', actual)
            self.assertNotIn('returncode', actual)


class CombinedReceiptBindingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / 'verification/results/combined-driver-memory/unit'
        self.directory.mkdir(parents=True)
        cp = self.root / 'gdal4k-runtime/build/combined-driver-memory-classpath.json'
        cp.parent.mkdir(parents=True)
        cp.write_text(json.dumps({'java': '/unit-jdk/bin/java', 'classpath': '/unit/classes:/unit/runtime.jar'}), encoding='utf-8')
        self.configure('macos-arm64')

    def configure(self, target):
        darwin = target == 'macos-arm64'
        flag, system, machine = ('-l', 'Darwin', 'arm64') if darwin else ('-v', 'Linux', 'x86_64')
        library = 'libgdal4k.dylib' if darwin else 'libgdal4k.so'
        settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
        prefix = 'verification/results/combined-driver-memory/unit/0'
        self.record = dict(trial=0, log=prefix + '.log', rssLog=prefix + '.time.txt', executionLog=prefix + '.command.json')
        self.execution = dict(command=['/usr/bin/time', flag, '-o', str(self.root / self.record['rssLog']),
                                      '/unit-jdk/bin/java', *settings, '-cp', '/unit/classes:/unit/runtime.jar',
                                      'dev.gdal4k.CombinedDriverMemoryProbe',
                                      str(self.root / f'gdal4k-engine/build/jvm-resources/gdal4k/{target}/{library}')],
                              cwd=str(self.root), timeoutSeconds=600, requestedNewSession=True,
                              pid=4321, observedProcessGroupId=4321, parentPid=4242,
                              pidRole='/usr/bin/time wrapper; JVM PID is not observed',
                              environmentOverrides={'LC_ALL': 'C'}, status='passed', returncode=0,
                              startedAtUtc='2026-10-23T00:00:00+00:00', completedAtUtc='2026-10-23T00:00:01+00:00',
                              elapsedSeconds=1.0)
        self.data = dict(profile='combined', target=target, system=system, machine=machine,
                         platform='inert unit fixture for ' + system, settings={'jvm': settings}, records=[self.record],
                         osRss=dict(timeExecutable='/usr/bin/time', timeArgument=flag,
                                    timeFormat='Darwin time -l' if darwin else 'GNU time -v',
                                    reportedUnit='bytes' if darwin else 'KiB', multiplierToBytes=1 if darwin else 1024))
        self.write_execution()

    def write_execution(self):
        path = self.root / self.record['executionLog']
        path.write_text(json.dumps(self.execution), encoding='utf-8')
        self.record['execution'] = json.loads(json.dumps(self.execution))
        self.record['executionLogSha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_actual_receipt_hash_content_and_both_host_commands_are_bound(self):
        for target in ['macos-arm64', 'linux-amd64']:
            with self.subTest(target=target):
                self.configure(target)
                self.assertEqual({self.record['executionLog']: self.record['executionLogSha256']},
                                 bindings.combined_execution_bindings(self.root, self.data))

    def test_tampered_file_or_stale_embedded_receipt_is_rejected(self):
        path = self.root / self.record['executionLog']
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(AssertionError, 'receipt changed'):
            bindings.combined_execution_bindings(self.root, self.data)
        self.write_execution()
        self.record['execution']['returncode'] = 17
        with self.assertRaisesRegex(AssertionError, 'embedded command receipt differs'):
            bindings.combined_execution_bindings(self.root, self.data)

    def test_rehashed_failure_and_unowned_pid_group_are_rejected(self):
        for field, value, message in [('returncode', 17, 'successful owned'),
                                      ('status', 'timed_out', 'successful owned'),
                                      ('observedProcessGroupId', 9999, 'PID/PGID'),
                                      ('pid', True, 'PID/PGID')]:
            with self.subTest(field=field):
                self.configure('macos-arm64')
                self.execution[field] = value
                self.write_execution()
                with self.assertRaisesRegex(AssertionError, message):
                    bindings.combined_execution_bindings(self.root, self.data)

    def test_rehashed_wrong_java_timer_probe_and_library_commands_are_rejected(self):
        for index, value in [(1, '-v'), (4, '/different-jdk/bin/java'),
                             (-2, 'dev.gdal4k.UnrelatedProbe'), (-1, '/old/libgdal4k.dylib')]:
            with self.subTest(index=index):
                self.configure('macos-arm64')
                self.execution['command'][index] = value
                self.write_execution()
                with self.assertRaisesRegex(AssertionError, 'command mismatch'):
                    bindings.combined_execution_bindings(self.root, self.data)

    def test_host_unit_mismatch_and_external_receipt_path_are_rejected(self):
        self.data['osRss']['multiplierToBytes'] = 1024
        with self.assertRaisesRegex(AssertionError, 'time format/unit mismatch'):
            bindings.combined_execution_bindings(self.root, self.data)
        self.configure('macos-arm64')
        self.data['system'] = 'Linux'
        with self.assertRaisesRegex(AssertionError, 'host metadata mismatch'):
            bindings.combined_execution_bindings(self.root, self.data)
        self.configure('macos-arm64')
        self.record['executionLog'] = '../private/command.json'
        with self.assertRaises(AssertionError):
            bindings.combined_execution_bindings(self.root, self.data)

    def test_diagnostic_binding_requires_commands_only_for_combined_profile(self):
        files = {'source.kt': b'unit source', 'probe.class': b'unit class bytes',
                 self.record['log']: b'unit stdout', self.record['rssLog']: b'unit OS output',
                 'gdal4k-engine/build/engine/macos-arm64/manifest.json': b'unit manifest'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        digest = lambda name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        self.data.update(engineFingerprint='unit fingerprint', engineLibrarySha256='unit JNI',
                         engineManifestSha256=digest('gdal4k-engine/build/engine/macos-arm64/manifest.json'),
                         compiledProductionSha256={'unit class': 'unit hash'},
                         compiledProbeSha256={'probe.class': digest('probe.class')}, sourceSha256={'source.kt': digest('source.kt')})
        self.record.update(logSha256=digest(self.record['log']), rssLogSha256=digest(self.record['rssLog']))
        measured, publication = {'fingerprint': 'unit fingerprint'}, {'jniSha256': 'unit JNI', 'compiledProductionSha256': {'unit class': 'unit hash'}}
        result = bindings.diagnostic_bindings(self.root, self.data, measured, publication)
        self.assertIn('executionLogSha256', result)
        del self.record['executionLogSha256']
        self.data['profile'] = 'http'
        self.assertNotIn('executionLogSha256', bindings.diagnostic_bindings(self.root, self.data, measured, publication))
        self.data['profile'] = 'combined'
        with self.assertRaises(KeyError):
            bindings.diagnostic_bindings(self.root, self.data, measured, publication)


if __name__ == '__main__':
    unittest.main()
