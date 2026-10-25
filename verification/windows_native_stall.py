"""Test-only source-CI native diagnostics. No runtime fallback or network access.

The command owner observes phases and retains its existing deadline. A separate
collector receives only an explicitly inherited read-only process handle and a
QUERY-only job handle. It never opens a process by an inherited PID or kills the
target. Only its parent can terminate the collector after its short deadline.
"""
import argparse
import ctypes as C
import datetime
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time

from windows_process import DWORD, HANDLE, _api, private_directory, private_file, same_identity, verify_private_path
import windows_dump_quota as dump_quota

FLAGS = 0x1000  # MiniDumpNormal + MiniDumpWithThreadInfo; no full/process memory.
MAX_DUMP = 128 * 1024**2
MAX_DLL = 512 * 1024**2
MAX_COLLECTOR_LOG_TAIL = 32 * 1024
PHASE = re.compile(r'^\s*GDAL4K_VECTOR_COLUMNS_PHASE trial=(\d+) driver=([A-Za-z0-9_]+) phase=([A-Za-z0-9-]+)\s*$')
WORKER = re.compile(r'^\s*GDAL4K_JVM_HANG_AGENT PREMAIN_COMPLETE workerPid=([1-9]\d*)\b')
READ = re.compile(r'read-(\d+)-before\Z')
AFTER = re.compile(r'read-(\d+)-after-rows-\d+\Z')


class ADDRESS64(C.Structure):
    _fields_ = [('Offset', C.c_uint64), ('Segment', C.c_uint16), ('Mode', C.c_uint32)]


class KDHELP64(C.Structure):
    _fields_ = [('Thread', C.c_uint64)] + [(n, C.c_uint32) for n in
               ['ThCallbackStack', 'ThCallbackBStore', 'NextCallback', 'FramePointer']] + [(n, C.c_uint64) for n in
               ['KiCallUserMode', 'KeUserCallbackDispatcher', 'SystemRangeStart', 'KiUserExceptionDispatcher', 'StackBase', 'StackLimit']] + [('Reserved', C.c_uint64 * 5)]


class STACKFRAME64(C.Structure):
    _fields_ = [(n, ADDRESS64) for n in ['AddrPC', 'AddrReturn', 'AddrFrame', 'AddrStack', 'AddrBStore']] + [
        ('FuncTableEntry', C.c_void_p), ('Params', C.c_uint64 * 4), ('Far', C.c_int), ('Virtual', C.c_int),
        ('Reserved', C.c_uint64 * 3), ('KdHelp', KDHELP64)]


def stackwalk_layout():
    # Verified with pinned Clang21 targeting x86_64 MinGW headers, including
    # sizeof/alignof(CONTEXT)=1232/16 and Rsp/Rbp/Rip=152/160/248.
    value = {'ADDRESS64': C.sizeof(ADDRESS64), 'ADDRESS64.Mode': ADDRESS64.Mode.offset,
             'KDHELP64': C.sizeof(KDHELP64), 'STACKFRAME64': C.sizeof(STACKFRAME64),
             'FuncTableEntry': STACKFRAME64.FuncTableEntry.offset, 'Params': STACKFRAME64.Params.offset,
             'Far': STACKFRAME64.Far.offset, 'Reserved': STACKFRAME64.Reserved.offset, 'KdHelp': STACKFRAME64.KdHelp.offset}
    if value != {'ADDRESS64': 16, 'ADDRESS64.Mode': 12, 'KDHELP64': 112, 'STACKFRAME64': 264,
                 'FuncTableEntry': 80, 'Params': 88, 'Far': 120, 'Reserved': 128, 'KdHelp': 152}:
        raise ValueError('Unsupported native StackWalk64 ABI')
    return value


def _sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as output:
        private_file(path)
        output.write(json.dumps(value, indent=2) + '\n')
        output.flush(); os.fsync(output.fileno())


def dump_inventory(path):
    """Read native contexts and module paths; never claim to unwind the PCs."""
    size = Path(path).stat().st_size
    if not 32 <= size <= MAX_DUMP:
        raise ValueError('Native dump exceeds header/size bound')
    with Path(path).open('rb') as source:
        signature, version, count, directory, checksum, stamp, flags = struct.unpack('<IIIIIIQ', source.read(32))
        if signature != 0x504d444d or not 0 < count <= 4096 or directory + count * 12 > size or flags & 2:
            raise ValueError('Invalid/full-memory minidump header')
        source.seek(directory)
        entries = [struct.unpack('<III', source.read(12)) for _ in range(count)]
        streams = {}
        for kind, amount, offset in entries:
            if offset + amount > size:
                raise ValueError('Minidump stream exceeds file')
            if kind in (3, 4, 17):
                if kind in streams:
                    raise ValueError('Duplicate required minidump stream')
                streams[kind] = (amount, offset)
        if set(streams) != {3, 4, 17}:
            raise ValueError('Native thread/module/thread-info stream missing')
        info_bytes, info_offset = streams[17]
        if info_bytes < 12: raise ValueError('Truncated native thread-info header')
        source.seek(info_offset); info_header, info_entry, info_count = struct.unpack('<III', source.read(12))
        if info_header < 12 or info_entry < 64 or not 0 < info_count <= 16384 or info_header + info_entry * info_count > info_bytes:
            raise ValueError('Invalid native thread-info table')
        creations = {}
        for i in range(info_count):
            source.seek(info_offset + info_header + info_entry * i); row = source.read(64)
            tid, = struct.unpack_from('<I', row); created, = struct.unpack_from('<Q', row, 16)
            if tid in creations: raise ValueError('Duplicate native thread-info identity')
            creations[tid] = created
        threads = []
        amount, offset = streams[3]
        source.seek(offset); n, = struct.unpack('<I', source.read(4))
        if not 0 < n <= 16384 or amount < 4 + 48 * n:
            raise ValueError('Invalid native thread list')
        for i in range(n):
            source.seek(offset + 4 + i * 48); row = source.read(48)
            tid, = struct.unpack_from('<I', row)
            context_size, context_rva = struct.unpack_from('<II', row, 40)
            if not context_size or context_rva + context_size > size:
                raise ValueError('Native thread context missing/outside dump')
            stack_start, stack_size, stack_rva = struct.unpack_from('<QII', row, 24)
            if stack_rva + stack_size > size: raise ValueError('Native stack memory exceeds dump')
            threads.append({'threadId': tid, 'creationTime100ns': creations.get(tid),
                            'contextBytes': context_size, 'contextRva': context_rva,
                            'stackStart': stack_start, 'stackBytes': stack_size, 'stackRva': stack_rva})
        amount, offset = streams[4]
        source.seek(offset); n, = struct.unpack('<I', source.read(4))
        if not 0 < n <= 16384 or amount < 4 + 108 * n:
            raise ValueError('Invalid native module list')
        modules = []
        for i in range(n):
            source.seek(offset + 4 + i * 108); row = source.read(108)
            base, image_size, checksum, timestamp, name_rva = struct.unpack_from('<QIIII', row)
            if name_rva + 4 > size:
                raise ValueError('Module name outside dump')
            source.seek(name_rva); length, = struct.unpack('<I', source.read(4))
            if length > 65536 or length % 2 or name_rva + 4 + length > size:
                raise ValueError('Invalid module name')
            name = source.read(length).decode('utf-16le', errors='strict')
            modules.append({'path': name, 'baseAddress': base, 'sizeOfImage': image_size, 'timeDateStamp': timestamp})
        return {'bytes': size, 'flags': flags, 'nativeThreads': threads, 'threadInfoEntries': info_count, 'modules': modules,
                'stackUnwindingPerformed': False, 'fullMemoryRequested': False}


def _frame(pc, sp, modules):
    module = next((m for m in modules if m['baseAddress'] <= pc < m['baseAddress'] + m['sizeOfImage']), None)
    return {'pc': pc, 'sp': sp, 'module': module['path'] if module else None,
            'moduleOffset': pc - module['baseAddress'] if module else None, 'functionNameClaimed': False}


def _native_stacks(dbg, api, process_handle, target, dump, inventory, directory, *, bounded_metadata=False):
    """Unwind captured contexts/stacks; never suspend a target thread.

    Forced collector termination cannot strand the target in SuspendThread.
    DbgHelp reads immutable module unwind data from the owned live process; stack
    reads use only the already-captured dump, so all contexts share its snapshot.
    """
    stackwalk_layout()
    WORD64 = C.c_uint64; BOOL = C.c_int
    READ_MEMORY = C.WINFUNCTYPE(BOOL, HANDLE, WORD64, C.c_void_p, DWORD, C.POINTER(DWORD))
    TABLE = C.WINFUNCTYPE(C.c_void_p, HANDLE, WORD64)
    BASE = C.WINFUNCTYPE(WORD64, HANDLE, WORD64)
    configure = dbg.SymSetOptions; configure.argtypes = [DWORD]; configure.restype = DWORD
    # Ignore environment paths, disable server detection/image searching and
    # prompting. Every module is loaded with SLMFLAG_NO_SYMBOLS; no PDB search.
    options = 0x1000 | 0x20000 | 0x40000 | 0x80000 | 0x200 | 0x2000000
    configure(options)
    initialize = dbg.SymInitializeW; initialize.argtypes = [HANDLE, C.c_wchar_p, BOOL]; initialize.restype = BOOL
    cleanup = dbg.SymCleanup; cleanup.argtypes = [HANDLE]; cleanup.restype = BOOL
    load = dbg.SymLoadModuleExW
    load.argtypes = [HANDLE, HANDLE, C.c_wchar_p, C.c_wchar_p, WORD64, DWORD, C.c_void_p, DWORD]; load.restype = WORD64
    access = dbg.SymFunctionTableAccess64; access.argtypes = [HANDLE, WORD64]; access.restype = C.c_void_p
    get_base = dbg.SymGetModuleBase64; get_base.argtypes = [HANDLE, WORD64]; get_base.restype = WORD64
    walk = dbg.StackWalk64
    walk.argtypes = [DWORD, HANDLE, HANDLE, C.POINTER(STACKFRAME64), C.c_void_p, READ_MEMORY, TABLE, BASE, C.c_void_p]
    walk.restype = BOOL
    read_process = api.k.ReadProcessMemory
    read_process.argtypes = [HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]; read_process.restype = BOOL
    open_thread = api.k.OpenThread; open_thread.argtypes = [DWORD, BOOL, DWORD]; open_thread.restype = HANDLE
    process_of_thread = api.k.GetProcessIdOfThread; process_of_thread.argtypes = [HANDLE]; process_of_thread.restype = DWORD
    from windows_process import FILETIME
    thread_times = api.k.GetThreadTimes; thread_times.argtypes = [HANDLE] + [C.POINTER(FILETIME)] * 4; thread_times.restype = BOOL
    drive_type = api.k.GetDriveTypeW; drive_type.argtypes = [C.c_wchar_p]; drive_type.restype = DWORD
    search = directory / 'offline-images'; search.mkdir(exist_ok=False); private_directory(search)
    api.check(initialize(process_handle, str(search), False))
    rows, module_loads = [], []
    logged_threads, omitted_threads, metadata_truncated = 0, 0, False
    omitted_modules = max(0, len(inventory['modules']) - 128) if bounded_metadata else 0
    segments = [t for t in inventory['nativeThreads'] if t['stackBytes']]
    log_path = directory / 'native-stacks.jsonl'
    options = {} if bounded_metadata else {'encoding': 'utf-8'}
    with Path(dump).open('rb') as source, log_path.open('xb' if bounded_metadata else 'x', **options) as log:
        private_file(directory / 'native-stacks.jsonl')
        bounded_log = dump_quota.BoundedJsonl(log) if bounded_metadata else None
        try:
            for module in inventory['modules'][:128] if bounded_metadata else inventory['modules']:
                observed = module['path']; local = observed[4:] if observed.startswith('\\\\?\\') else observed
                drive, _ = ntpath.splitdrive(local)
                if not re.fullmatch(r'[A-Za-z]:', drive) or drive_type(drive + '\\') not in (3, 6):
                    module_loads.append({'path': observed, 'status': 'nonlocal-image-refused'}); continue
                try:
                    raw_path = Path(observed)
                    for ancestor in [raw_path, *raw_path.parents]:
                        if ancestor.is_symlink() or getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400:
                            raise ValueError('Offline unwind image has a reparse ancestor')
                    path = raw_path.resolve(strict=True)
                    canonical = str(path); canonical = canonical[4:] if canonical.startswith('\\\\?\\') else canonical
                    canonical_drive, _ = ntpath.splitdrive(canonical)
                    if not re.fullmatch(r'[A-Za-z]:', canonical_drive) or drive_type(canonical_drive + '\\') not in (3, 6):
                        raise ValueError('Canonical unwind image is not on a local drive')
                    loaded = load(process_handle, None, str(path), None, module['baseAddress'], module['sizeOfImage'], None, 0x4)
                    module_loads.append({'path': observed, 'status': 'offline-image-no-symbols-loaded' if loaded else 'image-load-failed',
                                         'error': C.get_last_error() if not loaded else 0})
                except (OSError, ValueError) as error:
                    module_loads.append({'path': observed, 'status': 'image-read-failed', 'message': str(error)})

            def read_memory_impl(handle, address, output, amount, returned):
                returned[0] = 0
                if not 0 < amount <= 1024**2: return 0
                segment = next((t for t in segments if t['stackStart'] <= address and address + amount <= t['stackStart'] + t['stackBytes']), None)
                if segment:
                    source.seek(segment['stackRva'] + address - segment['stackStart']); data = source.read(amount)
                    if len(data) != amount: return 0
                    C.memmove(output, data, amount); returned[0] = amount; return 1
                # No arbitrary heap/PEB reads: only captured module image ranges.
                if not any(m['baseAddress'] <= address and address + amount <= m['baseAddress'] + m['sizeOfImage'] for m in inventory['modules']): return 0
                done = C.c_size_t(); success = read_process(handle, C.c_void_p(address), output, amount, C.byref(done))
                returned[0] = min(done.value, amount); return bool(success and done.value == amount)

            callback_errors = []
            def read_memory(handle, address, output, amount, returned):
                try: return read_memory_impl(handle, address, output, amount, returned)
                except Exception as error:
                    returned[0] = 0
                    if len(callback_errors) < 8: callback_errors.append({'type': type(error).__name__, 'message': str(error)})
                    return 0

            reader, table, base = READ_MEMORY(read_memory), TABLE(access), BASE(get_base)
            for thread in inventory['nativeThreads']:
                row = {'threadId': thread['threadId'], 'creationTime100ns': thread['creationTime100ns'], 'frames': [],
                       'contextSource': 'same-minidump-snapshot', 'targetSuspendedByCollector': False}
                handle = None
                try:
                    handle = api.check(open_thread(0x800, False, thread['threadId']))  # QUERY_LIMITED_INFORMATION only.
                    if process_of_thread(handle) != target['pid']: raise ValueError('Captured thread no longer belongs to target PID')
                    created, exited, kernel, user = (FILETIME() for _ in range(4))
                    api.check(thread_times(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)))
                    if ((created.high << 32) | created.low) != thread['creationTime100ns']:
                        raise ValueError('Captured thread creation identity changed')
                    source.seek(thread['contextRva']); context = source.read(thread['contextBytes'])
                    if len(context) < 1232 or not struct.unpack_from('<I', context, 48)[0] & 0x100000:
                        raise ValueError('Captured context is not supported AMD64 CONTEXT')
                    storage = C.create_string_buffer(len(context) + 15); pointer = (C.addressof(storage) + 15) & ~15
                    C.memmove(pointer, context, len(context))
                    rip, rsp, rbp = (struct.unpack_from('<Q', context, off)[0] for off in (248, 152, 160))
                    frame = STACKFRAME64()
                    for address, value in [(frame.AddrPC, rip), (frame.AddrStack, rsp), (frame.AddrFrame, rbp)]: address.Offset = value; address.Mode = 3
                    row['frames'].append(_frame(rip, rsp, inventory['modules'])); seen = {(rip, rsp)}
                    for iteration in range(128):
                        if not walk(0x8664, process_handle, handle, C.byref(frame), C.c_void_p(pointer), reader, table, base, None):
                            row['stop'] = 'StackWalk64-returned-false'; row['lastError'] = C.get_last_error(); break
                        pc, sp = frame.AddrPC.Offset, frame.AddrStack.Offset
                        if not pc: row['stop'] = 'zero-pc'; break
                        if (pc, sp) in seen:
                            if iteration == 0: continue  # First call can report the seeded frame.
                            row['stop'] = 'repeated-frame'; break
                        row['frames'].append(_frame(pc, sp, inventory['modules'])); seen.add((pc, sp))
                        if len(row['frames']) >= 128: row['stop'] = '128-frame-bound'; break
                    row['walkedFramesBeyondContext'] = max(0, len(row['frames']) - 1)
                except Exception as error:
                    row['error'] = {'type': type(error).__name__, 'message': str(error)}
                finally:
                    if handle: api.close(handle)
                if bounded_metadata:
                    kept = {key: value for key, value in row.items() if key != 'frames'}
                    if 'error' in kept:
                        kept['error'] = {'type': kept['error']['type'], 'message': dump_quota.short_text(kept['error']['message'])}
                    kept['frames'] = []
                    for frame in row['frames'][:16]:
                        actual = dict(frame)
                        if actual.get('module') is not None:
                            name = dump_quota.short_text(actual['module'])
                            actual.update(module=name['text'], modulePathTruncated=name['truncated'], fullModulePathSha256=name['fullUtf8Sha256'])
                        kept['frames'].append(actual)
                    kept.update(capturedFrameCount=len(row['frames']), framesTruncated=len(row['frames']) > 16)
                    try:
                        bounded_log.append(kept, reserve=4096)
                        logged_threads += 1
                        metadata_truncated = (metadata_truncated or kept['framesTruncated']
                                              or any(f.get('modulePathTruncated') for f in kept['frames'])
                                              or kept.get('error', {}).get('message', {}).get('truncated', False))
                    except dump_quota.QuotaExceeded:
                        omitted_threads = len(inventory['nativeThreads']) - logged_threads
                        metadata_truncated = True
                        break
                else:
                    rows.append(row); log.write(json.dumps(row) + '\n'); log.flush(); os.fsync(log.fileno())
            if bounded_metadata:
                bounded_log.append({'record': 'bounded-native-stack-summary', 'loggedThreads': logged_threads,
                                    'omittedThreads': omitted_threads, 'moduleLoadsOmitted': omitted_modules,
                                    'metadataTruncated': metadata_truncated or bool(omitted_modules),
                                    'allThreadStacksClaimed': False})
        finally:
            api.check(cleanup(process_handle))
    if bounded_metadata:
        module_sample = [{**{k: v for k, v in row.items() if k not in ['path', 'message']},
                          'path': dump_quota.short_text(row['path']),
                          **({'message': dump_quota.short_text(row['message'])} if 'message' in row else {})}
                         for row in module_loads[:4]]
        error_sample = [{**{k: v for k, v in row.items() if k != 'message'},
                         'message': dump_quota.short_text(row['message'])} for row in callback_errors[:4]]
        sample_truncated = (any(row['path']['truncated'] or row.get('message', {}).get('truncated', False) for row in module_sample)
                            or any(row['message']['truncated'] for row in error_sample))
        return {'stackLog': {'path': str(log_path), 'bytes': log_path.stat().st_size, 'sha256': _sha(log_path)},
                'loggedThreads': logged_threads, 'omittedThreads': omitted_threads,
                'moduleLoadCount': len(module_loads), 'moduleLoadsOmitted': omitted_modules,
                'moduleLoadSample': module_sample,
                'metadataTruncated': metadata_truncated or bool(omitted_modules) or len(module_loads) > 4 or len(callback_errors) > 4 or sample_truncated,
                'allThreadStacksClaimed': False, 'maxFramesPerThreadUnwound': 128, 'maxFramesPerThreadLogged': 16,
                'stackwalkAbi': stackwalk_layout(), 'memoryReadCallbackErrors': error_sample,
                'contextAndStackSource': 'minidump', 'unwindMetadataSource': 'owned-live-process-local-module-images',
                'symbolLookupRequested': False, 'symbolServerRequested': False, 'environmentSymbolPathsIgnored': True,
                'targetSuspendThreadCalls': 0, 'fullMemoryRequested': False}
    return {'threads': rows, 'moduleLoads': module_loads, 'maxFramesPerThread': 128, 'stackwalkAbi': stackwalk_layout(),
            'memoryReadCallbackErrors': callback_errors,
            'contextAndStackSource': 'minidump', 'unwindMetadataSource': 'owned-live-process-local-module-images',
            'symbolLookupRequested': False, 'symbolServerRequested': False, 'environmentSymbolPathsIgnored': True,
            'targetSuspendThreadCalls': 0, 'fullMemoryRequested': False}


def _membership(api, process_handle, job_handle):
    function = api.k.IsProcessInJob
    function.argtypes = [HANDLE, HANDLE, C.POINTER(C.c_int)]; function.restype = C.c_int
    present = C.c_int()
    api.check(function(process_handle, job_handle, C.byref(present)))
    if not present.value:
        raise ValueError('Native diagnostic target is outside the owned Job Object')


def _verify_inherited(api, process_handle, job_handle, identity):
    get_pid = api.k.GetProcessId; get_pid.argtypes = [HANDLE]; get_pid.restype = DWORD
    if int(get_pid(process_handle)) != identity['pid'] or identity['pid'] == os.getpid():
        raise ValueError('Inherited process handle/PID mismatch or in-target collector')
    if not same_identity(api.identity_from_handle(process_handle, identity['pid']), identity) or api.exited(process_handle):
        raise ValueError('Native diagnostic target identity is stale')
    _membership(api, process_handle, job_handle)


def _target_identity(owned, pid):
    if os.name != 'nt' or not owned.job or owned.process.poll() is not None:
        raise ValueError('A live owned Windows process tree is required')
    handle = owned.api.open_process(pid)
    try:
        value = owned.api.identity_from_handle(handle, pid)
        _verify_inherited(owned.api, handle, owned.job, value)
        return value
    finally:
        owned.api.close(handle)


def _copy_native(inventory, directory, expected_sha, required):
    candidates = [m for m in inventory['modules'] if ntpath.basename(m['path']).casefold() == 'gdal4k.dll']
    if not candidates and not required:
        return None
    if len(candidates) != 1:
        raise ValueError('Expected exactly one loaded gdal4k.dll module')
    module = candidates[0]; observed = Path(module['path'])
    if not observed.is_absolute(): raise ValueError('Loaded native DLL path is not absolute')
    # GetModuleFileName can retain a legitimate DOS 8.3 spelling. Expand it,
    # while refusing a symlink/junction at any observed ancestor independently.
    for parent in [observed, *observed.parents]:
        if parent.is_symlink() or getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Loaded native DLL path contains a reparse point')
    source = observed.resolve(strict=True)
    original_stat = observed.stat()
    if (original_stat.st_dev, original_stat.st_ino) != (source.stat().st_dev, source.stat().st_ino):
        raise ValueError('Observed/canonical native DLL file identity differs')
    before = source.stat()
    if not source.is_file() or not 0 < before.st_size <= MAX_DLL or shutil.disk_usage(directory).free - before.st_size < 1024**3:
        raise ValueError('Native DLL size/free-space bound')
    with source.open('rb') as original:
        header = original.read(64)
        if header[:2] != b'MZ':
            raise ValueError('Loaded native module file has no PE header')
        pe_offset, = struct.unpack_from('<I', header, 60)
        original.seek(pe_offset); pe = original.read(88)
        if pe[:4] != b'PE\0\0' or struct.unpack_from('<I', pe, 8)[0] != module['timeDateStamp'] or struct.unpack_from('<I', pe, 80)[0] != module['sizeOfImage']:
            raise ValueError('On-disk DLL header differs from the captured module')
    output = directory / 'gdal4k.dll'
    with source.open('rb') as original, output.open('xb') as destination:
        private_file(output); shutil.copyfileobj(original, destination, 1024**2)
        destination.flush(); os.fsync(destination.fileno())
    after = source.stat(); actual = _sha(output)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or _sha(source) != actual:
        raise ValueError('Loaded native module file changed while copying')
    if expected_sha is not None and actual != expected_sha:
        raise ValueError('Retained native DLL differs from producer SHA')
    return {'path': str(output), 'bytes': output.stat().st_size, 'sha256': actual,
            'sourcePath': str(source), 'observedModulePath': str(observed), 'canonicalSourcePath': str(source),
            'sourceFileIdentity': {'device': before.st_dev, 'inode': before.st_ino, 'bytes': before.st_size,
                                   'mtimeNs': before.st_mtime_ns, 'ctimeNs': before.st_ctime_ns},
            'module': module, 'producerSha256Matched': expected_sha is not None,
            'loadedMemoryDigestClaimed': False}


def _bounded_collector_failure(directory, target, stage, error):
    """Create one compact failure receipt without replacing earlier evidence."""
    path = Path(directory) / 'collector.json'
    if path.exists():
        return False
    dump = Path(directory) / 'native.dmp'
    catalog = {'path': str(dump), 'present': False, 'parsed': False, 'payloadValidClaimed': False}
    try:
        if dump.is_file():
            catalog.update(present=True, bytes=dump.stat().st_size)
    except OSError as observation_error:
        catalog['observationError'] = {'type': type(observation_error).__name__, 'message': str(observation_error)[:1024]}
    value = {'status': 'collector-failed', 'stage': str(stage)[:128], 'target': target,
             'error': {'type': type(error).__name__, 'message': str(error)[:1024]},
             'dumpQuota': getattr(error, 'quota', None),
             'dumpCatalog': catalog, 'rootCauseOrRuntimeAcceptance': False}
    try:
        dump_quota.write_json(path, value, dump_quota.COLLECTOR_METADATA, protect=private_file)
    except FileExistsError:
        return False  # Another earlier record won the create-only race.
    return True


def _run_validated_collector(request_path, request, collect):
    state = {'stage': 'request-validated'}
    try:
        return collect(state)
    except Exception as error:
        try:
            _bounded_collector_failure(Path(request_path).parent, request.get('target'), state['stage'], error)
        except Exception as receipt_error:
            # Preserve the original collector failure if even its private
            # bounded failure record cannot be created.
            error.collectorReceiptFailure = {'type': type(receipt_error).__name__, 'message': str(receipt_error)[:1024]}
        raise


def _collect_validated(request_path, request, process_handle, job_handle, state):
    directory = request_path.parent
    bounded_metadata = request.get('copyNativeDll', True) is False
    identity = request['target']; api = _api()
    state['stage'] = 'verify-target-before-dump'
    _verify_inherited(api, process_handle, job_handle, identity)
    dump = directory / 'native.dmp'; started = _now()
    state['stage'] = 'load-dbghelp'
    dbghelp = C.WinDLL('Dbghelp.dll', use_last_error=True, winmode=0x800)
    with dump.open('xb') as output:
        private_file(dump)
        state['stage'] = 'write-minidump'
        try:
            quota = dump_quota.write_minidump(dbghelp, process_handle, identity['pid'], output, flags=FLAGS)
        except Exception as error:
            if bounded_metadata:
                dump_quota.write_json(directory / 'collector.json',
                                      {'status': 'collector-failed', 'stage': 'write-minidump', 'target': identity,
                                       'error': {'type': type(error).__name__, 'message': str(error)[:1024]},
                                       'dumpQuota': getattr(error, 'quota', None),
                                       'partialDumpBytesObservedBeforeFileClose': dump.stat().st_size,
                                       'partialDumpPayloadValid': False, 'rootCauseOrRuntimeAcceptance': False},
                                      dump_quota.COLLECTOR_METADATA, protect=private_file)
            raise
    state['stage'] = 'verify-target-after-dump'
    _verify_inherited(api, process_handle, job_handle, identity)
    state['stage'] = 'parse-dump-inventory'
    inventory = dump_inventory(dump)
    state['stage'] = 'copy-native-module'
    native = (_copy_native(inventory, directory, request.get('expectedDllSha256'), request['requireGdal'])
              if request.get('copyNativeDll', True) else None)
    try:
        state['stage'] = 'unwind-native-stacks'
        native_stacks = _native_stacks(dbghelp, api, process_handle, identity, dump, inventory, directory,
                                       bounded_metadata=bounded_metadata)
    except Exception as error:
        native_stacks = {'error': {'type': type(error).__name__, 'message': str(error)[:1024] if bounded_metadata else str(error)},
                         'minidumpRetainedAsFallback': True, 'targetSuspendThreadCalls': 0}
    state['stage'] = 'build-collector-receipt'
    receipt = {'status': 'captured', 'startedAtUtc': started, 'completedAtUtc': _now(), 'target': identity,
               'ownedJobMembershipVerified': True, 'collectorPid': os.getpid(), 'collectorOutsideTarget': True,
               'dump': {'path': str(dump), 'sha256': _sha(dump), **(dump_quota.inventory_summary(inventory) if bounded_metadata else inventory)},
               'dumpQuota': quota, 'nativeDll': native, 'nativeStacks': native_stacks,
               'rootCauseOrRuntimeAcceptance': False}
    if bounded_metadata:
        state['stage'] = 'write-collector-receipt'
        dump_quota.write_json(directory / 'collector.json', receipt, dump_quota.COLLECTOR_METADATA, protect=private_file)
    else:
        _write_new(directory / 'collector.json', receipt)


def _collect(request_path, process_handle, job_handle):
    if os.name != 'nt':
        raise RuntimeError('Native stall collection requires actual Windows')
    request_path = Path(request_path); verify_private_path(request_path)
    directory = request_path.parent; verify_private_path(directory, directory=True)
    if request_path.stat().st_size > dump_quota.REQUEST_METADATA:
        raise ValueError('Native collector request exceeds its pre-read metadata cap')
    request = json.loads(request_path.read_text(encoding='utf-8'))
    return _run_validated_collector(request_path, request,
                                    lambda state: _collect_validated(request_path, request, process_handle, job_handle, state))


def _collector_failure_log(directory):
    """Retain only a bounded tail of this private collector's actual output."""
    path = Path(directory) / 'collector.log'
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Collector error log must be an owned regular nonreparse file')
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if signature(opened) != signature(before):
            raise ValueError('Collector error log identity changed before reading')
        offset = max(0, opened.st_size - MAX_COLLECTOR_LOG_TAIL)
        source.seek(offset)
        tail = source.read(MAX_COLLECTOR_LOG_TAIL)
        if signature(os.fstat(source.fileno())) != signature(opened) or signature(path.stat()) != signature(opened):
            raise ValueError('Collector error log changed during bounded reading')
    return dict(path='collector.log', bytesObserved=opened.st_size, tailOffsetBytes=offset,
                tailBytes=len(tail), tailSha256=hashlib.sha256(tail).hexdigest(),
                tailUtf8Replace=tail.decode('utf-8', errors='replace'), tailOnly=True)


class CaptureSession:
    def __init__(self, process, directory, clock, limit, metadata=None):
        self.process, self.directory, self.clock, self.limit = process, directory, clock, limit
        self.started = clock(); self.result = None; self.metadata = metadata or {}

    def poll(self):
        if self.result is not None:
            return self.result
        code = self.process.poll()
        if code is None and self.clock() - self.started < self.limit:
            return None
        if code is None:
            self.process.kill()  # Only this held collector process, never the target/job.
            self.result = {'status': 'collector-timed-out', 'limitSeconds': self.limit}
        else:
            path = self.directory / 'collector.json'
            try:
                bounded = self.metadata.get('boundedNativeMetadata', False)
                if (bounded or code != 0) and path.is_file() and path.stat().st_size > dump_quota.COLLECTOR_METADATA:
                    raise ValueError('Native collector receipt exceeds its metadata quota')
                self.result = json.loads(path.read_text()) if path.is_file() else {'status': 'collector-failed', 'returncode': code}
                if not isinstance(self.result, dict):
                    raise ValueError('Native collector receipt must be an object')
                if code != 0:
                    self.result.update(status='collector-failed', returncode=code)
            except (ValueError, OSError) as error:
                self.result = {'status': 'collector-invalid-receipt', 'returncode': code, 'message': str(error)}
            if code != 0:
                try:
                    log = _collector_failure_log(self.directory)
                    if log is not None:
                        self.result['collectorErrorLog'] = log
                except (ValueError, OSError) as error:
                    self.result['collectorErrorLogFailure'] = {'type': type(error).__name__, 'message': str(error)[:1024]}
        self.result['directory'] = str(self.directory)
        self.result.update(self.metadata)
        return self.result

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.result = {'status': 'collector-cleanup-failed', 'directory': str(self.directory)}
            self.result.update(self.metadata)
        return self.result or self.poll()


def _start_capture(owned, identity, directory, *, expected_sha=None, require_gdal=True,
                   copy_native=True, clock=time.monotonic, limit=20):
    if type(limit) not in (int, float) or not 0 < limit <= 20:
        raise ValueError('Native collector must have a positive bound of at most20s')
    if os.name != 'nt' or not owned.job or owned.process.poll() is not None:
        raise ValueError('A live owned Windows process tree is required')
    if require_gdal and (expected_sha is None or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)):
        raise ValueError('Source native capture requires the current producer DLL SHA')
    directory = Path(directory)
    directory.mkdir(exist_ok=False); private_directory(directory)
    if directory.resolve(strict=True) != directory:
        raise ValueError('Native diagnostic directory has a symlink ancestor')
    api = owned.api; handle = api.check(api.k.OpenProcess(0x400 | 0x10 | 0x100000, False, identity['pid']))
    duplicates = []; process = None
    try:
        _verify_inherited(api, handle, owned.job, identity)
        duplicate = api.k.DuplicateHandle
        duplicate.argtypes = [HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), DWORD, C.c_int, DWORD]; duplicate.restype = C.c_int
        get_current = api.k.GetCurrentProcess; get_current.argtypes = []; get_current.restype = HANDLE
        current = get_current()
        for original, rights in [(handle, 0x400 | 0x10 | 0x100000), (owned.job, 0x4)]:
            result = HANDLE(); api.check(duplicate(current, original, current, C.byref(result), rights, True, 0))
            duplicates.append(result.value)
        request = directory / 'request.json'
        value = {'target': identity, 'expectedDllSha256': expected_sha,
                 'requireGdal': require_gdal, 'copyNativeDll': copy_native}
        if copy_native:
            _write_new(request, value)
        else:
            dump_quota.write_json(request, value, dump_quota.REQUEST_METADATA, protect=private_file)
        startup = subprocess.STARTUPINFO(); startup.lpAttributeList = {'handle_list': duplicates}
        command = [sys.executable, str(Path(__file__).resolve()), '--collect', str(request),
                   '--process-handle', str(duplicates[0]), '--job-handle', str(duplicates[1])]
        if copy_native:
            log = directory / 'collector.log'
            with log.open('xb') as output:
                private_file(log)
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                           startupinfo=startup, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       startupinfo=startup, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        metadata = {'collectorHeldProcess': {'pid': process.pid,
                                             'creationTime100ns': api.creation_time_from_handle(process._handle)},
                    'targetIdentityRequested': identity, 'explicitInheritedHandleCount': 2,
                    'boundedNativeMetadata': not copy_native,
                    'inheritedTargetAccess': 'QUERY_INFORMATION|VM_READ|SYNCHRONIZE',
                    'inheritedJobAccess': 'JOB_OBJECT_QUERY', 'collectorTimeoutSeconds': limit}
        return CaptureSession(process, directory, clock, limit, metadata)
    except BaseException:
        if process is not None:
            if process.poll() is None: process.kill()
            process.wait(timeout=1)
        raise
    finally:
        for duplicate in duplicates: api.close(duplicate)
        api.close(handle)


class PhaseTracker:
    def __init__(self, clock=time.monotonic):
        self.clock, self.current, self.sequence = clock, None, 0

    def observe_line(self, line):
        match = PHASE.fullmatch(line)
        if not match: return
        trial, driver, phase = match.groups(); read = READ.fullmatch(phase); after = AFTER.fullmatch(phase)
        key = (int(trial), driver, int(read.group(1))) if read else None
        if read:
            if self.current and self.current['key'] == key: return
            self.sequence += 1
            self.current = {'key': key, 'trial': int(trial), 'driver': driver, 'readIndex': int(read.group(1)),
                            'sequence': self.sequence, 'started': self.clock(), 'phase': phase}
        elif self.current:
            if after:
                if (int(trial), driver, int(after.group(1))) == self.current['key']: self.current = None
            else:
                self.current = None


class ColumnReadWatchdog:
    def __init__(self, owned, directory, stem, *, expected_dll_sha256=None, stall_seconds=60, fail_seconds=180, collector_seconds=20):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', stem): raise ValueError('Invalid diagnostic stem')
        if not 0 < stall_seconds < fail_seconds or not 0 < collector_seconds <= 20: raise ValueError('Invalid native diagnostic limits')
        if expected_dll_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', expected_dll_sha256): raise ValueError('Invalid producer DLL SHA')
        self.owned, self.expected_sha = owned, expected_dll_sha256
        self.directory = Path(directory).resolve() / (stem + '.native-stall')
        self.directory.mkdir(exist_ok=False); private_directory(self.directory)
        self.stall, self.fail, self.collector_limit = stall_seconds, fail_seconds, collector_seconds
        self.lock = threading.Lock(); self.buffer = b''; self.tracker = PhaseTracker()
        self.target = None; self.worker_error = None; self.attempted = set(); self.sessions = []; self.receipts = []
        self.closed = False

    def observe(self, data):
        with self.lock:
            self.buffer += data
            if len(self.buffer) > 1024**2 and b'\n' not in self.buffer:
                self.buffer = b''; self.worker_error = 'Oversized diagnostic output line'; return
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if len(line) > 1024**2:
                    self.worker_error = 'Oversized diagnostic output line'; continue
                text = line.rstrip(b'\r').decode('utf-8', errors='replace')
                worker = WORKER.match(text)
                if worker:
                    try:
                        who = _target_identity(self.owned, int(worker.group(1)))
                        if ntpath.basename(who['imagePath']).casefold() != 'java.exe': raise ValueError('Diagnostic worker is not java.exe')
                        self.target = who; self.worker_error = None
                    except Exception as error:
                        self.target = None; self.worker_error = str(error)
                old_phase = self.tracker.current
                self.tracker.observe_line(text)
                if self.tracker.current is not None and self.tracker.current is not old_phase:
                    self.tracker.current['target'] = dict(self.target) if self.target is not None else None
                    self.tracker.current['workerError'] = self.worker_error

    def poll(self):
        with self.lock:
            for phase, session in self.sessions:
                result = session.poll()
                if result is not None and not any(r.get('sequence') == phase['sequence'] for r in self.receipts):
                    self.receipts.append({'sequence': phase['sequence'], 'phase': phase, 'capture': result})
            phase = self.tracker.current
            if phase is None: return None
            elapsed = self.tracker.clock() - phase['started']
            if elapsed >= self.stall and phase['sequence'] not in self.attempted:
                self.attempted.add(phase['sequence'])
                try:
                    if len(self.sessions) >= 2: raise ValueError('Native diagnostic capture budget exhausted')
                    if phase['target'] is None: raise ValueError(phase['workerError'] or 'No owned source JVM agent identity observed')
                    session = _start_capture(self.owned, dict(phase['target']), self.directory / ('phase-' + str(phase['sequence'])),
                                             expected_sha=self.expected_sha, clock=self.tracker.clock, limit=self.collector_limit)
                    self.sessions.append((dict(phase), session))
                except Exception as error:
                    self.receipts.append({'sequence': phase['sequence'], 'phase': dict(phase), 'capture': {'status': 'capture-refused', 'message': str(error)}})
            if elapsed >= self.fail:
                return {'status': 'native-phase-stalled', 'elapsedSeconds': elapsed, 'phase': dict(phase), 'diagnostics': list(self.receipts)}
            return None

    def close(self):
        with self.lock:
            if self.closed: return list(self.receipts)
            self.closed = True
            for phase, session in self.sessions:
                result = session.close()
                existing = next((r for r in self.receipts if r['sequence'] == phase['sequence']), None)
                if existing is None: self.receipts.append({'sequence': phase['sequence'], 'phase': phase, 'capture': result})
                else: existing['capture'] = result
            path = self.directory / 'watchdog.json'
            _write_new(path, {'diagnostics': self.receipts, 'rootCauseOrRuntimeAcceptance': False})
            return list(self.receipts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', type=Path, required=True)
    parser.add_argument('--process-handle', type=int, required=True)
    parser.add_argument('--job-handle', type=int, required=True)
    args = parser.parse_args()
    _collect(args.collect, args.process_handle, args.job_handle)
