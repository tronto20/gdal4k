"""Portable writer-quota controls; actual Windows callback ABI is separate."""
import ctypes as C
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import windows_dump_quota as quota
import windows_native_stall as native
import windows_process as windows


SDK_IO_EXCERPT = '''#include <pshpack4.h>
typedef struct _MINIDUMP_IO_CALLBACK {
    HANDLE Handle;
    ULONG64 Offset;
    PVOID Buffer;
    ULONG BufferBytes;
} MINIDUMP_IO_CALLBACK, *PMINIDUMP_IO_CALLBACK;
'''


class TrackingOutput(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.seeks, self.writes = [], []
    def seek(self, offset, whence=0):
        self.seeks.append((offset, whence))
        return super().seek(offset, whence)
    def write(self, data):
        self.writes.append(len(data))
        return super().write(data)


def callback_event(callbacks, kind, offset=0, pointer=None, amount=0, *, initial_status=0):
    value = quota.MINIDUMP_CALLBACK_INPUT_IO()
    value.ProcessId, value.ProcessHandle, value.CallbackType = 123, 456, kind
    value.Io.Offset, value.Io.Buffer, value.Io.BufferBytes = offset, pointer, amount
    output = quota.MINIDUMP_CALLBACK_OUTPUT()
    output.Status = initial_status
    result = callbacks.routine(None, C.byref(value), C.byref(output))
    return result, output.Status


class RandomAccessQuotaTests(unittest.TestCase):
    def test_sparse_and_overwritten_extents_stay_within_quota(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16)
        sink.write_at(15, b'z'); sink.write_at(0, b'ab'); sink.write_at(0, b'c')
        self.assertEqual(16, len(output.getvalue()))
        self.assertEqual(b'cb', output.getvalue()[:2])
        self.assertEqual(16, sink.extent)
        self.assertEqual(4, sink.bytes_written)

    def test_overrun_is_rejected_before_any_seek_pointer_read_or_write(self):
        for offset, amount in [(16, 1), (15, 2), (100000000000, 1)]:
            output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16); reader = MagicMock()
            with self.subTest(offset=offset), self.assertRaises(quota.QuotaExceeded): sink.write_pointer(offset, 1, amount, read=reader)
            self.assertFalse(output.seeks); self.assertFalse(output.writes); reader.assert_not_called()

    def test_invalid_offsets_lengths_and_limits_have_no_io(self):
        for offset, amount in [(-1, 1), (0, -1), (True, 1), (0, False), (0.5, 1)]:
            output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16)
            with self.subTest(offset=offset, amount=amount), self.assertRaises(ValueError): sink.write_pointer(offset, 1, amount)
            self.assertFalse(output.seeks); self.assertFalse(output.writes)
        for limit in [0, True, quota.MAX_DUMP + 1]:
            with self.subTest(limit=limit), self.assertRaises(ValueError): quota.BoundedRandomAccessSink(TrackingOutput(), limit)

    def test_typed_memoryview_uses_byte_count_not_element_count(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 8)
        with self.assertRaises(quota.QuotaExceeded): sink.write_at(0, memoryview((C.c_uint32 * 3)(1, 2, 3)))
        self.assertFalse(output.seeks); self.assertFalse(output.writes)

    def test_pointer_reads_and_file_writes_are_each_at_most_one_mib(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 3 * quota.CHUNK); calls = []
        def reader(address, amount):
            calls.append((address, amount)); return b'x' * amount
        sink.write_pointer(0, 1000, 2 * quota.CHUNK + 7, read=reader)
        self.assertEqual([quota.CHUNK, quota.CHUNK, 7], [size for _, size in calls])
        self.assertTrue(all(size <= quota.CHUNK for size in output.writes))
        self.assertEqual(2 * quota.CHUNK + 7, sink.extent)

    def test_short_pointer_read_or_write_is_an_explicit_failure(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16)
        with self.assertRaises(OSError): sink.write_pointer(0, 1, 5, read=lambda address, size: b'abc')
        self.assertFalse(output.seeks)
        class Partial(TrackingOutput):
            def write(self, data): return super().write(data[:2])
        output = Partial(); sink = quota.BoundedRandomAccessSink(output, 16)
        with self.assertRaises(OSError): sink.write_at(0, b'abcdef')
        self.assertEqual(2, sink.extent)
        self.assertEqual(2, len(output.getvalue()))

    def test_empty_at_eof_is_allowed_but_null_nonempty_buffer_is_not(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16)
        sink.write_pointer(16, None, 0)
        self.assertFalse(output.seeks)
        with self.assertRaises(ValueError): sink.write_pointer(0, None, 1)
        self.assertFalse(output.seeks)


class CallbackQuotaTests(unittest.TestCase):
    def setUp(self):
        # Portable contract checks exercise only this fake Win32 device API.
        # ActualWindowsCallbackQuotaTests below uses the real APIs separately.
        self.kernel = SimpleNamespace(CreateFileW=MagicMock(return_value=0x1234),
                                      GetFileType=MagicMock(return_value=2), CloseHandle=MagicMock(return_value=1))
        self.dll_patch = patch.object(quota.C, 'WinDLL', return_value=self.kernel, create=True)
        self.dll_patch.start()
        self.addCleanup(self.dll_patch.stop)

    def writer(self, mode='positive'):
        class Write:
            def __call__(self, process, pid, handle, flags, exception, user, information):
                self.handle = handle
                if mode == 'ignored':
                    return 1
                callback = C.cast(information, C.POINTER(quota.MINIDUMP_CALLBACK_INFORMATION)).contents.CallbackRoutine
                shim = SimpleNamespace(routine=callback)
                if not callback_event(shim, quota.IO_START)[0]:
                    return 0
                if mode != 'no-write':
                    buffer = C.create_string_buffer(b'MDMP' + b'x' * 28)
                    amount = 0 if mode == 'zero-write' else 32
                    if not callback_event(shim, quota.IO_WRITE_ALL, 0, C.addressof(buffer), amount)[0]:
                        return 0
                return callback_event(shim, quota.IO_FINISH)[0]
        return Write()

    def test_api_last_error_is_cleared_then_snapshotted_before_receipt_code(self):
        for returned, observed in [(0, 123), (1, 123), (0, -2147024809), (1, -2147024809)]:
            with self.subTest(apiReturn=returned, apiLastError=observed):
                events, state = [], {'lastError': 777}
                def clear(value):
                    events.append('clear'); state['lastError'] = value
                def snapshot():
                    events.append('snapshot'); return state['lastError']
                class Write:
                    def __call__(self, *args):
                        events.append('api')
                        self.assertion = state['lastError']
                        state['lastError'] = observed
                        return returned
                function = Write(); original = quota.DumpIoCallbacks.receipt
                def receipt(callbacks):
                    events.append('receipt'); state['lastError'] = 999
                    return original(callbacks)
                with patch.object(quota.C, 'set_last_error', clear, create=True), \
                     patch.object(quota.C, 'get_last_error', snapshot, create=True), \
                     patch.object(quota.DumpIoCallbacks, 'receipt', receipt), self.assertRaises(RuntimeError) as caught:
                    quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, TrackingOutput())
                self.assertEqual(['clear', 'api', 'snapshot', 'receipt'], events)
                self.assertEqual(0, function.assertion)
                value = caught.exception.quota
                self.assertEqual(observed, value['apiLastErrorObserved'])
                self.assertEqual(observed & 0xffffffff, value['apiLastErrorUnsigned32'])
                self.assertEqual(f'0x{observed & 0xffffffff:08x}', value['apiLastErrorHex'])
                if observed < 0:
                    self.assertEqual((2147942487, '0x80070057'),
                                     (value['apiLastErrorUnsigned32'], value['apiLastErrorHex']))
                self.assertTrue(value['apiLastErrorClearedBeforeCall'])
                self.assertIn('HRESULT', value['apiLastErrorInterpretation'])
                self.assertIn('no failure cause inferred', value['apiLastErrorInterpretation'])
                self.assertEqual(bool(returned), value['miniDumpBooleanReturn'])
                self.assertEqual(0x1000, value['requestedDumpFlags'])
                self.assertIn('"apiLastErrorObserved":' + str(observed), str(caught.exception))
                self.assertFalse(value['ordinaryFileHandleFallback'])

    def test_packed4_projection_matches_independently_parsed_sdk_io_fields(self):
        types = {'HANDLE': (8, 8), 'ULONG64': (8, 8), 'PVOID': (8, 8), 'ULONG': (4, 4)}
        fields = re.findall(r'^\s*(HANDLE|ULONG64|PVOID|ULONG)\s+(\w+);$', SDK_IO_EXCERPT, re.MULTILINE)
        offset, offsets = 0, {}
        for typename, name in fields:
            size, alignment = types[typename]; alignment = min(4, alignment)
            offset = (offset + alignment - 1) // alignment * alignment
            offsets[name] = offset; offset += size
        self.assertEqual({'Handle': 0, 'Offset': 8, 'Buffer': 16, 'BufferBytes': 24}, offsets)
        self.assertEqual(28, offset)
        layout = quota.callback_layout()
        self.assertEqual((0, 4, 12, 16), tuple(layout[x] for x in ['inputPid', 'inputHandle', 'inputType', 'inputUnion']))
        self.assertEqual(4, quota.MINIDUMP_CALLBACK_INPUT_IO._pack_)
        self.assertEqual(0, quota.MINIDUMP_CALLBACK_OUTPUT.Status.offset)
        root = Path(__file__).resolve().parent.parent
        reference = root / 'verification/results/json-string-subtype/pr44-windows-native-timeout-diagnosis/public-sdk-reference-minidumpapiset.h'
        if reference.exists():
            raw = reference.read_bytes()
            self.assertEqual('bd614402842ff7f890d96a5312416600c6c0d2ee2ddd3f7b8dac0961e5814d1f', hashlib.sha256(raw).hexdigest())
            self.assertIn(SDK_IO_EXCERPT.split('\n', 1)[1].strip(), raw.decode())
            self.assertLess(raw.index(b'#include <pshpack4.h>'), raw.index(b'typedef struct _MINIDUMP_CALLBACK_INPUT'))
        # This source/layout check deliberately makes no actual Windows ABI claim.

    def test_io_start_write_finish_owns_every_random_access_byte(self):
        output = TrackingOutput(); sink = quota.BoundedRandomAccessSink(output, 16); callbacks = quota.DumpIoCallbacks(sink)
        buffer = C.create_string_buffer(b'hello')
        self.assertEqual((1, quota.S_FALSE), callback_event(callbacks, quota.IO_START))
        self.assertEqual((1, quota.S_OK), callback_event(callbacks, quota.IO_WRITE_ALL, 4, C.addressof(buffer), 5))
        self.assertEqual((1, quota.S_OK), callback_event(callbacks, quota.IO_FINISH))
        self.assertEqual(b'\0' * 4 + b'hello', output.getvalue())
        self.assertTrue(callbacks.receipt()['ioFinishObserved'])
        self.assertFalse(callbacks.receipt()['ordinaryFileHandleFallback'])

    def test_live_process_candidate_and_vm_decline_overwrite_poisoned_output(self):
        for kind in [16, 17]:
            for poison in [quota.S_FALSE, quota.E_FAIL, 0x12345678]:
                with self.subTest(kind=kind, poison=poison):
                    output = TrackingOutput()
                    callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(output, 64))
                    self.assertEqual((1, quota.S_OK), callback_event(callbacks, kind, initial_status=poison))
                    self.assertIsNone(callbacks.error)
                    self.assertFalse(callbacks.started)
                    self.assertEqual(0, callbacks.io_calls)
                    self.assertFalse(output.writes)
        for kind in [0, 1, 0xfffffffe]:  # Module/thread flag defaults and unrelated callbacks.
            callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(TrackingOutput(), 64))
            self.assertEqual((1, 0x12345678), callback_event(callbacks, kind, initial_status=0x12345678))

    def test_reused_io_negotiation_output_still_requires_bounded_writes_and_finish(self):
        output = TrackingOutput(); callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(output, 32))
        value = quota.MINIDUMP_CALLBACK_INPUT_IO()
        value.ProcessId, value.ProcessHandle = 123, 456
        result = quota.MINIDUMP_CALLBACK_OUTPUT(); result.Status = quota.E_FAIL
        def emit(kind):
            value.CallbackType = kind
            return callbacks.routine(None, C.byref(value), C.byref(result)), result.Status
        self.assertEqual((1, quota.S_FALSE), emit(quota.IO_START))
        self.assertEqual((1, quota.S_OK), emit(16))
        # Poison the reused output as an SDK caller may; no zero-init assumption.
        result.Status = quota.S_FALSE
        self.assertEqual((1, quota.S_OK), emit(17))
        self.assertFalse(callbacks.finished)
        self.assertEqual(0, callbacks.io_calls)
        buffer = C.create_string_buffer(b'MDMP' + b'x' * 28)
        value.Io.Offset, value.Io.Buffer, value.Io.BufferBytes = 0, C.addressof(buffer), 32
        self.assertEqual((1, quota.S_OK), emit(quota.IO_WRITE_ALL))
        self.assertEqual((1, quota.S_OK), emit(quota.IO_FINISH))
        self.assertEqual(b'MDMP' + b'x' * 28, output.getvalue())
        self.assertEqual(32, callbacks.sink.extent)
        self.assertEqual({'11': 1, '12': 1, '13': 1, '16': 1, '17': 1}, callbacks.receipt()['callbackTypeCounts'])
        self.assertTrue(callbacks.finished)
        self.assertIsNone(callbacks.error)

    def test_quota_callback_failure_does_not_dereference_invalid_pointer(self):
        output = TrackingOutput(); callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(output, 4))
        callback_event(callbacks, quota.IO_START)
        self.assertEqual((0, quota.E_DISK_FULL), callback_event(callbacks, quota.IO_WRITE_ALL, 0, 1, 5))
        self.assertEqual('QuotaExceeded', callbacks.error['type'])
        self.assertFalse(output.seeks); self.assertFalse(output.writes)

    def test_callback_exception_is_caught_and_bounded(self):
        output = TrackingOutput(); output.seek = MagicMock(side_effect=OSError('x' * 10000))
        callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(output, 16)); buffer = C.create_string_buffer(b'x')
        callback_event(callbacks, quota.IO_START)
        self.assertEqual((0, quota.E_FAIL), callback_event(callbacks, quota.IO_WRITE_ALL, 0, C.addressof(buffer), 1))
        self.assertLessEqual(len(callbacks.error['message']), 1024)
        self.assertEqual('OSError', callbacks.error['type'])

    def test_missing_or_out_of_order_callbacks_refuse_without_c_throw(self):
        for kinds in [[quota.IO_WRITE_ALL], [quota.IO_FINISH], [quota.IO_START, quota.IO_START]]:
            callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(TrackingOutput(), 16))
            results = [callback_event(callbacks, kind)[0] for kind in kinds]
            self.assertEqual(0, results[-1])
        callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(TrackingOutput(), 16))
        self.assertEqual(0, callbacks.routine(None, None, None))
        self.assertIsNotNone(callbacks.error)

    def test_writer_passes_only_fixed_noninherit_nul_char_device_and_no_ordinary_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'quota.dmp'; function = self.writer()
            with path.open('xb') as output:
                receipt = quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, output, limit=64)
            self.assertEqual(0x1234, function.handle)
            self.assertEqual(32, path.stat().st_size)
            self.assertTrue(receipt['ioFinishObserved'])
            self.assertEqual({'11': 1, '12': 1, '13': 1}, receipt['callbackTypeCounts'])
            self.assertTrue(receipt['nulDevice']['closed'])
            self.assertFalse(receipt['nulDevice']['inheritableByCreateFileContract'])
            self.assertFalse(receipt['ordinaryFileHandleFallback'])
            self.kernel.CreateFileW.assert_called_once_with(r'\\.\NUL', 0x40000000, 3, None, 3, 0, None)
            self.kernel.GetFileType.assert_called_once_with(0x1234)
            self.kernel.CloseHandle.assert_called_once_with(0x1234)
            path = Path(temporary) / 'failure.dmp'; function = self.writer()
            with path.open('xb') as output, self.assertRaises(RuntimeError) as caught:
                quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, output, limit=16)
            self.assertEqual(0, path.stat().st_size)
            self.assertEqual('QuotaExceeded', caught.exception.quota['callbackError']['type'])
            path = Path(temporary) / 'ignored.dmp'; function = self.writer('ignored')
            with path.open('xb') as output, self.assertRaises(RuntimeError):
                quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, output)
            self.assertEqual(0, path.stat().st_size)
            self.assertEqual(3, self.kernel.CloseHandle.call_count)

    def test_invalid_or_noncharacter_nul_handles_never_reach_minidump(self):
        for handle in [None, 0, C.c_void_p(-1).value]:
            with self.subTest(handle=handle):
                self.kernel.CreateFileW.return_value = handle
                self.kernel.CloseHandle.reset_mock()
                function = MagicMock()
                with self.assertRaisesRegex(RuntimeError, 'rejected the fixed NUL'):
                    quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, TrackingOutput())
                function.assert_not_called()
                self.kernel.CloseHandle.assert_not_called()
        self.kernel.CreateFileW.return_value = 0x1234
        for kind in [0, 1, 3]:
            with self.subTest(fileType=kind):
                self.kernel.GetFileType.return_value = kind
                self.kernel.CloseHandle.reset_mock()
                function = MagicMock()
                with self.assertRaisesRegex(RuntimeError, 'not FILE_TYPE_CHAR') as caught:
                    quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=function), 1, 123, TrackingOutput())
                function.assert_not_called()
                self.kernel.CloseHandle.assert_called_once_with(0x1234)
                self.assertEqual(kind, caught.exception.quota['nulDevice']['fileTypeObserved'])

    def test_nul_api_acquisition_exception_keeps_original_bounded_quota(self):
        primary = OSError('actual fixed-device API acquisition fixture failure')
        with patch.object(quota, '_fixed_nul_api', side_effect=primary), self.assertRaises(OSError) as caught:
            quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=MagicMock()), 1, 123, TrackingOutput())
        self.assertIs(primary, caught.exception)
        self.assertEqual('load-fixed-nul-api', primary.quota['writerStage'])
        self.assertFalse(primary.quota['miniDumpApiCalled'])
        self.assertEqual('OSError', primary.quota['writerException']['type'])
        self.assertFalse(primary.quota['nulDevice']['closeAttempted'])
        self.kernel.CloseHandle.assert_not_called()
        self.assertLessEqual(len(quota.encode_json(primary.quota, quota.SELF_CHECK_METADATA)), quota.SELF_CHECK_METADATA)

    def test_start_finish_without_positive_dump_extent_cannot_pass(self):
        for mode in ['ignored', 'no-write', 'zero-write']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'not-a-dump.dmp'
                with path.open('xb') as output, self.assertRaises(RuntimeError) as caught:
                    quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=self.writer(mode)), 1, 123, output)
                self.assertEqual(0, path.stat().st_size)
                self.assertEqual(0, caught.exception.quota['extentBytes'])
                self.assertFalse(caught.exception.quota['ordinaryFileHandleFallback'])

    def test_callback_type_diagnostics_have_fixed_cardinality_and_saturating_counts(self):
        callbacks = quota.DumpIoCallbacks(quota.BoundedRandomAccessSink(TrackingOutput(), 64))
        for kind in range(1000, 1000 + quota.CALLBACK_TYPE_LIMIT + 7):
            self.assertEqual(1, callback_event(callbacks, kind)[0])
        callbacks.callback_types[1000] = quota.CALLBACK_COUNT_LIMIT
        self.assertEqual(1, callback_event(callbacks, 1000)[0])
        receipt = callbacks.receipt()
        self.assertEqual(quota.CALLBACK_TYPE_LIMIT, len(receipt['callbackTypeCounts']))
        self.assertEqual(7, receipt['callbackTypeOverflowEvents'])
        self.assertEqual(1, receipt['callbackCountOverflowEvents'])
        self.assertEqual(quota.CALLBACK_COUNT_LIMIT, receipt['callbackTypeCounts']['1000'])

    def test_api_exception_snapshot_and_original_quota_survive_device_close_failure(self):
        primary = OSError('actual ctypes API fixture exception')
        primary.winerror = -1073741816
        events, state = [], {'error': 0}
        def clear(value):
            events.append('clear'); state['error'] = value
        def snapshot():
            events.append('snapshot'); return state['error']
        class Write:
            def __call__(self, *args):
                events.append('api'); state['error'] = -2147024809
                raise primary
        original = quota.DumpIoCallbacks.receipt
        def receipt(callbacks):
            events.append('receipt'); state['error'] = 999
            self.kernel.CloseHandle.assert_not_called()
            return original(callbacks)
        self.kernel.CloseHandle.return_value = 0
        with patch.object(quota.C, 'set_last_error', clear, create=True), \
             patch.object(quota.C, 'get_last_error', snapshot, create=True), \
             patch.object(quota.DumpIoCallbacks, 'receipt', receipt), self.assertRaises(OSError) as caught:
            quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=Write()), 1, 123, TrackingOutput())
        self.assertIs(primary, caught.exception)
        self.assertEqual(['clear', 'api', 'snapshot', 'receipt', 'snapshot'], events)
        self.assertEqual(-2147024809, primary.quota['apiLastErrorObserved'])
        self.assertEqual('0x80070057', primary.quota['apiLastErrorHex'])
        self.assertEqual(-1073741816, primary.quota['apiException']['winerror']['raw'])
        self.assertEqual('0xc0000008', primary.quota['apiException']['winerror']['hex'])
        self.assertIsNone(primary.quota['miniDumpBooleanReturn'])
        self.assertFalse(primary.quota['miniDumpReturnedNormally'])
        self.assertIn('thread-local', primary.quota['apiLastErrorInterpretation'])
        self.assertIn('no normal HRESULT return', primary.quota['apiLastErrorInterpretation'])
        self.assertIn('nulDeviceCleanupFailure', primary.__dict__)
        self.assertTrue(primary.quota['nulDevice']['closeAttempted'])
        self.assertFalse(primary.quota['nulDevice']['closed'])
        self.kernel.CloseHandle.assert_called_once_with(0x1234)

    def test_device_close_failure_refuses_otherwise_successful_minidump(self):
        self.kernel.CloseHandle.return_value = 0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'quota.dmp'
            with path.open('xb') as output, self.assertRaisesRegex(RuntimeError, 'device cleanup failed') as caught:
                quota.write_minidump(SimpleNamespace(MiniDumpWriteDump=self.writer()), 1, 123, output)
            self.assertTrue(caught.exception.quota['miniDumpBooleanReturn'])
            self.assertEqual(32, caught.exception.quota['extentBytes'])
            self.assertIn('closeError', caught.exception.quota['nulDevice'])
            self.kernel.CloseHandle.assert_called_once_with(0x1234)

    def test_full_memory_or_invalid_pid_cannot_reach_dbghelp(self):
        function = MagicMock(); dbghelp = SimpleNamespace(MiniDumpWriteDump=function)
        for pid, flags in [(True, 0x1000), (0, 0x1000), (123, 2)]:
            with self.subTest(pid=pid, flags=flags), self.assertRaises(ValueError):
                quota.write_minidump(dbghelp, 1, pid, TrackingOutput(), flags=flags)
        function.assert_not_called()


class MetadataQuotaTests(unittest.TestCase):
    def mocked_self_check(self, path, payload, returncode):
        owned = MagicMock()
        owned.process.returncode = returncode
        def start(*args, **kwargs):
            if payload is not None:
                (path / 'self-check.json').write_bytes(payload)
            return owned
        with patch.object(quota, 'os', SimpleNamespace(name='nt')), \
             patch.object(windows, 'OwnedProcess', side_effect=start), \
             patch.object(windows, 'private_directory'):
            try:
                return quota.self_check(path)
            finally:
                owned.close_before.assert_called_once()

    def test_failed_self_check_worker_receipt_keeps_cause_and_quota_without_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'worker'
            receipt = dict(status='failed', sourceSha256=quota._source_sha(), actualWindowsOwnedChildSelfCheck=False,
                           error=dict(type='RuntimeError', message='owned worker actual error'),
                           dumpQuota=dict(miniDumpBooleanReturn=False, apiLastErrorObserved=123,
                                          callbackError=dict(type='QuotaExceeded', message='bounded callback')),
                           cleanupFailures=[dict(stage='close-owned-job', type='OSError', message='owned cleanup failed')])
            with self.assertRaisesRegex(RuntimeError, 'owned worker actual error') as caught:
                self.mocked_self_check(path, json.dumps(receipt).encode(), 1)
            self.assertEqual(receipt, caught.exception.selfCheckReceipt)
            self.assertEqual(receipt['dumpQuota'], caught.exception.quota)
            self.assertEqual(receipt['cleanupFailures'], caught.exception.selfCheckWorkerCleanupFailures)
            self.assertIn('"apiLastErrorObserved":123', str(caught.exception))
            self.assertIn('owned cleanup failed', str(caught.exception))
            self.assertIsNone(quota._LAST_SELF_CHECK)

    def test_rc1_cannot_promote_even_a_passed_source_qualified_worker_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = dict(status='passed', sourceSha256=quota._source_sha(), actualWindowsOwnedChildSelfCheck=True)
            with self.assertRaisesRegex(RuntimeError, '"workerReturncode":1'):
                self.mocked_self_check(Path(temporary) / 'worker', json.dumps(receipt).encode(), 1)
            self.assertIsNone(quota._LAST_SELF_CHECK)

    def test_missing_oversized_or_wrong_source_self_check_receipts_are_refused(self):
        wrong = json.dumps(dict(status='failed', sourceSha256='0' * 64, error=dict(message='unqualified'))).encode()
        for name, raw in [('missing', None), ('oversized', b'x' * (quota.SELF_CHECK_METADATA + 1)), ('wrong-source', wrong)]:
            with self.subTest(kind=name), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    self.mocked_self_check(Path(temporary) / 'worker', raw, 1)
                self.assertIsNone(quota._LAST_SELF_CHECK)

    def test_failed_worker_main_writes_its_quota_receipt_and_keeps_original_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            primary = RuntimeError('actual worker error')
            primary.quota = dict(miniDumpBooleanReturn=False, apiLastErrorObserved=123)
            primary.selfCheckWorkerCleanupFailures = [dict(stage='close-owned-job', type='OSError', message='owned cleanup failed')]
            with patch.object(quota.sys, 'argv', ['quota', '--self-check-worker', str(path)]), \
                 patch.object(quota, '_self_check_worker', side_effect=primary), \
                 patch.object(windows, 'private_file'), self.assertRaises(RuntimeError) as caught:
                quota.main()
            self.assertIs(primary, caught.exception)
            value = json.loads((path / 'self-check.json').read_bytes())
            self.assertEqual('failed', value['status'])
            self.assertEqual(primary.quota, value['dumpQuota'])
            self.assertEqual(primary.selfCheckWorkerCleanupFailures, value['cleanupFailures'])
            self.assertEqual(quota._source_sha(), value['sourceSha256'])
            self.assertLessEqual((path / 'self-check.json').stat().st_size, quota.SELF_CHECK_METADATA)

    def mocked_self_check_worker(self, directory, writer):
        owned = MagicMock()
        owned.identity = dict(pid=123, creationTime100ns=456, imagePath=r'C:\owned\python.exe')
        owned.job = 777
        owned.terminate_tree.side_effect = OSError('terminate fixture failure')
        owned.process.wait.side_effect = subprocess.TimeoutExpired(['owned-child'], 1)
        owned.close_before.side_effect = OSError('close fixture failure')
        api = MagicMock()
        api.k.OpenProcess.return_value = 101
        api.check.side_effect = lambda value: value
        api.process_id.return_value = 123
        api.in_job.return_value = True
        api.exited.return_value = False
        api.identity_from_handle.return_value = owned.identity
        api.close.side_effect = OSError('handle fixture failure')
        with patch.object(quota, 'os', SimpleNamespace(name='nt')), \
             patch.object(windows, 'OwnedProcess', return_value=owned), \
             patch.object(windows, '_api', return_value=api), \
             patch.object(windows, 'private_directory'), patch.object(windows, 'private_file'), \
             patch.object(quota.C, 'WinDLL', create=True), \
             patch.object(quota, 'write_minidump', side_effect=writer), \
             patch.object(quota, 'check_dump_directory', return_value={}):
            try:
                return quota._self_check_worker(directory)
            finally:
                api.close.assert_called_once_with(101)
                owned.terminate_tree.assert_called_once()
                owned.process.wait.assert_called_once_with(timeout=1)
                owned.close_before.assert_called_once()

    def test_worker_cleanup_failures_preserve_primary_writer_error_and_quota(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            primary = RuntimeError('original writer error')
            primary.quota = dict(apiLastErrorObserved=-2147024809, apiLastErrorUnsigned32=2147942487,
                                 apiLastErrorHex='0x80070057')
            def writer(*args, **kwargs):
                raise primary
            with self.assertRaises(RuntimeError) as caught:
                self.mocked_self_check_worker(path, writer)
            self.assertIs(primary, caught.exception)
            self.assertEqual('0x80070057', caught.exception.quota['apiLastErrorHex'])
            self.assertEqual(['close-read-handle', 'terminate-owned-tree', 'wait-owned-child', 'close-owned-job'],
                             [row['stage'] for row in caught.exception.selfCheckWorkerCleanupFailures])
            self.assertEqual(list(path.iterdir()), [])

    def test_worker_cleanup_failure_refuses_an_otherwise_successful_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            def writer(dbghelp, handle, pid, output, *, limit):
                if limit == 16:
                    error = RuntimeError('expected quota refusal')
                    error.quota = dict(callbackError=dict(type='QuotaExceeded'))
                    raise error
                output.write(b'MDMP' + b'x' * 28)
                return {}
            with self.assertRaisesRegex(RuntimeError, 'Owned minidump self-check cleanup failed') as caught:
                self.mocked_self_check_worker(path, writer)
            self.assertEqual(4, len(caught.exception.selfCheckWorkerCleanupFailures))
            self.assertEqual(list(path.iterdir()), [])

    def test_json_cap_is_checked_before_file_creation_or_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'metadata.json'; protect = MagicMock()
            with self.assertRaises(quota.QuotaExceeded): quota.write_json(path, {'text': '\U0001f600' * 20}, 32, protect=protect)
            self.assertFalse(path.exists()); protect.assert_not_called()
            count = quota.write_json(path, {'ok': True}, 32, protect=protect)
            self.assertEqual(count, path.stat().st_size)
            self.assertEqual({'ok': True}, json.loads(path.read_bytes()))

    def test_jsonl_reserve_keeps_final_truncation_record_inside_cap(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'stack.jsonl'
            with path.open('xb') as output:
                writer = quota.BoundedJsonl(output, 128)
                writer.append({'threadId': 1}, reserve=64)
                before = output.tell()
                with self.assertRaises(quota.QuotaExceeded): writer.append({'frames': 'x' * 120}, reserve=64)
                self.assertEqual(before, output.tell())
                writer.append({'metadataTruncated': True, 'omittedThreads': 100})
            self.assertLessEqual(path.stat().st_size, 128)
            self.assertTrue(json.loads(path.read_bytes().splitlines()[-1])['metadataTruncated'])

    def test_inventory_samples_explicitly_omit_full_arrays_and_bound_paths(self):
        inventory = dict(bytes=128, flags=0x1000, threadInfoEntries=100,
                         nativeThreads=[{'threadId': n} for n in range(100)],
                         modules=[dict(path='\U0001f600' * 2048, baseAddress=n, sizeOfImage=10, timeDateStamp=1) for n in range(100)])
        value = quota.inventory_summary(inventory)
        self.assertTrue(value['metadataTruncated'])
        self.assertTrue(value['fullInventoryRetainedOnlyInBoundedDump'])
        self.assertEqual(16, len(value['nativeThreadSample']))
        self.assertEqual(16, len(value['moduleSample']))
        self.assertNotIn('nativeThreads', value); self.assertNotIn('modules', value)
        self.assertLess(len(quota.encode_json(value, quota.COLLECTOR_METADATA)), quota.COLLECTOR_METADATA)

    def test_old_or_copied_receipt_is_not_an_actual_runtime_selfcheck_gate(self):
        with self.assertRaises(RuntimeError): quota.require_self_check({'status': 'passed', 'actualWindowsOwnedChildSelfCheck': True})
        if os.name != 'nt':
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'not-created'
                with self.assertRaises(ValueError): quota.self_check(path)
                self.assertFalse(path.exists())

    def test_native_collector_file_partitions_total_less_than_one_mib(self):
        self.assertLess(quota.STACK_METADATA + quota.COLLECTOR_METADATA + quota.REQUEST_METADATA, 1024**2)
        self.assertEqual((512 * 1024, 256 * 1024, 16 * 1024),
                         (quota.STACK_METADATA, quota.COLLECTOR_METADATA, quota.REQUEST_METADATA))

    def test_native_capture_session_refuses_receipt_before_unbounded_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary); receipt = path / 'collector.json'
            receipt.write_bytes(b'x' * (quota.COLLECTOR_METADATA + 1))
            process = MagicMock(); process.poll.return_value = 0
            session = native.CaptureSession(process, path, lambda: 0, 20, {'boundedNativeMetadata': True})
            result = session.poll()
            self.assertEqual('collector-invalid-receipt', result['status'])
            self.assertIn('metadata quota', result['message'])

    def test_failed_native_quota_receipt_is_retained_but_never_captured_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            value = dict(status='captured', dumpQuota=dict(callbackError=dict(type='QuotaExceeded')))
            quota.write_json(path / 'collector.json', value, quota.COLLECTOR_METADATA)
            process = MagicMock(); process.poll.return_value = 1
            session = native.CaptureSession(process, path, lambda: 0, 20, {'boundedNativeMetadata': True})
            result = session.poll()
            self.assertEqual('collector-failed', result['status'])
            self.assertEqual(1, result['returncode'])
            self.assertEqual('QuotaExceeded', result['dumpQuota']['callbackError']['type'])

    def test_capture_time_bound_is_refused_before_any_directory_or_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'never-created'
            for limit in [0, True, 20.01, float('nan'), float('inf')]:
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    native._start_capture(None, None, path, require_gdal=False, copy_native=False, limit=limit)
                self.assertFalse(path.exists())

    def test_control_dump_directory_checks_required_streams_without_full_abi_claim(self):
        def content(entries):
            start = 32 + len(entries) * 12
            data = bytearray(start + len(entries) * 4)
            struct.pack_into('<IIIIIIQ', data, 0, 0x504d444d, 0xa793, len(entries), 32, 0, 0, 0x1000)
            for index, kind in enumerate(entries):
                struct.pack_into('<III', data, 32 + index * 12, kind, 4, start + index * 4)
            return data
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'control.dmp'; path.write_bytes(content([3, 4, 17]))
            value = quota.check_dump_directory(path)
            self.assertTrue(value['checkedHeaderAndStreamDirectory'])
            self.assertFalse(value['fullContextOrStackValidationClaimed'])
            for data in [content([3, 4]), content([3, 4, 17, 17])]:
                path.write_bytes(data)
                with self.assertRaises(ValueError): quota.check_dump_directory(path)
            data = content([3, 4, 17]); struct.pack_into('<I', data, 32 + 8, len(data) + 100); path.write_bytes(data)
            with self.assertRaises(ValueError): quota.check_dump_directory(path)

    def test_postdump_identity_or_parser_exception_retains_bounded_failure_receipt(self):
        for stage in ['verify-target-after-dump', 'parse-dump-inventory']:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary); request_path = directory / 'request.json'
                request = dict(copyNativeDll=False, target=dict(pid=123, creationTime100ns=456, imagePath=r'C:\owned\native.exe'))
                request_path.write_text(json.dumps(request))
                (directory / 'native.dmp').write_bytes(b'MDMPtiny')
                original = ValueError('injected ' + stage + ' ' + 'x' * 5000)
                def collect(state):
                    state['stage'] = stage
                    raise original
                with patch.object(native, 'private_file'), self.assertRaises(ValueError) as caught:
                    native._run_validated_collector(request_path, request, collect)
                self.assertIs(original, caught.exception)
                path = directory / 'collector.json'; value = json.loads(path.read_bytes())
                self.assertEqual('collector-failed', value['status'])
                self.assertEqual(stage, value['stage'])
                self.assertEqual(request['target'], value['target'])
                self.assertLessEqual(len(value['error']['message']), 1024)
                self.assertLessEqual(path.stat().st_size, quota.COLLECTOR_METADATA)
                self.assertEqual(8, value['dumpCatalog']['bytes'])
                self.assertFalse(value['dumpCatalog']['parsed'])
                self.assertFalse(value['dumpCatalog']['payloadValidClaimed'])

    def test_outer_bounded_failure_never_overwrites_an_earlier_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); request_path = directory / 'request.json'
            request = dict(copyNativeDll=False, target=dict(pid=123))
            original_bytes = b'{"status":"earlier-first-failure","proof":"preserve"}\n'
            path = directory / 'collector.json'; path.write_bytes(original_bytes); before = path.stat()
            original = RuntimeError('later parser error')
            def collect(state):
                state['stage'] = 'parse-dump-inventory'
                raise original
            with patch.object(native, 'private_file'), self.assertRaises(RuntimeError) as caught:
                native._run_validated_collector(request_path, request, collect)
            self.assertIs(original, caught.exception)
            self.assertEqual(original_bytes, path.read_bytes())
            self.assertEqual(before, path.stat())

    def test_source_jvm_failure_receipt_and_acl_error_cannot_mask_primary(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); request_path = directory / 'request.json'
            original = RuntimeError('primary collector error')
            def collect(state): raise original
            with patch.object(native, 'private_file'), self.assertRaises(RuntimeError) as caught:
                native._run_validated_collector(request_path, dict(copyNativeDll=True), collect)
            self.assertIs(original, caught.exception)
            value = json.loads((directory / 'collector.json').read_bytes())
            self.assertEqual('collector-failed', value['status'])
            self.assertEqual('primary collector error', value['error']['message'])
            (directory / 'collector.json').unlink()
            with patch.object(native, 'private_file', side_effect=PermissionError('ACL failed')), self.assertRaises(RuntimeError) as caught:
                native._run_validated_collector(request_path, dict(copyNativeDll=False, target=dict(pid=123)), collect)
            self.assertIs(original, caught.exception)
            self.assertEqual('PermissionError', caught.exception.collectorReceiptFailure['type'])


@unittest.skipUnless(os.name == 'nt', 'Actual Windows callback quota self-check is unavailable on this host')
class ActualWindowsCallbackQuotaTests(unittest.TestCase):
    def test_owned_child_actual_success_and_actual_quota_failure_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'owned-self-check'
            value = quota.self_check(path)
            self.assertIs(value, quota.require_self_check(value))
            self.assertTrue(value['actualWindowsOwnedChildSelfCheck'])
            self.assertTrue(value['success']['ioStartObserved'])
            self.assertTrue(value['success']['ioFinishObserved'])
            self.assertEqual('QuotaExceeded', value['failure']['callbackError']['type'])
            self.assertLessEqual(value['rejectedDumpBytes'], 16)
            self.assertFalse((path / 'success.dmp').exists())
            self.assertFalse((path / 'quota-failure.dmp').exists())
            with self.assertRaises(RuntimeError): quota.require_self_check(dict(value))


if __name__ == '__main__':
    unittest.main()
