"""Pure receipt/ownership checks plus explicitly Windows-only real kernel tests.

The portable tests mock one Win32 boundary; they do not establish Windows runtime
support. The native tests launch only owned Python children, never GDAL or a DB.
"""
import copy
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import windows_process as windows
import command_evidence


def identity(pid=123, created=134000000000000000):
    return dict(pid=pid, creationTime100ns=created, imagePath=r'C:\Python312\python.exe')


def receipt():
    who = identity()
    peak = dict(collector='Win32 GetProcessMemoryInfo', field='PeakWorkingSetSize',
                reportedUnit='bytes', peakWorkingSetSizeBytes=1048576, windowsProcess=who,
                queryPhase='after-process-exit-before-handle-close', returncode=0)
    return dict(command=[who['imagePath'], '-c', 'pass'], cwd=r'C:\owned', pid=who['pid'],
                windowsProcess=who, peakMemory=peak, pidRole='owned JVM process',
                ownership=dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True),
                cleanup=dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False), status='passed', returncode=0,
                outputComplete=True, logSha256='a' * 64, elapsedSeconds=1.1,
                startedAtUtc='2026-10-23T00:00:00+00:00', completedAtUtc='2026-10-23T00:00:01+00:00')


class WindowsReceiptTest(unittest.TestCase):
    def test_both_reporters_map_actual_windows_x64_to_mingw_and_kernel_bytes(self):
        for name in ['combined-driver-memory-report.py', 'http-memory-report.py']:
            path = Path(__file__).with_name(name)
            spec = importlib.util.spec_from_file_location('unit_windows_' + name, path)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            host = module.host_settings({'target': 'windows-amd64'}, 'Windows', 'AMD64')
            self.assertEqual(('MingwX64', 'windows-x64', 'gdal4k.dll', None, 'bytes', 1),
                             tuple(host[k] for k in ['gradleTarget', 'classifier', 'library', 'flag',
                                                    'reportedRssUnit', 'rssMultiplierToBytes']))
            with self.assertRaises(ValueError):
                module.host_settings({'target': 'windows-amd64'}, 'Windows', 'ARM64')
    def validate(self, value):
        expected = receipt()
        return windows.validate_execution_record(value, command=expected['command'], cwd=expected['cwd'],
                                                  log_sha256=expected['logSha256'], peak_record=value['peakMemory'])

    def test_final_kernel_peak_is_bytes_and_bound_to_exact_command_and_creation_time(self):
        value = receipt()
        self.assertEqual(1048576, self.validate(value)['peakWorkingSetSizeBytes'])
        self.assertEqual(1048576, windows.peak_record_bytes(json.dumps(value['peakMemory'])))
        for change in [lambda v: v.update(status='timed_out'), lambda v: v.update(returncode=17),
                       lambda v: v.update(outputComplete=False), lambda v: v.update(logSha256='b' * 64),
                       lambda v: v.update(command=['time.exe', *v['command']]),
                       lambda v: v['ownership'].update(assignedBeforeResume=False),
                       lambda v: v['cleanup'].update(activeProcessesAfter=1),
                       lambda v: v['cleanup'].update(forcedTreeTermination=True),
                       lambda v: v.update(completedAtUtc=v['startedAtUtc']),
                       lambda v: v['peakMemory'].update(field='sampledWorkingSetSize'),
                       lambda v: v['peakMemory'].update(queryPhase='sampled-before-exit'),
                       lambda v: v['peakMemory'].update(peakWorkingSetSizeBytes=True)]:
            altered = copy.deepcopy(value); change(altered)
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                self.validate(altered)
        altered = copy.deepcopy(value)
        altered['peakMemory']['windowsProcess'] = identity(created=1)
        with self.assertRaises(ValueError):
            self.validate(altered)
        altered = copy.deepcopy(value)
        altered['windowsProcess']['imagePath'] = r'C:\other\python.exe'
        with self.assertRaises(ValueError):
            self.validate(altered)

    def test_pid_reuse_is_distinct_and_access_denial_is_not_assumed_exit(self):
        with patch.object(windows, 'process_identity', return_value=identity(created=1)):
            self.assertFalse(windows.same_process(identity()))
        with patch.object(windows, 'process_identity', side_effect=ProcessLookupError()):
            self.assertFalse(windows.same_process(identity()))
        with patch.object(windows, 'process_identity', side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                windows.same_process(identity())

    def test_acl_rejects_extra_inherited_or_wrong_owner_permissions(self):
        value = dict(ownerSid='S-1-5-21-123', currentUserSid='S-1-5-21-123', daclProtected=True,
                     entries=[dict(type=0, flags=0, mask=0x1f01ff, sid=s)
                              for s in ['S-1-5-18', 'S-1-5-21-123']])
        self.assertEqual(value, windows.validate_private_acl(value, False))
        for change in [lambda v: v.update(ownerSid='S-1-5-32-544'),
                       lambda v: v.update(daclProtected=False),
                       lambda v: v['entries'].append(dict(type=0, flags=0, mask=0x1f01ff, sid='S-1-1-0')),
                       lambda v: v['entries'][0].update(flags=16),
                       lambda v: v['entries'][0].update(mask=1),
                       lambda v: v['entries'][0].update(type=1)]:
            altered = copy.deepcopy(value); change(altered)
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                windows.validate_private_acl(altered, False)

    def test_port_owner_preserves_actual_wildcard_binding_instead_of_claiming_loopback(self):
        api = MagicMock(); api.listeners.return_value = [dict(state=2, localPort=54321, localAddress='0.0.0.0', pid=123)]
        with patch.object(windows, '_api', return_value=api), patch.object(windows, 'process_identity', return_value=identity()):
            self.assertEqual('0.0.0.0', windows.port_owner(54321)['localAddress'])
            self.assertFalse(windows.port_closed(54321))
            with self.assertRaises(ValueError):
                windows.port_owner(54321, 'example.com')

    def test_owned_job_assignment_precedes_first_resume_and_cleanup_is_bounded(self):
        events = []; api = MagicMock(); process = MagicMock(pid=123, _handle=456)
        api.create_job.return_value = 789
        api.identity_from_handle.return_value = identity()
        api.assign_job.side_effect = lambda *a: events.append('assign')
        api.resume_only_thread.side_effect = lambda *a: events.append('resume')
        api.active_processes.side_effect = [1, 0]
        api.terminate_job.side_effect = lambda *a: events.append('terminate')
        api.close.side_effect = lambda *a: events.append('close')
        with patch.object(windows, '_api', return_value=api), patch.object(windows.subprocess, 'Popen', return_value=process) as popen:
            owned = windows.OwnedProcess(['unit-only.exe'])
            self.assertEqual(['assign', 'resume'], events)
            self.assertTrue(popen.call_args.kwargs['creationflags'] & 4)
            self.assertEqual({'activeProcessesAfter': 0, 'jobClosed': True,
                              'forcedTreeTermination': True}, owned.close())
        self.assertEqual(['assign', 'resume', 'terminate', 'close'], events)

    def test_graceful_job_close_and_prior_explicit_termination_remain_distinct(self):
        for explicit_termination in [False, True]:
            with self.subTest(explicit_termination=explicit_termination):
                api = MagicMock(); process = MagicMock(pid=123, _handle=456)
                api.identity_from_handle.return_value = identity()
                api.active_processes.return_value = 0
                with patch.object(windows, '_api', return_value=api), \
                        patch.object(windows.subprocess, 'Popen', return_value=process):
                    owned = windows.OwnedProcess(['unit-only.exe'])
                    if explicit_termination:
                        owned.terminate_tree()
                    cleanup = owned.close()
                    self.assertIs(explicit_termination, cleanup['forcedTreeTermination'])
                    self.assertEqual(0, cleanup['activeProcessesAfter'])
                    self.assertIs(cleanup, owned.close())
                    self.assertEqual(int(explicit_termination), api.terminate_job.call_count)

    def test_final_kernel_query_failure_is_retained_and_cannot_become_a_sampled_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); owned = MagicMock()
            owned.identity = identity()
            owned.process.pid = 123; owned.process.returncode = 0; owned.process.poll.return_value = 0
            owned.process.stdout = io.BytesIO(b'unit-mocked-child-output\n')
            owned.peak_after_exit.side_effect = OSError('unit simulated kernel query failure')
            owned.close.return_value = dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False)
            with patch.object(command_evidence, '_windows_host', return_value=True), \
                    patch.object(windows, 'OwnedProcess', return_value=owned):
                with self.assertRaisesRegex(RuntimeError, 'kernel peak'):
                    command_evidence.run_with_evidence([r'C:\unit.exe'], cwd=root,
                                                      directory=root/'evidence', timeout=10, windows_peak=True)
            actual = json.loads((root/'evidence/001.json').read_text(encoding='utf-8'))
            self.assertEqual('failed', actual['status'])
            self.assertEqual(0, actual['returncode'])
            self.assertTrue(actual['outputComplete'])
            self.assertNotIn('peakMemory', actual)
            self.assertEqual('OSError', actual['finalPeakFailure']['type'])
            self.assertEqual(hashlib.sha256((root/'evidence/001.log').read_bytes()).hexdigest(), actual['logSha256'])
            owned.close.assert_called_once()

    def test_final_peak_keeps_live_image_and_queries_only_the_held_handle(self):
        api = MagicMock(); process = MagicMock(pid=123, _handle=456, returncode=0)
        process.poll.return_value = 0
        api.identity_from_handle.return_value = identity()
        api.creation_time_from_handle.return_value = identity()['creationTime100ns']
        api.memory.return_value = 32 * 1024 * 1024
        with patch.object(windows, '_api', return_value=api), \
                patch.object(windows.subprocess, 'Popen', return_value=process):
            owned = windows.OwnedProcess(['unit-only.exe'])
            # Actual Windows CI rejected the image query after this child exited.
            api.identity_from_handle.side_effect = OSError('post-exit image unavailable')
            peak = owned.peak_after_exit()
            self.assertEqual(identity(), peak['windowsProcess'])
            self.assertEqual(32 * 1024 * 1024, peak['peakWorkingSetSizeBytes'])
            api.creation_time_from_handle.assert_called_once_with(456)
            api.memory.assert_called_once_with(456)
            api.identity_from_handle.assert_called_once_with(456, 123)
            api.open_process.assert_not_called()
            for pid, created in [(124, identity()['creationTime100ns']), (123, 1)]:
                process.pid = pid; api.creation_time_from_handle.return_value = created
                with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                    owned.peak_after_exit()
            process.pid = 123; process.poll.return_value = None
            with self.assertRaisesRegex(ValueError, 'actual process exit'):
                owned.peak_after_exit()
            self.assertEqual(1, api.memory.call_count)

    def test_exit_wait_uses_creation_time_without_querying_a_terminated_image(self):
        for created, signaled, expected in [(identity()['creationTime100ns'], True, True),
                                            (identity()['creationTime100ns'], False, False),
                                            (1, False, True)]:
            with self.subTest(created=created, signaled=signaled):
                api = MagicMock(); api.open_process.return_value = 456
                api.creation_time_from_handle.return_value = created
                api.exited.return_value = signaled
                api.identity_from_handle.side_effect = OSError('post-exit image unavailable')
                with patch.object(windows, '_api', return_value=api):
                    self.assertIs(expected, windows.wait_process_exit(identity(), 10))
                api.identity_from_handle.assert_not_called()
                api.close.assert_called_once_with(456)
        api = MagicMock(); api.creation_time_from_handle.side_effect = PermissionError('query denied')
        with patch.object(windows, '_api', return_value=api), self.assertRaises(PermissionError):
            windows.wait_process_exit(identity(), 10)
        api.close.assert_called_once()

    def test_live_identity_query_race_requires_observed_exit_on_the_same_handle(self):
        for signaled in [True, False]:
            with self.subTest(signaled=signaled):
                api = object.__new__(windows.Win32)
                api.open_process = MagicMock(return_value=456)
                api.close = MagicMock()
                api.exited = MagicMock(side_effect=[False, signaled])
                denied = PermissionError('post-exit image unavailable or live access denied')
                api.identity_from_handle = MagicMock(side_effect=denied)
                with self.assertRaises(ProcessLookupError if signaled else PermissionError) as caught:
                    api.identity(123)
                if not signaled:
                    self.assertIs(denied, caught.exception)
                self.assertEqual([(456,), (456,)],
                                 [call.args for call in api.exited.call_args_list])
                api.close.assert_called_once_with(456)


@unittest.skipUnless(os.name == 'nt', 'Actual Windows kernel/DACL acceptance runs only on Windows')
class ActualWindowsKernelTest(unittest.TestCase):
    def test_real_owned_python_final_peak_and_private_acl_and_loopback_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); windows.private_directory(root)
            secret = root / 'private-unit.txt'; secret.touch(); windows.private_file(secret)
            secret.write_text('unit-only', encoding='utf-8')
            self.assertTrue(windows.verify_private_path(secret)['daclProtected'])
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); listener.listen()
                owner = windows.port_owner(listener.getsockname()[1])
                self.assertEqual((os.getpid(), '127.0.0.1'), (owner['pid'], owner['localAddress']))
                self.assertTrue(windows.same_process(owner))
            command = [sys.executable, '-c', 'x=bytearray(16*1024*1024); x[:]=b"x"*len(x); print(len(x))']
            record = windows.run_diagnostic(command, root/'stdout.log', root/'peak.json', root/'command.json', cwd=root)
            peak = json.loads((root/'peak.json').read_text(encoding='utf-8'))
            windows.validate_execution_record(record, command=command, cwd=root,
                                               log_sha256=hashlib.sha256((root/'stdout.log').read_bytes()).hexdigest(),
                                               peak_record=peak)
            self.assertGreater(peak['peakWorkingSetSizeBytes'], 16*1024*1024)


if __name__ == '__main__':
    unittest.main()
