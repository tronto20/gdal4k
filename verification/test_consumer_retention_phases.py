"""Portable evidence/deadline guards, not Windows kernel/runtime acceptance."""
import errno
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import consumer_evidence as evidence


class ConsumerRetentionPhasesTest(unittest.TestCase):
    def fixture(self, root):
        consumer = Path(root) / 'consumer'
        consumer.mkdir()
        source = consumer / 'build/test-results/nativeTest/partial.xml'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'<testsuite tests="0"/>')
        return consumer, Path(root) / 'evidence', source

    def test_nonlink_directory_alias_returns_same_canonical_identity(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned).resolve()
            (root / 'existing').mkdir()
            alias = root / 'existing' / '..'
            self.assertNotEqual(alias, root)
            self.assertEqual(evidence._directory(alias, create=False), root)
            self.assertTrue(alias.samefile(root))

    def test_canonical_resolution_to_another_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned).resolve()
            source, other = root / 'source', root / 'other'
            source.mkdir(); other.mkdir()
            original = Path.resolve
            def resolve(path, *args, **kwargs):
                return other if path == source else original(path, *args, **kwargs)
            with mock.patch.object(Path, 'resolve', resolve):
                with self.assertRaisesRegex(RuntimeError, 'canonical identity differs'):
                    evidence._directory(source, create=False)
            self.assertEqual(list(source.iterdir()), [])
            self.assertEqual(list(other.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows 8.3 directory alias control')
    def test_actual_windows_short_alias_preserves_partial_xml(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-retention-long-name-') as owned:
            root = Path(owned).resolve()
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            short_path = kernel.GetShortPathNameW
            short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            short_path.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = short_path(str(root), buffer, len(buffer))
            if not length:
                raise ctypes.WinError(ctypes.get_last_error())
            self.assertLess(length, len(buffer))
            alias = Path(buffer.value)
            if alias == root:
                self.skipTest('This actual Windows filesystem exposes no distinct 8.3 alias for the owned directory')
            self.assertTrue(alias.samefile(root))
            consumer, destination, source = self.fixture(alias)
            contents = source.read_bytes()
            failure = subprocess.TimeoutExpired(['owned-native-fixture'], 3540)
            result = evidence.preserve_consumer_evidence(consumer, destination, failure)
            self.assertFalse(result.get('incomplete'), repr(result))
            self.assertEqual((destination / 'test-results/nativeTest/partial.xml').read_bytes(), contents)
            self.assertEqual(json.loads((destination / 'failure.json').read_text())['exception'], 'TimeoutExpired')
            self.assertEqual(evidence._directory(consumer, create=False), consumer.resolve())

    def test_failure_identity_is_durable_before_copytree(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            failure = subprocess.TimeoutExpired(['actual-native'], 3540)
            def copying(*args, **kwargs):
                record = json.loads((destination / 'failure.json').read_text())
                self.assertEqual(record['exception'], 'TimeoutExpired')
                self.assertEqual(record['artifact_preservation'], {'hardlinked_files': 0, 'copied_files': 0})
                raise OSError('injected copytree failure')
            recorder = evidence.PhaseRecorder(Path(owned) / 'stable-phases')
            with mock.patch.object(evidence.shutil, 'copytree', side_effect=copying):
                result = evidence.preserve_consumer_evidence(consumer, destination, failure, phase_recorder=recorder)
            self.assertTrue(result['incomplete'])
            record = json.loads((destination / 'failure.json').read_text())
            self.assertEqual(record['exception'], 'TimeoutExpired')
            self.assertEqual(record['artifact_preservation']['errors'][0]['exception'], 'OSError')
            phases = [json.loads(path.read_text()) for path in sorted(recorder.directory.iterdir())]
            self.assertEqual([(r['phase'], r['event']) for r in phases], [('preserve', 'before'), ('copy-test-results', 'before'), ('copy-test-results', 'error')])
            self.assertIn('phaseElapsedSeconds', phases[-1])

    def test_secondary_failure_json_update_cannot_mask_timeout(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            original = evidence._atomic_json
            calls = []
            def write(path, value):
                calls.append(path)
                if len(calls) > 1:
                    raise OSError('final record disk error')
                original(path, value)
            failure = subprocess.TimeoutExpired(['actual-native'], 3540)
            with mock.patch.object(evidence, '_atomic_json', side_effect=write):
                result = evidence.preserve_consumer_evidence(consumer, destination, failure)
            self.assertTrue(result['incomplete'])
            self.assertEqual(json.loads((destination / 'failure.json').read_text())['exception'], 'TimeoutExpired')
            self.assertIn('final record disk error', result['errors'][0]['message'])

    def test_initial_record_failure_does_not_start_bulk_or_hide_timeout(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            with mock.patch.object(evidence, '_atomic_json', side_effect=OSError('metadata full')), mock.patch.object(evidence.shutil, 'copytree') as copying:
                result = evidence.preserve_consumer_evidence(consumer, destination, subprocess.TimeoutExpired(['native'], 3540))
            self.assertTrue(result['incomplete'])
            copying.assert_not_called()

    def test_success_never_swallows_preservation_failure(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            with mock.patch.object(evidence.shutil, 'copytree', side_effect=OSError('cannot retain')):
                with self.assertRaisesRegex(OSError, 'cannot retain'):
                    evidence.preserve_consumer_evidence(consumer, destination)

    def test_expired_failure_retains_identity_and_explicit_omission(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            with mock.patch.object(evidence.time, 'monotonic', return_value=100), mock.patch.object(evidence.shutil, 'copytree') as copying:
                result = evidence.preserve_consumer_evidence(consumer, destination, subprocess.TimeoutExpired(['native'], 3540), deadline=99)
            copying.assert_not_called()
            self.assertTrue(result['incomplete'])
            self.assertTrue(result['omitted'])
            self.assertEqual(json.loads((destination / 'failure.json').read_text())['exception'], 'TimeoutExpired')

    def test_expired_success_refused_even_without_outputs(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer = Path(owned) / 'consumer'; consumer.mkdir()
            with mock.patch.object(evidence.time, 'monotonic', return_value=100):
                with self.assertRaises(TimeoutError): evidence.preserve_consumer_evidence(consumer, Path(owned) / 'evidence', deadline=99)

    def test_invalid_deadline_is_not_a_success_or_budget_extension(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            for deadline in [True, False, math.nan, math.inf, -math.inf, 0, -1, '100']:
                with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                    evidence.preserve_consumer_evidence(consumer, destination, deadline=deadline)

    def test_fallback_copy_not_started_in_final_reserve(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            with mock.patch.object(evidence.time, 'monotonic', return_value=100), mock.patch.object(evidence.os, 'link', side_effect=OSError(errno.EXDEV, 'different volume')), mock.patch.object(evidence.shutil, 'copy2') as copying:
                result = evidence.preserve_consumer_evidence(consumer, destination, RuntimeError('native failure'), deadline=105)
            copying.assert_not_called()
            self.assertTrue(result['incomplete'])
            self.assertIn('copy reserve', result['omitted'][0])

    def test_same_volume_hardlink_keeps_bytes_after_source_removal(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            contents = source.read_bytes()
            result = evidence.preserve_consumer_evidence(consumer, destination)
            retained = destination / 'test-results/nativeTest/partial.xml'
            self.assertEqual(result, {'hardlinked_files': 1, 'copied_files': 0})
            self.assertEqual(source.stat().st_ino, retained.stat().st_ino)
            source.unlink()
            self.assertEqual(retained.read_bytes(), contents)

    def test_cross_volume_fallback_count_and_bytes_are_explicit(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            with mock.patch.object(evidence.os, 'link', side_effect=OSError(errno.EXDEV, 'different volume')):
                result = evidence.preserve_consumer_evidence(consumer, destination)
            self.assertEqual(result, {'hardlinked_files': 0, 'copied_files': 1})
            self.assertEqual((destination / 'test-results/nativeTest/partial.xml').read_bytes(), source.read_bytes())

    def test_failed_copy_keeps_previous_retained_bytes(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            retained = destination / 'test-results/nativeTest/partial.xml'
            retained.parent.mkdir(parents=True); retained.write_bytes(b'earlier retained bytes')
            with mock.patch.object(evidence.os, 'link', side_effect=OSError(errno.EXDEV, 'different volume')), mock.patch.object(evidence.shutil, 'copy2', side_effect=OSError('copy stopped')):
                result = evidence.preserve_consumer_evidence(consumer, destination, RuntimeError('native failed'))
            self.assertTrue(result['incomplete'])
            self.assertEqual(retained.read_bytes(), b'earlier retained bytes')
            self.assertEqual(result['copied_files'], 0)
            self.assertEqual(list(retained.parent.glob('.*.tmp')), [])

    def test_phase_records_are_unique_bounded_fsynced_and_timed(self):
        with tempfile.TemporaryDirectory() as owned:
            clock = iter([100.0, 101.0, 103.5])
            recorder = evidence.PhaseRecorder(Path(owned) / 'stable', clock=lambda: next(clock))
            with mock.patch.object(evidence.os, 'fsync', wraps=evidence.os.fsync) as syncing:
                first = recorder.record('preserve', 'before', path='owned')
                last = recorder.record('preserve', 'after')
            self.assertNotEqual(first['path'], last['path'])
            self.assertGreaterEqual(syncing.call_count, 2)
            value = json.loads(Path(last['path']).read_text())
            self.assertEqual(value['phaseElapsedSeconds'], 2.5)
            self.assertEqual(value['elapsedSeconds'], 3.5)
            with self.assertRaises(ValueError): recorder.record('../outside', 'before')

    def test_phase_recorder_refuses_oversized_or_nonfinite_metadata(self):
        with tempfile.TemporaryDirectory() as owned:
            recorder = evidence.PhaseRecorder(Path(owned) / 'stable')
            with self.assertRaisesRegex(ValueError, '64KiB'): recorder.record('test', 'before', oversized='x' * evidence.MAX_PHASE_BYTES)
            with self.assertRaises(ValueError): recorder.record('test', 'before', value=math.nan)
            self.assertEqual(list(recorder.directory.iterdir()), [])

    def test_concurrent_phase_records_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as owned:
            recorder = evidence.PhaseRecorder(Path(owned) / 'stable')
            threads = [threading.Thread(target=recorder.record, args=('native', 'progress'), kwargs={'index': i}) for i in range(20)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            values = [json.loads(path.read_text()) for path in recorder.directory.iterdir()]
            self.assertEqual(len(values), 20)
            self.assertEqual({value['details']['index'] for value in values}, set(range(20)))

    def test_phase_aggregate_allocation_cap_rejects_before_new_file_bytes(self):
        with tempfile.TemporaryDirectory() as owned:
            with mock.patch.object(evidence, '_allocation_unit', return_value=4096):
                recorder = evidence.PhaseRecorder(Path(owned) / 'stable', max_total_bytes=8192)
            recorder.record('native', 'one'); recorder.record('native', 'two')
            before = {path.name: path.read_bytes() for path in recorder.directory.iterdir()}
            self.assertEqual(recorder.charged_bytes, 8192)
            with self.assertRaisesRegex(ValueError, 'aggregate allocation budget'):
                recorder.record('native', 'three')
            self.assertEqual({path.name: path.read_bytes() for path in recorder.directory.iterdir()}, before)
            self.assertEqual(recorder.charged_bytes, 8192)

    def test_exhausted_phase_quota_cannot_mask_primary_failure(self):
        with tempfile.TemporaryDirectory() as owned:
            with mock.patch.object(evidence, '_allocation_unit', return_value=4096):
                recorder = evidence.PhaseRecorder(Path(owned) / 'stable', max_total_bytes=4096)
            recorder.record('native', 'filled')
            failure = subprocess.TimeoutExpired(['native'], 3540)
            destination = Path(owned) / 'evidence'
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                with evidence.consumer_failure_guard(destination, phase_recorder=recorder): raise failure
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(list(recorder.directory.iterdir())), 1)
            self.assertEqual(json.loads((destination / 'failure.json').read_text())['exception'], 'TimeoutExpired')

    def test_invalid_phase_aggregate_budget_refused(self):
        with tempfile.TemporaryDirectory() as owned:
            for limit in [True, False, 0, -1, 1.0, math.nan, '100']:
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    evidence.PhaseRecorder(Path(owned) / 'stable', max_total_bytes=limit)

    def test_native_failure_atomic_versions_stay_within_reserved64k(self):
        with tempfile.TemporaryDirectory() as owned:
            path = Path(owned) / 'failure.json'; path.write_bytes(b'earlier')
            recorder = mock.Mock(max_total_bytes=960 * 1024)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'atomic-version budget'):
                evidence._write_failure(path, {'oversized': 'x' * (32 * 1024)}, recorder)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(owned).glob('.*.tmp')), [])

    def test_workspace_is_same_volume_ignored_owned_subtree(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned)
            workspace = evidence.consumer_workspace_root(root)
            expected = root / 'verification/build/consumer-workspaces'
            self.assertEqual(workspace, expected.resolve(strict=True))
            self.assertTrue(workspace.samefile(expected))
            with tempfile.TemporaryDirectory(prefix='consumer-', dir=workspace) as temporary:
                self.assertEqual(Path(temporary).stat().st_dev, (root / 'verification/results').stat().st_dev)
                (Path(temporary) / 'settings.gradle.kts').write_text('rootProject.name="isolated"')
            self.assertEqual(list(workspace.iterdir()), [])

    def test_workspace_context_removes_only_owned_project_and_records_timing(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned); protected = root / 'protected'; protected.mkdir(); (protected / 'input').write_bytes(b'keep')
            recorder = evidence.PhaseRecorder(root / 'stable-phases')
            with evidence.consumer_workspace(root, phase_recorder=recorder) as temporary:
                project = Path(temporary)
                self.assertEqual(project.stat().st_dev, (root / 'verification/results').stat().st_dev)
                (project / 'private-input').write_bytes(b'only temporary')
                (project / 'external-link').symlink_to(protected, target_is_directory=True)
            self.assertFalse(project.exists())
            self.assertEqual((protected / 'input').read_bytes(), b'keep')
            events = [json.loads(path.read_text()) for path in sorted(recorder.directory.iterdir())]
            self.assertEqual([(v['phase'], v['event']) for v in events], [('workspace-delete', 'before'), ('workspace-delete', 'after')])
            self.assertIn('phaseElapsedSeconds', events[-1])

    def test_workspace_deadline_failure_keeps_primary_and_records_owned_omission(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned); recorder = evidence.PhaseRecorder(root / 'stable-phases'); failure = RuntimeError('original native failure')
            now = [100]
            with mock.patch.object(evidence.time, 'monotonic', side_effect=lambda: now[0]):
                with self.assertRaises(RuntimeError) as caught:
                    with evidence.consumer_workspace(root, phase_recorder=recorder, deadline=101) as temporary:
                        project = Path(temporary); now[0] = 102; raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(project.exists())
            record = json.loads(next(recorder.directory.iterdir()).read_text())
            self.assertEqual(record['event'], 'omitted')
            self.assertEqual(record['details']['path'], str(project))
            self.assertTrue(record['details']['ownedPathRemaining'])
            self.assertTrue(any(str(project) in note for note in failure.__notes__))
            shutil.rmtree(project)

    def test_workspace_expired_success_never_looks_like_completion(self):
        with tempfile.TemporaryDirectory() as owned:
            now = [100]
            with mock.patch.object(evidence.time, 'monotonic', side_effect=lambda: now[0]):
                with self.assertRaises(TimeoutError):
                    with evidence.consumer_workspace(Path(owned), deadline=101) as temporary:
                        project = Path(temporary); now[0] = 102
            self.assertTrue(project.exists())
            shutil.rmtree(project)

    def test_workspace_cleanup_start_reserve_boundary_and_epsilon(self):
        with tempfile.TemporaryDirectory() as owned:
            with mock.patch.object(evidence.time, 'monotonic', return_value=100):
                with self.assertRaises(TimeoutError):
                    with evidence.consumer_workspace(Path(owned), deadline=110) as temporary:
                        omitted = Path(temporary)
                self.assertTrue(omitted.exists())
                with evidence.consumer_workspace(Path(owned), deadline=110.000001) as temporary:
                    cleaned = Path(temporary)
                self.assertFalse(cleaned.exists())
            shutil.rmtree(omitted)

    def test_workspace_cleanup_failure_cannot_replace_body_exception(self):
        with tempfile.TemporaryDirectory() as owned:
            failure = subprocess.TimeoutExpired(['native'], 3540)
            with mock.patch.object(evidence.tempfile.TemporaryDirectory, 'cleanup', side_effect=OSError('injected cleanup block')):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    with evidence.consumer_workspace(Path(owned)) as temporary:
                        project = Path(temporary); raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(project.exists())
            self.assertTrue(any('injected cleanup block' in note for note in failure.__notes__))
            shutil.rmtree(project)

    def test_workspace_successful_body_cleanup_failure_keeps_exact_error_and_path(self):
        with tempfile.TemporaryDirectory() as owned:
            failure = OSError('actual cleanup failure')
            recorder = mock.Mock()
            recorder.record.side_effect = [None, OSError('secondary record failure')]
            with mock.patch.object(evidence.tempfile.TemporaryDirectory, 'cleanup', side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    with evidence.consumer_workspace(Path(owned), phase_recorder=recorder) as temporary:
                        project = Path(temporary)
            self.assertIs(caught.exception, failure)
            self.assertTrue(any(str(project) in note for note in failure.__notes__))
            self.assertTrue(any('secondary record failure' in note for note in failure.__notes__))
            shutil.rmtree(project)

    def test_workspace_and_phase_recorder_refuse_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned); outside = root / 'outside'; outside.mkdir()
            (root / 'verification').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'symlinked/reparse'): evidence.consumer_workspace_root(root)
            with self.assertRaisesRegex(RuntimeError, 'symlinked/reparse'): evidence.PhaseRecorder(root / 'verification/phases')
            self.assertEqual(list(outside.iterdir()), [])

    def test_previous_success_is_archived_before_first_copy(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned); destination.mkdir()
            (destination / 'result.json').write_text('{"result":"passed"}\n')
            def copying(*args, **kwargs):
                self.assertFalse((destination / 'result.json').exists())
                self.assertEqual(json.loads((destination / 'result-before-failure-0.json').read_text())['result'], 'passed')
                self.assertEqual(json.loads((destination / 'failure.json').read_text())['result'], 'failed')
                raise OSError('copy failed')
            with mock.patch.object(evidence.shutil, 'copytree', side_effect=copying):
                evidence.preserve_consumer_evidence(consumer, destination, RuntimeError('original failure'))

    def hidden(self, root, count=3):
        pairs = []
        for index in range(count):
            original = Path(root) / ('producer-' + str(index))
            moved = original.with_name(original.name + '-consumer-hidden')
            moved.mkdir(); (moved / 'input').write_text(str(index))
            pairs.append((original, moved))
        return pairs

    def test_restore_attempts_all_paths_despite_every_phase_record_failing(self):
        with tempfile.TemporaryDirectory() as owned:
            hidden = self.hidden(owned); primary = subprocess.TimeoutExpired(['native'], 3540)
            recorder = mock.Mock(); recorder.record.side_effect = OSError('phase disk failure')
            report = evidence.restore_hidden_paths(hidden, phase_recorder=recorder, failure=primary)
            self.assertEqual(report['attempted'], 3)
            self.assertEqual(len(report['restored']), 3)
            self.assertEqual(len(report['errors']), 6)
            self.assertEqual(primary.consumer_restore_report, report)
            self.assertEqual(len(primary.consumer_restore_errors), 6)
            for index, (original, moved) in enumerate(hidden):
                self.assertEqual((original / 'input').read_text(), str(index))
                self.assertFalse(moved.exists())
            self.assertTrue(primary.__notes__)

    def test_restore_phase_error_fails_success_after_restoring_every_path(self):
        with tempfile.TemporaryDirectory() as owned:
            hidden = self.hidden(owned)
            recorder = mock.Mock(); recorder.record.side_effect = OSError('phase failed')
            with self.assertRaisesRegex(RuntimeError, 'restoration/phase recording failed') as caught:
                evidence.restore_hidden_paths(hidden, phase_recorder=recorder)
            self.assertEqual(len(caught.exception.consumer_restore_report['restored']), 3)
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertTrue(all(original.exists() and not moved.exists() for original, moved in hidden))

    def test_restore_refuses_unowned_sibling_and_still_restores_owned_paths(self):
        with tempfile.TemporaryDirectory() as owned:
            hidden = self.hidden(owned, 2)
            wrong = Path(owned) / 'unrelated'; wrong.write_bytes(b'protected')
            target = Path(owned) / 'would-replace'
            hidden.insert(1, (target, wrong))
            with self.assertRaises(RuntimeError) as caught: evidence.restore_hidden_paths(hidden)
            self.assertEqual(caught.exception.consumer_restore_report['attempted'], 3)
            self.assertEqual(len(caught.exception.consumer_restore_report['restored']), 2)
            self.assertEqual(wrong.read_bytes(), b'protected')
            self.assertFalse(target.exists())

    def test_restore_existing_original_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as owned:
            hidden = self.hidden(owned, 1)
            original, moved = hidden[0]; original.mkdir(); (original / 'keep').write_text('prior producer')
            failure = subprocess.TimeoutExpired(['native'], 3540)
            report = evidence.restore_hidden_paths(hidden, failure=failure)
            self.assertEqual(report['restored'], [])
            self.assertEqual((original / 'keep').read_text(), 'prior producer')
            self.assertEqual((moved / 'input').read_text(), '0')

    def test_owned_hidden_link_is_restored_as_link_without_mutating_target(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned); protected = root / 'protected'; protected.mkdir(); (protected / 'keep').write_bytes(b'unchanged')
            original = root / 'producer'; moved = root / 'producer-consumer-hidden'
            moved.symlink_to(protected, target_is_directory=True)
            report = evidence.restore_hidden_paths([(original, moved)])
            self.assertEqual(len(report['restored']), 1)
            self.assertTrue(original.is_symlink())
            self.assertEqual(original.resolve(strict=True), protected.resolve(strict=True))
            self.assertTrue(original.samefile(protected))
            self.assertEqual((protected / 'keep').read_bytes(), b'unchanged')

    def test_failure_guard_catches_workspace_exit_and_archives_provisional_success(self):
        with tempfile.TemporaryDirectory() as owned:
            root = Path(owned); destination = root / 'evidence'; destination.mkdir()
            failure = OSError('actual workspace cleanup failure')
            with mock.patch.object(evidence.tempfile.TemporaryDirectory, 'cleanup', side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    with evidence.consumer_failure_guard(destination), evidence.consumer_workspace(root) as temporary:
                        project = Path(temporary)
                        (destination / 'result.json').write_text('{"result":"passed"}\n')
            self.assertIs(caught.exception, failure)
            self.assertFalse((destination / 'result.json').exists())
            self.assertEqual(json.loads((destination / 'result-before-failure-0.json').read_text())['result'], 'passed')
            self.assertEqual(json.loads((destination / 'failure.json').read_text())['exception'], 'OSError')
            shutil.rmtree(project)

    def test_failure_guard_phase_errors_do_not_hide_primary_or_identity(self):
        with tempfile.TemporaryDirectory() as owned:
            destination = Path(owned) / 'evidence'
            failure = subprocess.TimeoutExpired(['native'], 3540)
            recorder = mock.Mock(); recorder.record.side_effect = OSError('phase unavailable')
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                with evidence.consumer_failure_guard(destination, phase_recorder=recorder): raise failure
            self.assertIs(caught.exception, failure)
            record = json.loads((destination / 'failure.json').read_text())
            self.assertEqual(record['exception'], 'TimeoutExpired')
            self.assertEqual(len(record['consumer_evidence_errors']), 2)

    def test_failure_guard_directory_error_does_not_replace_primary(self):
        with tempfile.TemporaryDirectory() as owned:
            destination = Path(owned) / 'not-directory'; destination.write_bytes(b'keep')
            failure = subprocess.TimeoutExpired(['native'], 3540)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                with evidence.consumer_failure_guard(destination): raise failure
            self.assertIs(caught.exception, failure)
            self.assertTrue(any('failure-guard-directory' in note for note in failure.__notes__))
            self.assertEqual(destination.read_bytes(), b'keep')

    def test_failure_guard_previous_rename_error_still_writes_identity(self):
        with tempfile.TemporaryDirectory() as owned:
            destination = Path(owned) / 'evidence'; destination.mkdir(); (destination / 'result.json').write_text('{"result":"passed"}\n')
            failure = subprocess.TimeoutExpired(['native'], 3540)
            with mock.patch.object(Path, 'rename', side_effect=OSError('archive denied')):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    with evidence.consumer_failure_guard(destination): raise failure
            self.assertIs(caught.exception, failure)
            record = json.loads((destination / 'failure.json').read_text())
            self.assertEqual(record['exception'], 'TimeoutExpired')
            self.assertEqual(record['consumer_evidence_errors'][0]['stage'], 'failure-guard-archive-success')

    def test_preserve_initial_validation_cannot_replace_existing_timeout(self):
        with tempfile.TemporaryDirectory() as owned:
            consumer, destination, source = self.fixture(owned)
            for invalid in ['consumer', 'deadline', 'archive']:
                with self.subTest(invalid=invalid):
                    failure = subprocess.TimeoutExpired(['native'], 3540)
                    if invalid == 'consumer':
                        result = evidence.preserve_consumer_evidence(Path(owned) / 'absent', destination, failure)
                    elif invalid == 'deadline':
                        result = evidence.preserve_consumer_evidence(consumer, destination, failure, deadline=float('nan'))
                    else:
                        destination.mkdir(exist_ok=True); (destination / 'result.json').write_text('{"result":"passed"}\n')
                        with mock.patch.object(Path, 'rename', side_effect=OSError('rename failed')):
                            result = evidence.preserve_consumer_evidence(consumer, destination, failure)
                    self.assertTrue(result['incomplete'])
                    self.assertEqual(failure.consumer_preservation, result)
                    self.assertTrue(failure.__notes__)

    def test_failure_guard_does_not_create_failure_on_success(self):
        with tempfile.TemporaryDirectory() as owned:
            destination = Path(owned) / 'not-created'
            with evidence.consumer_failure_guard(destination): pass
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
