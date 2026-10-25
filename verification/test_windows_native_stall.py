"""Actual Win32 capture tests run only on Windows; parser/phase tests are portable."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import windows_native_stall as native
import windows_process as windows


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now


def phase(index=1, suffix='before', trial=0, driver='GPKG'):
    return f'    GDAL4K_VECTOR_COLUMNS_PHASE trial={trial} driver={driver} phase=read-{index}-{suffix}\r\n'


def synthetic_dump(path, flags=native.FLAGS):
    name = r'C:\fixture\gdal4k.dll'.encode('utf-16le')
    # Header+directory(68), threadlist(52), modulelist(112), threadinfo(76), context(128), name.
    header = struct.pack('<IIIIIIQ', 0x504d444d, 0, 3, 32, 0, 0, flags)
    directory = struct.pack('<III', 3, 52, 68) + struct.pack('<III', 4, 112, 120) + struct.pack('<III', 17, 76, 232)
    thread = bytearray(48); struct.pack_into('<I', thread, 0, 123); struct.pack_into('<II', thread, 40, 128, 308)
    module = bytearray(108); struct.pack_into('<QIIII', module, 0, 0x10000000, 4096, 0, 42, 436)
    path.write_bytes(header + directory + struct.pack('<I', 1) + thread + struct.pack('<I', 1) + module +
                     struct.pack('<III', 12, 64, 1) + bytes(64) + bytes(128) + struct.pack('<I', len(name)) + name)


class PhaseAndParserTest(unittest.TestCase):
    def log_stat(self, actual, **changes):
        value = dict(st_dev=actual.st_dev, st_ino=actual.st_ino, st_size=actual.st_size,
                     st_mtime_ns=actual.st_mtime_ns, st_ctime_ns=1000, st_mode=actual.st_mode,
                     st_file_attributes=0, st_reparse_tag=0, st_birthtime_ns=1000)
        value.update(changes)
        return SimpleNamespace(**value)

    def test_tail_accepts_stable_cross_api_ctime_meanings_with_strict_same_api_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; raw = b'actual log tail\n'; path.write_bytes(raw)
            actual = path.stat(); before = self.log_stat(actual); opened = self.log_stat(actual, st_ctime_ns=2000)
            with patch.object(Path, 'lstat', side_effect=[before, before]), patch.object(Path, 'is_symlink', return_value=False), \
                    patch.object(native.os, 'fstat', side_effect=[opened, opened]):
                result = native._collector_failure_log(root)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), result['tailSha256'])
            self.assertEqual(raw.decode(), result['tailUtf8Replace'])

    def test_tail_refuses_changed_path_or_fd_ctime_with_exact_labelled_signatures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; path.write_bytes(b'owned log\n'); actual = path.stat()
            for family in ['path', 'fd']:
                before = self.log_stat(actual); opened = self.log_stat(actual, st_ctime_ns=2000)
                path_after = self.log_stat(actual, st_ctime_ns=1001) if family == 'path' else before
                fd_after = self.log_stat(actual, st_ctime_ns=2001) if family == 'fd' else opened
                with self.subTest(family=family), patch.object(Path, 'lstat', side_effect=[before, path_after]), \
                        patch.object(Path, 'is_symlink', return_value=False), patch.object(native.os, 'fstat', side_effect=[opened, fd_after]):
                    with self.assertRaisesRegex(ValueError, 'changed during bounded reading') as caught:
                        native._collector_failure_log(root)
                observed = caught.exception.collectorLogSignatures
                self.assertEqual('1000', observed['pathLstatBefore']['ctimeNs'])
                self.assertEqual('2000', observed['heldFdFstatBefore']['ctimeNs'])
                self.assertEqual(str(path_after.st_ctime_ns), observed['pathLstatAfter']['ctimeNs'])
                self.assertEqual(str(fd_after.st_ctime_ns), observed['heldFdFstatAfter']['ctimeNs'])

    def test_tail_refuses_cross_api_identity_size_mtime_birthtime_changes_or_zero_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; path.write_bytes(b'owned log\n'); actual = path.stat()
            changes = [dict(st_dev=actual.st_dev + 1), dict(st_ino=actual.st_ino + 1),
                       dict(st_size=actual.st_size + 1), dict(st_mtime_ns=actual.st_mtime_ns + 1),
                       dict(st_birthtime_ns=1001), dict(st_dev=0), dict(st_ino=0)]
            for change in changes:
                before = self.log_stat(actual); opened = self.log_stat(actual, **change)
                with self.subTest(change=change), patch.object(Path, 'lstat', return_value=before), \
                        patch.object(Path, 'is_symlink', return_value=False), patch.object(native.os, 'fstat', return_value=opened):
                    with self.assertRaisesRegex(ValueError, 'identity changed before reading') as caught:
                        native._collector_failure_log(root)
                self.assertEqual({'pathLstatBefore', 'heldFdFstatBefore'}, set(caught.exception.collectorLogSignatures))

    def test_tail_refuses_metadata_identity_or_reparse_changes_after_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; path.write_bytes(b'owned log\n'); actual = path.stat()
            changes = [dict(st_dev=actual.st_dev + 1), dict(st_ino=actual.st_ino + 1), dict(st_size=actual.st_size + 1),
                       dict(st_mtime_ns=actual.st_mtime_ns + 1), dict(st_mode=stat.S_IFLNK | 0o777),
                       dict(st_file_attributes=0x400), dict(st_reparse_tag=0xa000000c)]
            for family in ['path', 'fd']:
                for change in changes:
                    before = self.log_stat(actual); changed = self.log_stat(actual, **change)
                    path_after = changed if family == 'path' else before
                    fd_after = changed if family == 'fd' else before
                    with self.subTest(family=family, change=change), patch.object(Path, 'lstat', side_effect=[before, path_after]), \
                            patch.object(Path, 'is_symlink', return_value=False), patch.object(native.os, 'fstat', side_effect=[before, fd_after]):
                        with self.assertRaisesRegex(ValueError, 'changed during bounded reading'):
                            native._collector_failure_log(root)

    def test_tail_refuses_nonregular_or_reparse_metadata_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; path.write_bytes(b'owned log\n'); actual = path.stat()
            for change in [dict(st_mode=stat.S_IFLNK | 0o777), dict(st_file_attributes=0x400), dict(st_reparse_tag=0xa000000c)]:
                before = self.log_stat(actual, **change)
                with self.subTest(change=change), patch.object(Path, 'lstat', return_value=before), \
                        patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'open') as opening:
                    with self.assertRaisesRegex(ValueError, 'regular nonreparse'):
                        native._collector_failure_log(root)
                opening.assert_not_called()

    def test_tail_signature_refusal_is_serialized_in_failed_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'; path.write_bytes(b'owned log\n'); actual = path.stat()
            before = self.log_stat(actual); opened = self.log_stat(actual, st_ino=actual.st_ino + 1)
            process = MagicMock(); process.poll.return_value = 1
            with patch.object(Path, 'lstat', return_value=before), patch.object(Path, 'is_symlink', return_value=False), \
                    patch.object(native.os, 'fstat', return_value=opened):
                result = native.CaptureSession(process, root, lambda: 0, 20).poll()
            self.assertEqual('collector-failed', result['status'])
            signatures = result['collectorErrorLogFailure']['signatures']
            self.assertEqual(str(actual.st_ino), signatures['pathLstatBefore']['inode'])
            self.assertEqual(str(actual.st_ino + 1), signatures['heldFdFstatBefore']['inode'])

    def test_failed_collector_receipt_and_bounded_error_tail_survive_both_modes(self):
        for bounded in [False, True]:
            with self.subTest(bounded=bounded), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                receipt = dict(status='captured', stage='write-minidump',
                               error=dict(type='RuntimeError', message='actual writer error'),
                               dumpQuota=dict(apiLastErrorUnsigned32=2147942487, apiLastErrorHex='0x80070057',
                                              miniDumpReturnCode=0, ioStartObserved=False))
                (root / 'collector.json').write_text(json.dumps(receipt))
                raw = b'not-in-tail-' * 4096 + b'\nactual collector traceback\xff\n'
                (root / 'collector.log').write_bytes(raw)
                process = MagicMock(); process.poll.return_value = 1
                result = native.CaptureSession(process, root, lambda: 0, 20,
                                               dict(boundedNativeMetadata=bounded)).poll()
                self.assertEqual('collector-failed', result['status'])
                self.assertEqual(1, result['returncode'])
                self.assertEqual(receipt['error'], result['error'])
                self.assertEqual(receipt['dumpQuota'], result['dumpQuota'])
                log = result['collectorErrorLog']; tail = raw[-32 * 1024:]
                self.assertEqual(len(raw) - len(tail), log['tailOffsetBytes'])
                self.assertEqual(len(tail), log['tailBytes'])
                self.assertEqual(hashlib.sha256(tail).hexdigest(), log['tailSha256'])
                self.assertEqual(tail.decode('utf-8', errors='replace'), log['tailUtf8Replace'])
                self.assertTrue(log['tailOnly'])

    def test_failed_collector_receipt_bound_precedes_json_read_for_both_modes(self):
        for bounded in [False, True]:
            with self.subTest(bounded=bounded), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / 'collector.json'
                path.write_bytes(b' ' * (256 * 1024 + 1))
                process = MagicMock(); process.poll.return_value = 1
                original = Path.read_text
                def no_oversize_read(candidate, *args, **kwargs):
                    self.assertNotEqual(candidate, path, 'Oversized failure JSON must not be read')
                    return original(candidate, *args, **kwargs)
                with patch.object(Path, 'read_text', no_oversize_read):
                    result = native.CaptureSession(process, root, lambda: 0, 20,
                                                   dict(boundedNativeMetadata=bounded)).poll()
                self.assertEqual('collector-invalid-receipt', result['status'])
                self.assertEqual(1, result['returncode'])
                self.assertIn('metadata quota', result['message'])

    def test_failed_collector_without_receipt_retains_tail_and_refuses_linked_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'collector.log'
            path.write_bytes(b'actual startup exception before request validation\n')
            process = MagicMock(); process.poll.return_value = 1
            result = native.CaptureSession(process, root, lambda: 0, 20).poll()
            self.assertEqual('collector-failed', result['status'])
            self.assertIn('actual startup exception', result['collectorErrorLog']['tailUtf8Replace'])
            with patch.object(Path, 'is_symlink', return_value=True), patch.object(Path, 'open') as opening:
                with self.assertRaisesRegex(ValueError, 'regular nonreparse'):
                    native._collector_failure_log(root)
            opening.assert_not_called()

    def test_exact_current_read_duplicate_and_late_after_do_not_reset_timer(self):
        clock = Clock(); tracker = native.PhaseTracker(clock)
        tracker.observe_line(phase().strip()); clock.now = 50
        tracker.observe_line(phase().strip())
        self.assertEqual(0, tracker.current['started'])
        tracker.observe_line(phase(index=0, suffix='after-rows-3').strip())
        self.assertEqual(1, tracker.current['readIndex'])
        tracker.observe_line(phase(suffix='after-rows-1').strip())
        self.assertIsNone(tracker.current)
        tracker.observe_line(phase(index=2).strip())
        self.assertEqual(50, tracker.current['started'])
        tracker.observe_line('GDAL4K_VECTOR_COLUMNS_PHASE trial=0 driver=GPKG phase=stream-close-before')
        self.assertIsNone(tracker.current)

    def test_non_exact_or_embedded_marker_is_not_a_phase(self):
        tracker = native.PhaseTracker()
        for line in ['prefix' + phase(), phase() + ' trailing', phase().replace('trial=0', 'trial=oops')]:
            tracker.observe_line(line)
            self.assertIsNone(tracker.current)

    def test_native_context_and_module_inventory_and_reject_invalid_dumps(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'native.dmp'; synthetic_dump(p)
            result = native.dump_inventory(p)
            self.assertEqual(123, result['nativeThreads'][0]['threadId'])
            self.assertEqual(r'C:\fixture\gdal4k.dll', result['modules'][0]['path'])
            self.assertFalse(result['fullMemoryRequested'])
            for kind in ['full-memory', 'bounds', 'missing-context', 'duplicate-stream', 'truncated']:
                synthetic_dump(p)
                data = bytearray(p.read_bytes())
                if kind == 'full-memory': struct.pack_into('<Q', data, 24, native.FLAGS | 2)
                elif kind == 'bounds': struct.pack_into('<I', data, 44, 999999)
                elif kind == 'missing-context': struct.pack_into('<I', data, 112, 0)
                elif kind == 'duplicate-stream': struct.pack_into('<I', data, 56, 3)
                else: data = data[:8]
                p.write_bytes(data)
                with self.subTest(kind=kind), self.assertRaises((ValueError, struct.error)):
                    native.dump_inventory(p)

    def test_stackwalk_abi_matches_windows_x64_record_contract(self):
        layout = native.stackwalk_layout()
        self.assertEqual((16, 112, 264), (layout['ADDRESS64'], layout['KDHELP64'], layout['STACKFRAME64']))
        self.assertEqual((80, 88, 120, 128, 152), tuple(layout[k] for k in ['FuncTableEntry', 'Params', 'Far', 'Reserved', 'KdHelp']))
        mapped = native._frame(0x1008, 0x7000, [{'path': r'C:\fixture.dll', 'baseAddress': 0x1000, 'sizeOfImage': 4096}])
        self.assertEqual(8, mapped['moduleOffset']); self.assertFalse(mapped['functionNameClaimed'])
        self.assertIsNone(native._frame(0x8000, 0, [])['module'])

    def test_private_pe_copy_is_bound_to_module_header_and_producer_sha(self):
        with tempfile.TemporaryDirectory() as t, patch.object(native, 'private_file'):
            root = Path(t); source = root / 'gdal4k.dll'
            data = bytearray(152); data[:2] = b'MZ'; struct.pack_into('<I', data, 60, 64)
            data[64:68] = b'PE\0\0'; struct.pack_into('<I', data, 72, 42); struct.pack_into('<I', data, 144, 4096)
            source.write_bytes(data); digest = hashlib.sha256(data).hexdigest()
            inventory = {'modules': [{'path': str(source), 'timeDateStamp': 42, 'sizeOfImage': 4096}]}
            good = root / 'good'; good.mkdir()
            result = native._copy_native(inventory, good, digest, True)
            self.assertEqual(digest, result['sha256']); self.assertTrue(result['producerSha256Matched'])
            bad = root / 'bad'; bad.mkdir()
            with self.assertRaisesRegex(ValueError, 'producer SHA'):
                native._copy_native(inventory, bad, '0' * 64, True)
            changed = copy.deepcopy(inventory); changed['modules'][0]['timeDateStamp'] = 43
            with self.assertRaisesRegex(ValueError, 'header differs'):
                native._copy_native(changed, bad, digest, True)
            with self.assertRaises(FileExistsError): native._copy_native(inventory, good, digest, True)

    def test_short_name_alias_is_canonicalized_but_symlink_ancestor_is_refused(self):
        with tempfile.TemporaryDirectory() as t, patch.object(native, 'private_file'):
            # Keep the modeled alias distinct from the real LONGDI~1 alias
            # that Windows may already create for this long directory.
            root = Path(t).resolve(); long = root / 'long directory'; short = root / 'SYNTH~1'
            long.mkdir(); short.mkdir()
            source = long / 'gdal4k.dll'; alias = short / 'gdal4k.dll'
            data = bytearray(152); data[:2] = b'MZ'; struct.pack_into('<I', data, 60, 64)
            data[64:68] = b'PE\0\0'; struct.pack_into('<I', data, 72, 42); struct.pack_into('<I', data, 144, 4096)
            source.write_bytes(data); os.link(source, alias)
            digest = hashlib.sha256(data).hexdigest(); inventory = {'modules': [{'path': str(alias), 'timeDateStamp': 42, 'sizeOfImage': 4096}]}
            output = root / 'output'; output.mkdir()
            resolve = Path.resolve
            # Model Win32 expanding an actual same-inode DOS alias, with real
            # file reads/header/SHA checks; this is not a Windows API claim.
            with patch.object(Path, 'resolve', lambda p, **kw: source if p == alias else resolve(p, **kw)):
                value = native._copy_native(inventory, output, digest, True)
            self.assertEqual(str(alias), value['observedModulePath']); self.assertEqual(str(source), value['canonicalSourcePath'])
            link = root / 'link'
            try: link.symlink_to(long, target_is_directory=True)
            except OSError: return  # Actual Windows symlink privilege may be absent.
            inventory['modules'][0]['path'] = str(link / 'gdal4k.dll')
            with self.assertRaisesRegex(ValueError, 'reparse point'):
                native._copy_native(inventory, output, digest, True)

    def test_partial_lines_frozen_worker_capture_and_180s_failure(self):
        with tempfile.TemporaryDirectory() as t, patch.object(native, 'private_directory'), patch.object(native, '_write_new'), \
                patch.object(native, '_target_identity') as target, patch.object(native, '_start_capture') as start:
            target.side_effect = lambda owned, pid: dict(pid=pid, creationTime100ns=123, imagePath=r'C:\jdk\bin\java.exe')
            session = MagicMock(); session.poll.return_value = None; session.close.return_value = {'status': 'collector-timed-out'}
            start.return_value = session
            watcher = native.ColumnReadWatchdog(object(), Path(t), '001', expected_dll_sha256='a' * 64)
            clock = Clock(); watcher.tracker.clock = clock
            payload = ('로그\nGDAL4K_JVM_HANG_AGENT PREMAIN_COMPLETE workerPid=123 utc=x\r\n' + phase()).encode()
            for byte in payload: watcher.observe(bytes([byte]))
            clock.now = 59; self.assertIsNone(watcher.poll()); start.assert_not_called()
            watcher.observe(b'GDAL4K_JVM_HANG_AGENT PREMAIN_COMPLETE workerPid=456 utc=x\n')
            clock.now = 60; self.assertIsNone(watcher.poll()); self.assertEqual(123, start.call_args.args[1]['pid'])
            clock.now = 179; self.assertIsNone(watcher.poll()); self.assertEqual(1, start.call_count)
            clock.now = 180; self.assertEqual('native-phase-stalled', watcher.poll()['status'])
            self.assertEqual(1, len(watcher.close())); self.assertEqual(1, len(watcher.close()))

    def test_completed_read_no_capture_and_missing_worker_never_fake_passes_stall(self):
        with tempfile.TemporaryDirectory() as t, patch.object(native, 'private_directory'), patch.object(native, '_write_new'), patch.object(native, '_start_capture') as start:
            watcher = native.ColumnReadWatchdog(object(), Path(t), '002')
            clock = Clock(); watcher.tracker.clock = clock
            watcher.observe((phase() + phase(suffix='after-rows-1')).encode())
            clock.now = 200; self.assertIsNone(watcher.poll()); start.assert_not_called()
            watcher.observe(phase(index=2).encode()); clock.now = 260
            self.assertIsNone(watcher.poll()); self.assertEqual('capture-refused', watcher.receipts[0]['capture']['status'])
            clock.now = 380; self.assertEqual('native-phase-stalled', watcher.poll()['status']); start.assert_not_called()
            watcher.close()

    def test_collector_timeout_only_kills_held_collector_and_never_waits_20s(self):
        clock = Clock(); process = MagicMock(); process.poll.return_value = None
        session = native.CaptureSession(process, Path('.'), clock, 20)
        clock.now = 19; self.assertIsNone(session.poll()); process.kill.assert_not_called()
        clock.now = 20; self.assertEqual('collector-timed-out', session.poll()['status']); process.kill.assert_called_once()
        process.wait.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Requires actual Win32 process/job/DbgHelp operations')
class ActualWindowsCaptureTest(unittest.TestCase):
    def target(self, root):
        heartbeat = root / ('heartbeat-' + str(time.time_ns()) + '.txt')
        code = ('import pathlib,threading,time\n'
                'threading.Thread(target=lambda:time.sleep(90),daemon=True).start()\n'
                f'path=pathlib.Path({str(heartbeat)!r})\n'
                'count=0\n'
                'while True:\n count+=1;path.write_text(str(count));time.sleep(0.05)\n')
        owned = windows.OwnedProcess([sys.executable, '-c', code], cwd=root,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owned.native_test_heartbeat = heartbeat
        return owned

    def cleanup(self, owned):
        owned.terminate_tree(); owned.process.wait(timeout=5); owned.close()

    def ready(self, owned):
        until, previous = time.monotonic() + 5, None
        while time.monotonic() < until:
            self.assertIsNone(owned.process.poll(), 'Owned capture fixture exited during startup')
            try: value = owned.native_test_heartbeat.read_bytes()
            except FileNotFoundError: value = b''
            if value:
                if previous is not None and value != previous: return
                previous = value
            time.sleep(0.02)
        self.fail('Owned capture fixture did not produce two distinct heartbeat values within5s')

    def test_actual_owned_python_dump_contains_native_threads_modules_and_contexts(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); owned = self.target(root)
            try:
                # Observe actual child/thread readiness rather than assume a
                # loaded Windows runner completed Python startup within200ms.
                self.ready(owned)
                session = native._start_capture(owned, owned.identity, root / 'capture', require_gdal=False)
                until = time.monotonic() + 25
                result = None
                while result is None and time.monotonic() < until:
                    result = session.poll(); time.sleep(0.05)
                self.assertIsNotNone(result); self.assertEqual('captured', result['status'], result)
                self.assertGreaterEqual(len(result['dump']['nativeThreads']), 2)
                self.assertGreater(len(result['dump']['modules']), 0)
                self.assertNotEqual(owned.process.pid, result['collectorPid'])
                self.assertFalse(result['dump']['fullMemoryRequested']); self.assertIsNone(result['nativeDll'])
                stacks = result['nativeStacks']
                self.assertEqual(0, stacks['targetSuspendThreadCalls'])
                self.assertFalse(stacks['symbolLookupRequested']); self.assertFalse(stacks['symbolServerRequested'])
                self.assertTrue(any(row.get('walkedFramesBeyondContext', 0) > 0 and any(f['module'] for f in row['frames'][1:])
                                    for row in stacks['threads']), stacks)
                self.assertTrue(all(len(row['frames']) <= 128 for row in stacks['threads']))
                self.assertIsNone(owned.process.poll()); session.close()
                self.assertEqual(native._sha(root / 'capture/native.dmp'), result['dump']['sha256'])
                # A live PID alone would not detect a stranded suspended thread.
                before = owned.native_test_heartbeat.read_bytes(); until = time.monotonic() + 2
                progressed = False
                while time.monotonic() < until:
                    value = owned.native_test_heartbeat.read_bytes()
                    if value and value != before: progressed = True; break
                    time.sleep(0.02)
                self.assertTrue(progressed, 'Target heartbeat stopped after native collection')
            finally: self.cleanup(owned)

    def test_actual_stale_foreign_and_inherited_pid_handle_mistakes_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t).resolve(); owned, other = self.target(root), self.target(root)
            try:
                stale = dict(owned.identity, creationTime100ns=owned.identity['creationTime100ns'] + 1)
                with self.assertRaisesRegex(ValueError, 'identity is stale'):
                    native._start_capture(owned, stale, root / 'stale', require_gdal=False)
                with self.assertRaisesRegex(ValueError, 'outside the owned'):
                    native._start_capture(owned, other.identity, root / 'foreign', require_gdal=False)
                with self.assertRaisesRegex(ValueError, 'handle/PID mismatch'):
                    native._verify_inherited(owned.api, owned.process._handle, owned.job, dict(owned.identity, pid=other.identity['pid']))
                existing = root / 'existing'; existing.mkdir(); (existing / 'keep').write_text('unchanged')
                with self.assertRaises(FileExistsError): native._start_capture(owned, owned.identity, existing, require_gdal=False)
                self.assertEqual('unchanged', (existing / 'keep').read_text())
                self.assertIsNone(owned.process.poll()); self.assertIsNone(other.process.poll())
            finally: self.cleanup(owned); self.cleanup(other)


if __name__ == '__main__': unittest.main()
