"""Quota-enforced DbgHelp I/O callbacks and bounded diagnostic metadata.

The callback declarations project only the SDK's packed4 fields used here.
Portable layout checks are not a Windows ABI pass: an actual owned-child
self-check must succeed before the Native observer enables large captures.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

MAX_DUMP = 128 * 1024**2
CHUNK = 1024**2
STACK_METADATA = 512 * 1024
COLLECTOR_METADATA = 256 * 1024
REQUEST_METADATA = 16 * 1024
SELF_CHECK_METADATA = 16 * 1024
SDK_REFERENCE_SHA256 = 'bd614402842ff7f890d96a5312416600c6c0d2ee2ddd3f7b8dac0961e5814d1f'
IO_START, IO_WRITE_ALL, IO_FINISH = 11, 12, 13
S_OK, S_FALSE = 0, 1
E_FAIL = -2147467259
E_DISK_FULL = -2147024784  # HRESULT_FROM_WIN32(ERROR_DISK_FULL=112).
NUL_DEVICE = r'\\.\NUL'
CALLBACK_TYPE_LIMIT = 32
CALLBACK_COUNT_LIMIT = 0xffffffff


class QuotaExceeded(ValueError):
    pass


class MINIDUMP_IO_CALLBACK(C.Structure):
    _pack_ = 4
    _fields_ = [('Handle', C.c_void_p), ('Offset', C.c_uint64),
                ('Buffer', C.c_void_p), ('BufferBytes', C.c_uint32)]


class MINIDUMP_CALLBACK_INPUT_IO(C.Structure):
    _pack_ = 4
    _fields_ = [('ProcessId', C.c_uint32), ('ProcessHandle', C.c_void_p),
                ('CallbackType', C.c_uint32), ('Io', MINIDUMP_IO_CALLBACK)]


class CANCEL_OUTPUT(C.Structure):
    _pack_ = 4
    _fields_ = [('CheckCancel', C.c_int32), ('Cancel', C.c_int32)]


class MINIDUMP_CALLBACK_OUTPUT(C.Union):
    _pack_ = 4
    _fields_ = [('Status', C.c_int32), ('Cancel', CANCEL_OUTPUT)]


CALLBACK = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(
    C.c_int32, C.c_void_p, C.POINTER(MINIDUMP_CALLBACK_INPUT_IO), C.POINTER(MINIDUMP_CALLBACK_OUTPUT))


class MINIDUMP_CALLBACK_INFORMATION(C.Structure):
    _pack_ = 4
    _fields_ = [('CallbackRoutine', CALLBACK), ('CallbackParam', C.c_void_p)]


def callback_layout():
    actual = dict(pointerBytes=C.sizeof(C.c_void_p), inputPid=MINIDUMP_CALLBACK_INPUT_IO.ProcessId.offset,
                  inputHandle=MINIDUMP_CALLBACK_INPUT_IO.ProcessHandle.offset,
                  inputType=MINIDUMP_CALLBACK_INPUT_IO.CallbackType.offset, inputUnion=MINIDUMP_CALLBACK_INPUT_IO.Io.offset,
                  ioHandle=MINIDUMP_IO_CALLBACK.Handle.offset, ioOffset=MINIDUMP_IO_CALLBACK.Offset.offset,
                  ioBuffer=MINIDUMP_IO_CALLBACK.Buffer.offset, ioBytes=MINIDUMP_IO_CALLBACK.BufferBytes.offset,
                  ioProjectionBytes=C.sizeof(MINIDUMP_IO_CALLBACK), outputStatus=MINIDUMP_CALLBACK_OUTPUT.Status.offset,
                  informationParam=MINIDUMP_CALLBACK_INFORMATION.CallbackParam.offset)
    expected = dict(pointerBytes=8, inputPid=0, inputHandle=4, inputType=12, inputUnion=16,
                    ioHandle=0, ioOffset=8, ioBuffer=16, ioBytes=24, ioProjectionBytes=28,
                    outputStatus=0, informationParam=8)
    if actual != expected:
        raise ValueError('Unsupported packed4 Windows x64 minidump callback projection')
    return actual


class BoundedRandomAccessSink:
    """Enforce maximum file extent before every seek, pointer read and write."""
    def __init__(self, output, limit=MAX_DUMP):
        if type(limit) is not int or not 0 < limit <= MAX_DUMP:
            raise ValueError('Minidump quota must be within1..128MiB')
        self.output, self.limit = output, limit
        self.extent = self.bytes_written = self.write_calls = 0
        self.failure = None

    def check(self, offset, amount):
        if type(offset) is not int or type(amount) is not int or offset < 0 or amount < 0:
            raise ValueError('Minidump offset/length must be nonnegative integers')
        if offset > self.limit or amount > self.limit - offset:
            raise QuotaExceeded('Minidump write would exceed its pre-write file quota')

    def write_at(self, offset, data):
        view = memoryview(data).cast('B')
        self.check(offset, len(view))
        for start in range(0, len(view), CHUNK):
            chunk = view[start:start + CHUNK]
            self.check(offset + start, len(chunk))
            self.output.seek(offset + start)
            done = self.output.write(chunk)
            if type(done) is int and 0 <= done <= len(chunk):
                self.extent = max(self.extent, offset + start + done)
                self.bytes_written += done
            self.write_calls += 1
            if type(done) is not int or done != len(chunk):
                raise OSError('Incomplete bounded minidump write')

    def write_pointer(self, offset, pointer, amount, read=C.string_at):
        self.check(offset, amount)  # Must precede pointer reads and file seeks.
        if amount and not pointer:
            raise ValueError('Minidump callback supplied a null data buffer')
        for start in range(0, amount, CHUNK):
            size = min(CHUNK, amount - start)
            data = read(pointer + start, size)
            if len(data) != size:
                raise OSError('Incomplete minidump callback buffer read')
            self.write_at(offset + start, data)


class DumpIoCallbacks:
    def __init__(self, sink):
        callback_layout()
        self.sink = sink
        self.started = self.finished = False
        self.io_calls = 0
        self.callback_types = {}
        self.callback_type_overflow = self.callback_count_overflow = 0
        self.error = None
        self.routine = CALLBACK(self._callback)
        self.information = MINIDUMP_CALLBACK_INFORMATION(self.routine, None)

    def _callback(self, parameter, input_pointer, output_pointer):
        # No Python exception may escape across the DbgHelp callback boundary.
        try:
            if not input_pointer:
                raise ValueError('Missing minidump callback input/output')
            value = input_pointer.contents
            kind = value.CallbackType
            if kind in self.callback_types:
                if self.callback_types[kind] == CALLBACK_COUNT_LIMIT:
                    self.callback_count_overflow = min(CALLBACK_COUNT_LIMIT, self.callback_count_overflow + 1)
                else:
                    self.callback_types[kind] += 1
            elif len(self.callback_types) < CALLBACK_TYPE_LIMIT:
                self.callback_types[kind] = 1
            else:
                self.callback_type_overflow = min(CALLBACK_COUNT_LIMIT, self.callback_type_overflow + 1)
            if not output_pointer:
                raise ValueError('Missing minidump callback input/output')
            output = output_pointer.contents
            if kind == IO_START:
                if self.started:
                    raise ValueError('Repeated minidump IoStart callback')
                self.started = True
                output.Status = S_FALSE  # This callback owns all I/O.
            elif kind == IO_WRITE_ALL:
                if not self.started or self.finished or self.error:
                    raise ValueError('Minidump I/O callback order or prior failure')
                self.sink.write_pointer(int(value.Io.Offset), value.Io.Buffer, int(value.Io.BufferBytes))
                self.io_calls = min(CALLBACK_COUNT_LIMIT, self.io_calls + 1)
                output.Status = S_OK
            elif kind == IO_FINISH:
                if not self.started or self.finished or self.error:
                    raise ValueError('Minidump finish callback order or prior failure')
                self.finished = True
                output.Status = S_OK
            elif kind == 6:  # CancelCallback, output union's two BOOL fields.
                output.Cancel.CheckCancel = bool(self.error)
                output.Cancel.Cancel = bool(self.error)
            elif kind == 16:  # IsProcessSnapshotCallback: live held process handle.
                # Live-process candidate; actual owned-child capture must validate it.
                output.Status = S_OK
            elif kind == 17:  # VmStartCallback: no custom virtual-memory callbacks.
                output.Status = S_OK
            return 1
        except BaseException as error:
            if self.error is None:
                self.error = dict(type=type(error).__name__, message=str(error)[:1024])
                self.sink.failure = dict(self.error)
            try:
                if output_pointer:
                    output_pointer.contents.Status = E_DISK_FULL if isinstance(error, QuotaExceeded) else E_FAIL
            except BaseException:
                pass
            return 0

    def receipt(self):
        return dict(quotaBytes=self.sink.limit, extentBytes=self.sink.extent,
                    cumulativeWrittenBytes=self.sink.bytes_written, writeCalls=self.sink.write_calls,
                    ioWriteAllCallbacks=self.io_calls, ioStartObserved=self.started, ioFinishObserved=self.finished,
                    callbackError=self.error, ordinaryFileHandleFallback=False, maxCopyChunkBytes=CHUNK,
                    callbackLayoutProjection=callback_layout(), sdkSourceReferenceSha256=SDK_REFERENCE_SHA256,
                    callbackTypeCounts={str(kind): count for kind, count in sorted(self.callback_types.items())},
                    callbackTypeCardinalityLimit=CALLBACK_TYPE_LIMIT, callbackCountLimit=CALLBACK_COUNT_LIMIT,
                    callbackTypeOverflowEvents=self.callback_type_overflow,
                    callbackCountOverflowEvents=self.callback_count_overflow,
                    compiledSdkAbiVerified=False)


def _fixed_nul_api():
    kernel = C.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    create = kernel.CreateFileW
    create.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p,
                       C.c_uint32, C.c_uint32, C.c_void_p]
    create.restype = C.c_void_p
    kind = kernel.GetFileType
    kind.argtypes = [C.c_void_p]; kind.restype = C.c_uint32
    close = kernel.CloseHandle
    close.argtypes = [C.c_void_p]; close.restype = C.c_int32
    return create, kind, close


def _observed_error(value):
    unsigned = None if value is None else value & 0xffffffff
    return dict(raw=value, unsigned32=unsigned, hex=None if unsigned is None else f'0x{unsigned:08x}')


def write_minidump(dbghelp, process_handle, pid, output, *, limit=MAX_DUMP, flags=0x1000):
    if flags != 0x1000 or type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('Only an actual positive PID and bounded normal/thread-info dump are supported')
    sink = BoundedRandomAccessSink(output, limit)
    callbacks = DumpIoCallbacks(sink)
    write = dbghelp.MiniDumpWriteDump
    write.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p,
                      C.POINTER(MINIDUMP_CALLBACK_INFORMATION)]
    write.restype = C.c_int32
    clear_error = getattr(C, 'set_last_error', None)
    get_error = getattr(C, 'get_last_error', None)
    create = get_type = close = None
    handle = None
    primary = receipt = api_error = None
    ok = error_code = None
    called = False
    stage = 'load-fixed-nul-api'
    device = dict(path=NUL_DEVICE, desiredAccess=0x40000000, shareMode=3,
                  securityAttributesNull=True, inheritableByCreateFileContract=False,
                  creationDisposition=3, flagsAndAttributes=0, ordinaryFileFallback=False,
                  nativeDumpWrittenOnlyByBoundedCallbacks=True, closeAttempted=False, closed=False)

    def evidence():
        value = callbacks.receipt()
        observed = _observed_error(error_code)
        value.update(miniDumpApiCalled=called, miniDumpReturnedNormally=called and api_error is None,
                     miniDumpReturnCode=None if not called or api_error is not None else int(bool(ok)),
                     miniDumpBooleanReturn=None if not called or api_error is not None else bool(ok),
                     apiLastErrorObserved=observed['raw'], apiLastErrorUnsigned32=observed['unsigned32'],
                     apiLastErrorHex=observed['hex'], apiLastErrorClearedBeforeCall=called and clear_error is not None,
                     requestedDumpFlags=flags, nulDevice=device, writerStage=stage,
                     apiLastErrorInterpretation=(
                         'MiniDumpWriteDump documents this value as an HRESULT; no failure cause inferred'
                         if called and api_error is None else
                         'Observed ctypes thread-local last-error snapshot after API raised; no normal HRESULT return or cause inferred'
                         if called else 'MiniDumpWriteDump was not called; no API error return claimed'))
        if api_error is not None:
            winerror = getattr(api_error, 'winerror', None)
            winerror = winerror if type(winerror) is int else None
            value['apiException'] = dict(type=type(api_error).__name__[:128], message=str(api_error)[:1024],
                                         winerror=_observed_error(winerror),
                                         winerrorInterpretation='Exception winerror preserved separately as observed NTSTATUS; no conversion or cause inferred')
        return value

    try:
        create, get_type, close = _fixed_nul_api()
        stage = 'open-fixed-nul-device'
        # A fixed non-inherited character-device handle satisfies the API's
        # handle contract. It cannot write ordinary payload bytes to disk even
        # if DbgHelp ignores custom I/O; ignored callbacks still fail below.
        opened = create(NUL_DEVICE, 0x40000000, 3, None, 3, 0, None)
        if opened in (None, 0, C.c_void_p(-1).value, -1):
            code = get_error() if get_error is not None else None
            device['createFileErrorObserved'] = _observed_error(code)
            raise RuntimeError('CreateFileW rejected the fixed NUL device handle')
        handle = opened
        stage = 'verify-fixed-nul-character-device'
        device['fileTypeObserved'] = int(get_type(handle))
        if device['fileTypeObserved'] != 2:  # FILE_TYPE_CHAR
            raise RuntimeError('Fixed NUL device handle is not FILE_TYPE_CHAR')
        stage = 'write-minidump'
        if clear_error is not None:
            clear_error(0)
        called = True
        try:
            ok = write(process_handle, pid, handle, flags, None, None, C.byref(callbacks.information))
        except Exception as error:
            error_code = get_error() if get_error is not None else None
            api_error = error
        else:
            error_code = get_error() if get_error is not None else None
        receipt = evidence()
        if api_error is not None:
            api_error.quota = receipt
            api_error.add_note('Bounded MiniDumpWriteDump observation: ' + json.dumps(receipt, separators=(',', ':'), ensure_ascii=True))
            raise api_error
        if (not ok or callbacks.error or not callbacks.started or not callbacks.finished
                or callbacks.io_calls <= 0 or sink.extent <= 0):
            error = RuntimeError('Quota-backed MiniDumpWriteDump failed: ' +
                                 json.dumps(receipt, separators=(',', ':'), ensure_ascii=True))
            error.quota = receipt
            raise error
        output.flush()
        os.fsync(output.fileno())
        if output.tell() > limit or os.fstat(output.fileno()).st_size > limit:
            raise RuntimeError('Bounded callback sink violated its extent quota')
        return receipt
    except BaseException as error:
        primary = error
        if receipt is None:
            receipt = evidence()
        if api_error is None:
            receipt['writerException'] = dict(type=type(error).__name__[:128], message=str(error)[:1024])
        error.quota = receipt
        raise
    finally:
        if handle is not None:
            device['closeAttempted'] = True
            try:
                if not close(handle):
                    code = get_error() if get_error is not None else None
                    raise RuntimeError('CloseHandle failed for the fixed NUL device: ' + str(code))
                device['closed'] = True
            except Exception as error:
                failure = dict(type=type(error).__name__[:128], message=str(error)[:1024])
                device['closeError'] = failure
                if primary is not None:
                    primary.nulDeviceCleanupFailure = failure
                    primary.add_note('Fixed NUL device cleanup also failed: ' + json.dumps(failure, separators=(',', ':'), ensure_ascii=True))
                else:
                    error = RuntimeError('Fixed NUL device cleanup failed: ' + json.dumps(failure, separators=(',', ':'), ensure_ascii=True))
                    error.quota = receipt
                    raise error


def encode_json(value, limit):
    raw = (json.dumps(value, separators=(',', ':'), ensure_ascii=True) + '\n').encode('ascii')
    if len(raw) > limit:
        raise QuotaExceeded('Diagnostic JSON exceeds its pre-write metadata quota')
    return raw


def write_json(path, value, limit, *, protect=None):
    raw = encode_json(value, limit)
    with Path(path).open('xb') as output:
        if protect is not None:
            protect(path)
        if output.write(raw) != len(raw):
            raise OSError('Incomplete bounded diagnostic JSON write')
        output.flush(); os.fsync(output.fileno())
    return len(raw)


class BoundedJsonl:
    def __init__(self, output, limit=STACK_METADATA):
        if type(limit) is not int or not 0 < limit <= STACK_METADATA:
            raise ValueError('Native stack metadata quota must be within1..512KiB')
        self.output, self.limit, self.bytes, self.rows = output, limit, 0, 0

    def append(self, value, *, reserve=0):
        raw = encode_json(value, self.limit)
        if type(reserve) is not int or reserve < 0 or self.bytes + len(raw) + reserve > self.limit:
            raise QuotaExceeded('Diagnostic JSONL exceeds its pre-write metadata quota')
        if self.output.write(raw) != len(raw):
            raise OSError('Incomplete bounded diagnostic JSONL write')
        self.output.flush(); os.fsync(self.output.fileno())
        self.bytes += len(raw); self.rows += 1


def short_text(value, limit=512):
    text = str(value)
    return dict(text=text[:limit], truncated=len(text) > limit,
                fullUtf8Sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())


def inventory_summary(inventory):
    modules = inventory['modules']
    threads = inventory['nativeThreads']
    return dict(bytes=inventory['bytes'], flags=inventory['flags'], nativeThreadCount=len(threads),
                threadInfoEntries=inventory['threadInfoEntries'], moduleCount=len(modules),
                nativeThreadSample=threads[:16],
                moduleSample=[{**{key: row[key] for key in ['baseAddress', 'sizeOfImage', 'timeDateStamp']},
                               'path': short_text(row['path'])} for row in modules[:16]],
                metadataTruncated=len(threads) > 16 or len(modules) > 16 or any(len(row['path']) > 512 for row in modules[:16]),
                fullInventoryRetainedOnlyInBoundedDump=True, stackUnwindingPerformed=False, fullMemoryRequested=False)


_LAST_SELF_CHECK = None
_SELF_CHECK_READY_PREFIX = b'gdal4k-ready-v1:'
_SELF_CHECK_READY_MAX_BYTES = 60


def _self_check_ready_marker(pid, nonce):
    if (type(pid) is not int or not 0 < pid <= 0xffffffff
            or type(nonce) is not str or len(nonce) != 32
            or any(c not in '0123456789abcdef' for c in nonce)):
        raise ValueError('Invalid owned self-check readiness identity')
    # At most60 bytes, including the largest Win32 PID and the final newline.
    marker = _SELF_CHECK_READY_PREFIX + nonce.encode('ascii') + b':' + str(pid).encode('ascii') + b'\n'
    if len(marker) > _SELF_CHECK_READY_MAX_BYTES:
        raise ValueError('Owned self-check readiness marker exceeds its protocol bound')
    return marker


def _self_check_child_code(nonce):
    _self_check_ready_marker(1, nonce)  # Validate before starting any process.
    prefix = _SELF_CHECK_READY_PREFIX + nonce.encode('ascii') + b':'
    return ('import os,sys,time\n'
            f'marker={prefix!r} + str(os.getpid()).encode("ascii") + b"\\n"\n'
            'fd=sys.stdout.fileno()\n'
            'if os.write(fd,marker)!=len(marker): raise OSError("Incomplete readiness marker write")\n'
            'os.close(fd)\n'
            'time.sleep(30)\n')


def _wait_self_check_ready(owned, nonce):
    """Wait for this owned child's script, bounded by its enclosing20s Job gate.

    Identity/Job checks alone can succeed before the newly resumed process has
    initialized its loader and Python runtime. This pipe is private to the new
    child. Read only the fixed-size marker and one EOF byte; the worker's existing
    outer deadline kills its whole owned Job if a child does not finish writing.
    """
    marker = _self_check_ready_marker(owned.identity['pid'], nonce)
    pipe = owned.process.stdout
    if pipe is None:
        raise RuntimeError('Owned self-check readiness pipe is missing')
    received = bytearray()
    while len(received) < len(marker):
        if owned.process.poll() is not None:
            raise RuntimeError('Owned self-check target exited before readiness')
        chunk = pipe.read(len(marker) - len(received))
        if not isinstance(chunk, bytes) or not chunk:
            raise RuntimeError('Owned self-check readiness ended before its complete marker')
        if len(chunk) > len(marker) - len(received):
            raise RuntimeError('Owned self-check readiness exceeded its marker bound')
        received.extend(chunk)
        if received != marker[:len(received)]:
            raise RuntimeError('Owned self-check readiness marker differs')
    if pipe.read(1) != b'':
        raise RuntimeError('Owned self-check readiness contains trailing data')
    if owned.process.poll() is not None:
        raise RuntimeError('Owned self-check target exited after readiness')
    return dict(protocol='owned-private-pipe-v1', readyBeforeFirstDump=True,
                targetPid=owned.identity['pid'], markerBytes=len(marker),
                markerSha256=hashlib.sha256(marker).hexdigest(),
                markerAndEofObserved=True, enclosingWorkerDeadlineCeilingSeconds=20)


def check_dump_directory(path):
    size = Path(path).stat().st_size
    if not 32 <= size <= MAX_DUMP:
        raise ValueError('Self-check minidump header/size bound')
    with Path(path).open('rb') as source:
        header = source.read(32)
        signature, version, count, directory, checksum, stamp, flags = struct.unpack('<IIIIIIQ', header)
        if signature != 0x504d444d or not 0 < count <= 4096 or directory + 12 * count > size or flags & 2:
            raise ValueError('Self-check minidump stream directory is invalid')
        source.seek(directory)
        kinds = []
        for _ in range(count):
            kind, amount, offset = struct.unpack('<III', source.read(12))
            if offset + amount > size or (kind in (3, 4, 17) and (amount == 0 or kind in kinds)):
                raise ValueError('Self-check minidump stream is empty/outside file')
            kinds.append(kind)
    if not {3, 4, 17}.issubset(kinds):
        raise ValueError('Self-check minidump lacks thread/module/thread-info streams')
    return dict(bytes=size, flags=flags, streamCount=count, streamTypes=kinds,
                checkedHeaderAndStreamDirectory=True, fullContextOrStackValidationClaimed=False)


def _self_check_worker(directory):
    if os.name != 'nt':
        raise RuntimeError('Minidump callback self-check requires actual Windows')
    from windows_process import OwnedProcess, _api, private_directory, private_file, same_identity
    directory = Path(directory); private_directory(directory)
    api = _api()
    nonce = os.urandom(16).hex()
    owned = OwnedProcess([sys.executable, '-c', _self_check_child_code(nonce)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    handle = None
    paths = [directory / 'success.dmp', directory / 'quota-failure.dmp']
    primary = None
    try:
        readiness = _wait_self_check_ready(owned, nonce)
        handle = api.check(api.k.OpenProcess(0x400 | 0x10 | 0x100000, False, owned.identity['pid']))
        if (api.process_id(handle) != owned.identity['pid'] or not api.in_job(handle, owned.job)
                or api.exited(handle) or not same_identity(api.identity_from_handle(handle, owned.identity['pid']), owned.identity)):
            raise ValueError('Self-check target is not the held owned child')
        dbghelp = C.WinDLL('Dbghelp.dll', use_last_error=True, winmode=0x800)
        with paths[0].open('xb') as output:
            private_file(paths[0])
            success = write_minidump(dbghelp, handle, owned.identity['pid'], output, limit=8 * 1024**2)
        success_directory = check_dump_directory(paths[0])
        failed = None
        with paths[1].open('xb') as output:
            private_file(paths[1])
            try:
                write_minidump(dbghelp, handle, owned.identity['pid'], output, limit=16)
            except RuntimeError as error:
                failed = getattr(error, 'quota', None)
        if not failed or not failed['callbackError'] or failed['callbackError']['type'] != 'QuotaExceeded' or paths[1].stat().st_size > 16:
            raise ValueError('Self-check did not refuse an actual over-quota callback')
        if (api.exited(handle) or not api.in_job(handle, owned.job)
                or not same_identity(api.identity_from_handle(handle, owned.identity['pid']), owned.identity)):
            raise ValueError('Self-check target ownership changed')
        return dict(status='passed', actualWindowsOwnedChildSelfCheck=True, sourceSha256=_source_sha(),
                    success=success, failure=failed, successfulDumpBytes=paths[0].stat().st_size,
                    successDumpDirectory=success_directory,
                    rejectedDumpBytes=paths[1].stat().st_size, callbackLayoutProjection=callback_layout(),
                    targetReadiness=readiness,
                    compiledSdkAbiVerified=False, fullMemoryRequested=False)
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_failures = []
        def cleanup(stage, action):
            try:
                action()
            except Exception as error:
                cleanup_failures.append(dict(stage=stage, type=type(error).__name__[:128],
                                             message=str(error)[:512]))
        if handle:
            cleanup('close-read-handle', lambda: api.close(handle))
        if owned.process.stdout is not None:
            cleanup('close-readiness-pipe', owned.process.stdout.close)
        cleanup('terminate-owned-tree', owned.terminate_tree)
        cleanup('wait-owned-child', lambda: owned.process.wait(timeout=1))
        cleanup('close-owned-job', lambda: owned.close_before(time.monotonic() + 1))
        for path in paths:
            cleanup('remove-' + path.name, lambda path=path: path.unlink(missing_ok=True))
        if cleanup_failures:
            text = json.dumps(cleanup_failures, separators=(',', ':'), ensure_ascii=True)
            if primary is not None:
                primary.selfCheckWorkerCleanupFailures = cleanup_failures
                primary.add_note('Owned self-check cleanup also failed: ' + text)
            else:
                error = RuntimeError('Owned minidump self-check cleanup failed: ' + text)
                error.selfCheckWorkerCleanupFailures = cleanup_failures
                raise error


def _source_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def self_check(directory, *, timeout=20):
    """Run bounded actual-Windows writer controls before enabling an observer."""
    global _LAST_SELF_CHECK
    _LAST_SELF_CHECK = None
    if os.name != 'nt' or not 0 < timeout <= 20:
        raise ValueError('Actual Windows self-check with <=20s bound is required')
    from windows_process import OwnedProcess, private_directory
    directory = Path(directory); directory.mkdir(exist_ok=False); private_directory(directory)
    command = [sys.executable, str(Path(__file__).resolve()), '--self-check-worker', str(directory)]
    deadline = time.monotonic() + timeout
    owned = OwnedProcess(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    primary = None
    value = None
    try:
        try:
            owned.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuntimeError('Actual minidump self-check exceeded its bounded deadline')
        path = directory / 'self-check.json'
        if not path.is_file() or path.stat().st_size > SELF_CHECK_METADATA:
            raise RuntimeError('Actual minidump self-check failed or omitted its bounded receipt')
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get('sourceSha256') != _source_sha():
            raise RuntimeError('Actual minidump self-check receipt/source differs')
        if owned.process.returncode != 0 or value.get('status') != 'passed':
            details = dict(workerReturncode=owned.process.returncode,
                           error=value.get('error'), dumpQuota=value.get('dumpQuota'),
                           cleanupFailures=value.get('cleanupFailures'))
            text = encode_json(details, SELF_CHECK_METADATA).decode('ascii').rstrip()
            error = RuntimeError('Actual minidump self-check failed: ' + text)
            error.selfCheckReceipt = value
            error.quota = value.get('dumpQuota')
            error.selfCheckWorkerCleanupFailures = value.get('cleanupFailures')
            raise error
        if value.get('actualWindowsOwnedChildSelfCheck') is not True:
            raise RuntimeError('Actual minidump self-check receipt/source differs')
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            owned.close_before(deadline)
        except Exception as error:
            if primary is None:
                raise
            primary.selfCheckCleanupFailure = dict(type=type(error).__name__, message=str(error)[:1024],
                                                  cleanup=getattr(error, 'cleanup', None))
    _LAST_SELF_CHECK = value
    return value


def require_self_check(receipt):
    if (os.name != 'nt' or receipt is not _LAST_SELF_CHECK or not receipt or receipt['status'] != 'passed'
            or receipt.get('sourceSha256') != _source_sha()):
        raise RuntimeError('Native observer requires this process actual Windows callback quota self-check')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check-worker', type=Path)
    args = parser.parse_args()
    if args.self_check_worker is None:
        parser.error('Only the owned self-check worker entry is available')
    from windows_process import private_file
    try:
        value = _self_check_worker(args.self_check_worker)
    except Exception as error:
        value = dict(status='failed', actualWindowsOwnedChildSelfCheck=False, sourceSha256=_source_sha(),
                     error=dict(type=type(error).__name__, message=str(error)[:2048]),
                     dumpQuota=getattr(error, 'quota', None),
                     cleanupFailures=getattr(error, 'selfCheckWorkerCleanupFailures', None))
        write_json(args.self_check_worker / 'self-check.json', value, SELF_CHECK_METADATA, protect=private_file)
        raise
    write_json(args.self_check_worker / 'self-check.json', value, SELF_CHECK_METADATA, protect=private_file)


if __name__ == '__main__':
    main()
