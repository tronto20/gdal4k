"""Owned Windows CI processes and private fixture files; no global state changes.

The Win32 calls run only on Windows. Pure receipt validation is portable and is
not evidence of a Windows process or kernel-memory measurement.
"""
import ctypes as C
import json
from functools import lru_cache
import math
import os
import ntpath
from pathlib import Path
import socket
import struct
import subprocess
import time

DWORD = C.c_uint32
HANDLE = C.c_void_p
SIZE_T = C.c_size_t


class FILETIME(C.Structure):
    _fields_ = [('low', DWORD), ('high', DWORD)]


class MEMORY_COUNTERS(C.Structure):
    _fields_ = [('cb', DWORD), ('PageFaultCount', DWORD)] + [
        (name, SIZE_T) for name in ['PeakWorkingSetSize', 'WorkingSetSize',
                                   'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                                   'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                                   'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage']]


class BASIC_LIMITS(C.Structure):
    _fields_ = [('PerProcessUserTimeLimit', C.c_int64), ('PerJobUserTimeLimit', C.c_int64),
                ('LimitFlags', DWORD), ('MinimumWorkingSetSize', SIZE_T),
                ('MaximumWorkingSetSize', SIZE_T), ('ActiveProcessLimit', DWORD),
                ('Affinity', SIZE_T), ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ['ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount']]


class EXTENDED_LIMITS(C.Structure):
    _fields_ = [('BasicLimitInformation', BASIC_LIMITS), ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class JOB_ACCOUNTING(C.Structure):
    _fields_ = [(n, C.c_int64) for n in ['TotalUserTime', 'TotalKernelTime',
                'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime']] + [
                (n, DWORD) for n in ['TotalPageFaultCount', 'TotalProcesses',
                                    'ActiveProcesses', 'TotalTerminatedProcesses']]


class THREADENTRY(C.Structure):
    _fields_ = [('dwSize', DWORD), ('cntUsage', DWORD), ('th32ThreadID', DWORD),
                ('th32OwnerProcessID', DWORD), ('tpBasePri', C.c_int32),
                ('tpDeltaPri', C.c_int32), ('dwFlags', DWORD)]


class Win32:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Windows process and ACL operations require actual Windows')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        self.p = C.WinDLL('psapi', use_last_error=True)
        self.ip = C.WinDLL('iphlpapi', use_last_error=True)
        specs = [
            (self.k, 'OpenProcess', [DWORD, C.c_int, DWORD], HANDLE),
            (self.k, 'CloseHandle', [HANDLE], C.c_int),
            (self.k, 'GetCurrentProcess', [], HANDLE),
            (self.k, 'GetProcessId', [HANDLE], DWORD),
            (self.k, 'GetProcessTimes', [HANDLE] + [C.POINTER(FILETIME)] * 4, C.c_int),
            (self.k, 'QueryFullProcessImageNameW', [HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD)], C.c_int),
            (self.k, 'WaitForSingleObject', [HANDLE, DWORD], DWORD),
            (self.k, 'CreateJobObjectW', [HANDLE, C.c_wchar_p], HANDLE),
            (self.k, 'SetInformationJobObject', [HANDLE, C.c_int, HANDLE, DWORD], C.c_int),
            (self.k, 'AssignProcessToJobObject', [HANDLE, HANDLE], C.c_int),
            (self.k, 'TerminateJobObject', [HANDLE, C.c_uint], C.c_int),
            (self.k, 'QueryInformationJobObject', [HANDLE, C.c_int, HANDLE, DWORD, C.POINTER(DWORD)], C.c_int),
            (self.k, 'IsProcessInJob', [HANDLE, HANDLE, C.POINTER(C.c_int)], C.c_int),
            (self.k, 'CreateToolhelp32Snapshot', [DWORD, DWORD], HANDLE),
            (self.k, 'Thread32First', [HANDLE, C.POINTER(THREADENTRY)], C.c_int),
            (self.k, 'Thread32Next', [HANDLE, C.POINTER(THREADENTRY)], C.c_int),
            (self.k, 'OpenThread', [DWORD, C.c_int, DWORD], HANDLE),
            (self.k, 'ResumeThread', [HANDLE], DWORD),
            (self.k, 'TerminateProcess', [HANDLE, C.c_uint], C.c_int),
            (self.k, 'LocalFree', [HANDLE], HANDLE),
            (self.p, 'GetProcessMemoryInfo', [HANDLE, C.POINTER(MEMORY_COUNTERS), DWORD], C.c_int),
            (self.a, 'OpenProcessToken', [HANDLE, DWORD, C.POINTER(HANDLE)], C.c_int),
            (self.a, 'GetTokenInformation', [HANDLE, C.c_int, HANDLE, DWORD, C.POINTER(DWORD)], C.c_int),
            (self.a, 'ConvertSidToStringSidW', [HANDLE, C.POINTER(C.c_void_p)], C.c_int),
            (self.a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
             [C.c_wchar_p, DWORD, C.POINTER(HANDLE), C.POINTER(DWORD)], C.c_int),
            (self.a, 'GetSecurityDescriptorDacl', [HANDLE, C.POINTER(C.c_int), C.POINTER(HANDLE), C.POINTER(C.c_int)], C.c_int),
            (self.a, 'GetSecurityDescriptorOwner', [HANDLE, C.POINTER(HANDLE), C.POINTER(C.c_int)], C.c_int),
            (self.a, 'SetNamedSecurityInfoW', [C.c_wchar_p, C.c_int, DWORD, HANDLE, HANDLE, HANDLE, HANDLE], DWORD),
            (self.a, 'GetNamedSecurityInfoW', [C.c_wchar_p, C.c_int, DWORD] + [C.POINTER(HANDLE)] * 5, DWORD),
            (self.a, 'GetSecurityDescriptorControl', [HANDLE, C.POINTER(C.c_uint16), C.POINTER(DWORD)], C.c_int),
            (self.a, 'GetAclInformation', [HANDLE, HANDLE, DWORD, C.c_int], C.c_int),
            (self.a, 'GetAce', [HANDLE, DWORD, C.POINTER(HANDLE)], C.c_int),
            (self.ip, 'GetExtendedTcpTable', [HANDLE, C.POINTER(DWORD), C.c_int, DWORD, C.c_int, DWORD], DWORD)]
        for dll, name, args, result in specs:
            fn = getattr(dll, name); fn.argtypes = args; fn.restype = result

    @staticmethod
    def check(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def close(self, handle):
        self.check(self.k.CloseHandle(handle))

    def open_process(self, pid):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            error = C.get_last_error()
            if error in (87, 1168):
                raise ProcessLookupError(pid)
            raise C.WinError(error)
        return handle

    def creation_time_from_handle(self, handle):
        created, exited, kernel, user = (FILETIME() for _ in range(4))
        self.check(self.k.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)))
        return (created.high << 32) | created.low

    def exited(self, handle, timeout_ms=0):
        result = self.k.WaitForSingleObject(handle, timeout_ms)
        if result not in (0, 258):
            raise C.WinError(C.get_last_error())
        return result == 0

    def identity_from_handle(self, handle, pid):
        created = self.creation_time_from_handle(handle)
        size = DWORD(32768); image = C.create_unicode_buffer(size.value)
        self.check(self.k.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)))
        return dict(pid=int(pid), creationTime100ns=created, imagePath=image.value)

    def identity(self, pid):
        handle = self.open_process(pid)
        try:
            if self.exited(handle):
                raise ProcessLookupError(pid)
            try:
                return self.identity_from_handle(handle, pid)
            except OSError:
                # Image queries can fail if the process exits during the query.
                # Only a signaled held handle proves exit; live errors propagate.
                if self.exited(handle):
                    raise ProcessLookupError(pid) from None
                raise
        finally:
            self.close(handle)

    def memory(self, handle):
        counters = MEMORY_COUNTERS(); counters.cb = C.sizeof(counters)
        self.check(self.p.GetProcessMemoryInfo(handle, C.byref(counters), counters.cb))
        if counters.PeakWorkingSetSize <= 0:
            raise RuntimeError('Win32 returned no final PeakWorkingSetSize; no sampled fallback is permitted')
        return int(counters.PeakWorkingSetSize)

    def create_job(self):
        job = self.check(self.k.CreateJobObjectW(None, None))
        limits = EXTENDED_LIMITS(); limits.BasicLimitInformation.LimitFlags = 0x2000
        try:
            self.check(self.k.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
        except BaseException:
            self.close(job); raise
        return job

    def assign_job(self, job, process):
        self.check(self.k.AssignProcessToJobObject(job, process))

    def active_processes(self, job):
        value = JOB_ACCOUNTING()
        self.check(self.k.QueryInformationJobObject(job, 1, C.byref(value), C.sizeof(value), None))
        return int(value.ActiveProcesses)

    def in_job(self, handle, job):
        """Verify membership using held process and Job handles, never a PID name."""
        present = C.c_int()
        self.check(self.k.IsProcessInJob(handle, job, C.byref(present)))
        return bool(present.value)

    def process_id(self, handle):
        return int(self.check(self.k.GetProcessId(handle)))

    def job_process_ids(self, job, max_processes=4096):
        """Read one complete bounded JobObjectBasicProcessIdList snapshot.

        The two DWORD counts precede pointer-sized IDs on both supported ABIs.
        Job membership may change while querying; truncated/unstable lists are
        retried with a strict allocation and retry bound, never treated as full.
        """
        if type(max_processes) is not int or not 0 < max_processes <= 4096:
            raise ValueError('Owned Job PID bound must be within1..4096')
        capacity = min(16, max_processes)
        for _ in range(10):
            size = 2 * C.sizeof(DWORD) + capacity * C.sizeof(SIZE_T)
            buffer = C.create_string_buffer(size)
            returned = DWORD()
            ok = self.k.QueryInformationJobObject(job, 3, buffer, size, C.byref(returned))
            assigned, count = struct.unpack_from('<II', buffer.raw)
            if assigned > max_processes or count > capacity or count > assigned:
                raise ValueError('Owned Job PID list exceeds the configured bound')
            if ok:
                if returned.value and not 8 + count * C.sizeof(SIZE_T) <= returned.value <= size:
                    raise ValueError('Owned Job PID list returned an invalid length')
                if assigned == count:
                    ids = list((SIZE_T * count).from_buffer_copy(buffer.raw, 8))
                    if any(pid <= 0 or pid > 0xffffffff for pid in ids) or len(ids) != len(set(ids)):
                        raise ValueError('Owned Job PID list contains invalid/duplicate IDs')
                    return ids
            else:
                error = C.get_last_error()
                if error != 234:  # ERROR_MORE_DATA is the only retriable error.
                    raise C.WinError(error)
            if capacity >= max_processes:
                raise ValueError('Owned Job PID list is incomplete at the configured bound')
            capacity = min(max_processes, max(capacity * 2, assigned))
        raise RuntimeError('Owned Job PID list remained unstable within the retry bound')

    def terminate_job(self, job):
        self.check(self.k.TerminateJobObject(job, 124))

    def resume_only_thread(self, pid):
        snapshot = self.k.CreateToolhelp32Snapshot(4, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        threads = []
        try:
            entry = THREADENTRY(); entry.dwSize = C.sizeof(entry)
            more = self.k.Thread32First(snapshot, C.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    threads.append(entry.th32ThreadID)
                more = self.k.Thread32Next(snapshot, C.byref(entry))
            if C.get_last_error() != 18:
                raise C.WinError(C.get_last_error())
        finally:
            self.close(snapshot)
        if len(threads) != 1:
            raise RuntimeError('New suspended owned process must have exactly one primary thread')
        thread = self.check(self.k.OpenThread(2, False, threads[0]))
        try:
            if self.k.ResumeThread(thread) != 1:
                raise RuntimeError('Owned primary thread was not suspended exactly once')
        finally:
            self.close(thread)

    def user_sid(self):
        token = HANDLE(); self.check(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)))
        try:
            size = DWORD(); self.a.GetTokenInformation(token, 1, None, 0, C.byref(size))
            buffer = C.create_string_buffer(size.value)
            self.check(self.a.GetTokenInformation(token, 1, buffer, size, C.byref(size)))
            return self.sid_string(C.cast(buffer, C.POINTER(HANDLE))[0])
        finally:
            self.close(token)

    def sid_string(self, sid):
        text = C.c_void_p(); self.check(self.a.ConvertSidToStringSidW(sid, C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            self.k.LocalFree(text)

    def protect(self, path, directory):
        user = self.user_sid(); flags = 'OICI' if directory else ''
        sddl = f'O:{user}D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;{user})'
        descriptor = HANDLE()
        self.check(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None))
        try:
            present, defaulted, acl, owner = C.c_int(), C.c_int(), HANDLE(), HANDLE()
            self.check(self.a.GetSecurityDescriptorDacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)))
            self.check(self.a.GetSecurityDescriptorOwner(descriptor, C.byref(owner), C.byref(defaulted)))
            error = self.a.SetNamedSecurityInfoW(str(path), 1, 0x80000005, owner, None, acl, None)
            if error:
                raise C.WinError(error)
        finally:
            self.k.LocalFree(descriptor)
        return self.inspect_acl(path)

    def inspect_acl(self, path):
        owner, group, acl, sacl, descriptor = (HANDLE() for _ in range(5))
        error = self.a.GetNamedSecurityInfoW(str(path), 1, 5, C.byref(owner), C.byref(group), C.byref(acl), C.byref(sacl), C.byref(descriptor))
        if error:
            raise C.WinError(error)
        try:
            control, revision = C.c_uint16(), DWORD()
            self.check(self.a.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision)))
            info = (DWORD * 3)(); self.check(self.a.GetAclInformation(acl, info, C.sizeof(info), 2))
            entries = []
            for index in range(info[0]):
                ace = HANDLE(); self.check(self.a.GetAce(acl, index, C.byref(ace)))
                raw = C.string_at(ace, 8); kind, flags, size, mask = struct.unpack('<BBHI', raw)
                entries.append(dict(type=kind, flags=flags, mask=mask, sid=self.sid_string(ace.value + 8)))
            return dict(ownerSid=self.sid_string(owner), currentUserSid=self.user_sid(),
                        daclProtected=bool(control.value & 0x1000), entries=entries)
        finally:
            self.k.LocalFree(descriptor)

    def listeners(self):
        size = DWORD(); code = self.ip.GetExtendedTcpTable(None, C.byref(size), False, 2, 3, 0)
        if code != 122:
            raise C.WinError(code)
        buffer = C.create_string_buffer(size.value)
        code = self.ip.GetExtendedTcpTable(buffer, C.byref(size), False, 2, 3, 0)
        if code:
            raise C.WinError(code)
        data = buffer.raw[:size.value]; count = struct.unpack_from('<I', data)[0]
        rows = []
        for index in range(count):
            state, addr, port, _, _, pid = struct.unpack_from('<6I', data, 4 + index * 24)
            rows.append(dict(state=state, localAddress=socket.inet_ntoa(struct.pack('<I', addr)),
                             localPort=socket.ntohs(port & 65535), pid=pid))
        return rows


@lru_cache(maxsize=1)
def _api():
    return Win32()


def process_identity(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('A positive owned PID is required')
    return _api().identity(pid)


def same_identity(first, second):
    return (first['pid'] == second['pid'] and first['creationTime100ns'] == second['creationTime100ns']
            and first['imagePath'].casefold() == second['imagePath'].casefold())


def same_process(identity):
    try:
        return same_identity(identity, process_identity(identity['pid']))
    except ProcessLookupError:
        return False


def wait_process_exit(identity, timeout):
    api = _api()
    try:
        handle = api.open_process(identity['pid'])
    except ProcessLookupError:
        return True
    try:
        if api.creation_time_from_handle(handle) != identity['creationTime100ns']:
            return True  # Original process exited; this reused PID is never stopped.
        return api.exited(handle, max(0, int(timeout * 1000)))
    finally:
        api.close(handle)


def port_owner(port, host='127.0.0.1'):
    if host != '127.0.0.1' or not 0 < port < 65536:
        raise ValueError('Only a loopback IPv4 fixture port is accepted')
    matches = [r for r in _api().listeners() if r['state'] == 2 and r['localPort'] == port
               and r['localAddress'] in (host, '0.0.0.0')]
    if len(matches) > 1:
        raise RuntimeError('Fixture port has ambiguous Windows listeners')
    return dict(process_identity(matches[0]['pid']), **matches[0]) if matches else None


def port_closed(port, host='127.0.0.1'):
    return port_owner(port, host) is None


def validate_private_acl(value, directory):
    allowed = {value['currentUserSid'], 'S-1-5-18'}
    if (value['ownerSid'] != value['currentUserSid'] or not value['daclProtected']
            or len(value['entries']) != 2 or {a['sid'] for a in value['entries']} != allowed
            or any(a['type'] != 0 or a['mask'] != 0x1f01ff or a['flags'] != (3 if directory else 0)
                   for a in value['entries'])):
        raise ValueError('Fixture DACL must grant only current user and SYSTEM full access')
    return value


def _private(path, directory):
    path = Path(path)
    if not path.exists() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('Fixture ACL target must already exist with the expected kind')
    if getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or path.is_symlink():
        raise ValueError('Fixture ACL target cannot be a reparse point')
    return validate_private_acl(_api().protect(path, directory), directory)


def verify_private_path(path, directory=False):
    """Inspect an already-private file without changing its permissions."""
    path = Path(path)
    if not path.exists() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('Private fixture path has the wrong kind')
    if getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or path.is_symlink():
        raise ValueError('Private fixture path cannot be a reparse point')
    return validate_private_acl(_api().inspect_acl(path), directory)


def private_directory(path):
    return _private(path, True)


def private_file(path):
    return _private(path, False)


class OwnedCleanupIncomplete(RuntimeError):
    """A bounded own-Job close did not prove both exit and handle closure."""
    def __init__(self, cleanup):
        self.cleanup = dict(cleanup)
        super().__init__('Owned Windows Job cleanup incomplete: ' + cleanup['reason'])


class OwnedProcess:
    """Assign a suspended child to an owned kill-on-close job before any code runs."""
    def __init__(self, command, **kwargs):
        self.api = _api(); self.job = self.api.create_job(); self.process = None
        self.cleanup = None; self.forced_tree_termination = False
        try:
            self.process = subprocess.Popen(command, creationflags=0x4 | 0x200, **kwargs)
            self.identity = self.api.identity_from_handle(self.process._handle, self.process.pid)
            self.api.assign_job(self.job, self.process._handle)
            self.api.resume_only_thread(self.process.pid)
        except BaseException:
            if self.process is not None:
                self.api.k.TerminateProcess(self.process._handle, 124)
                self.process.wait(timeout=30)
            self.api.close(self.job); self.job = None
            raise

    def peak_after_exit(self):
        if self.process.poll() is None:
            raise ValueError('Final kernel peak requires actual process exit')
        # Keep the handle and image captured before resume. Windows can no longer
        # query an exited image, but its held process object retains creation time.
        if (self.process.pid != self.identity['pid'] or
                self.api.creation_time_from_handle(self.process._handle) != self.identity['creationTime100ns']):
            raise RuntimeError('Owned Windows process identity changed')
        return dict(collector='Win32 GetProcessMemoryInfo', field='PeakWorkingSetSize',
                    reportedUnit='bytes', peakWorkingSetSizeBytes=self.api.memory(self.process._handle),
                    windowsProcess=dict(self.identity), queryPhase='after-process-exit-before-handle-close',
                    returncode=self.process.returncode)

    def _require_live_job(self):
        if not self.job or self.process is None or self.process.poll() is not None:
            raise ValueError('A live owned Windows process tree is required')
        handle = self.process._handle
        if (self.api.exited(handle) or self.api.process_id(handle) != self.identity['pid']
                or not self.api.in_job(handle, self.job)
                or not same_identity(self.api.identity_from_handle(handle, self.identity['pid']), self.identity)):
            raise ValueError('Owned Windows root process identity or Job membership changed')

    @staticmethod
    def _valid_member_identity(value):
        if (type(value) is not dict or type(value.get('pid')) is not int or value['pid'] <= 0
                or type(value.get('creationTime100ns')) is not int or value['creationTime100ns'] <= 0
                or type(value.get('imagePath')) is not str or not ntpath.isabs(value['imagePath'])
                or any(c in value['imagePath'] for c in '\x00\r\n')):
            raise ValueError('Invalid owned Windows member identity')
        return value

    def _member_identity(self, pid, expected=None):
        handle = self.api.open_process(pid)
        try:
            if self.api.exited(handle):
                raise ProcessLookupError(pid)
            # A listed PID can exit and be reused before OpenProcess. Verify
            # membership before reading its image/creation identity; a foreign
            # reused PID is never returned as an owned diagnostic target.
            if self.api.process_id(handle) != pid or not self.api.in_job(handle, self.job):
                raise ValueError('Enumerated process is outside the owned Job Object')
            try:
                first = self._valid_member_identity(self.api.identity_from_handle(handle, pid))
                second = self._valid_member_identity(self.api.identity_from_handle(handle, pid))
            except OSError:
                if self.api.exited(handle):
                    raise ProcessLookupError(pid) from None
                raise
            if (first['pid'] != pid or not same_identity(first, second)
                    or (expected is not None and not same_identity(first, expected))
                    or not self.api.in_job(handle, self.job)):
                raise ValueError('Owned Windows member identity changed')
            if self.api.exited(handle):
                raise ProcessLookupError(pid)
            return first
        finally:
            self.api.close(handle)

    def member_identity(self, pid, expected=None):
        """Return a fresh identity only for an enumerated live member.

        The returned identity is an observation, not a retained process handle.
        A later collector must revalidate its held handle against this identity.
        """
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError('A positive Windows PID is required')
        if expected is not None:
            self._valid_member_identity(expected)
            if expected['pid'] != pid:
                raise ValueError('Expected Windows member PID differs')
        self._require_live_job()
        if pid not in self.api.job_process_ids(self.job):
            raise ValueError('Requested PID is not in the owned Job Object')
        result = self._member_identity(pid, expected)
        self._require_live_job()
        return result

    def member_identities(self, max_processes=4096):
        """Enumerate live own-Job identities with a held handle per observation."""
        self._require_live_job()
        result = []
        for pid in self.api.job_process_ids(self.job, max_processes):
            try:
                result.append(self._member_identity(pid))
            except ProcessLookupError:
                pass  # A signaled held handle proves this listed process exited.
        self._require_live_job()
        return result

    def terminate_tree(self):
        self.api.terminate_job(self.job)
        self.forced_tree_termination = True

    def close(self):
        if self.job is None:
            return self.cleanup
        if self.api.active_processes(self.job):
            self.terminate_tree()
        deadline = time.monotonic() + 30
        while self.api.active_processes(self.job):
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned Windows Job Object still has live processes')
            time.sleep(0.05)
        self.api.close(self.job); self.job = None
        self.cleanup = dict(activeProcessesAfter=0, jobClosed=True,
                            forcedTreeTermination=self.forced_tree_termination)
        return self.cleanup

    def close_before(self, deadline, *, clock=time.monotonic):
        """Close only this held Job without polling past an absolute deadline.

        The bounded path never waits on a PID or changes the existing close().
        CloseHandle still requests the Job's kill-on-close policy if active
        processes remain; that request is not reported as observed process exit.
        Synchronous Win32 calls themselves cannot be interrupted by this clock.
        """
        def finite(value):
            try:
                return type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                return False
        if not finite(deadline) or not callable(clock):
            raise ValueError('A finite absolute cleanup deadline and clock are required')
        started = clock()
        if not finite(started):
            raise ValueError('Cleanup clock must return a finite monotonic value')
        last_clock = started
        def read_clock():
            nonlocal last_clock
            now = clock()
            if not finite(now) or now < last_clock:
                raise ValueError('Cleanup clock returned a nonfinite or decreasing value')
            last_clock = now
            return now
        if self.job is None:
            if self.cleanup and self.cleanup.get('status') == 'incomplete':
                raise OwnedCleanupIncomplete(self.cleanup)
            return self.cleanup
        job = self.job
        last_active = None
        closed = False
        reason = None
        errors = []
        first_error = None
        operation = 'query-active-processes'
        try:
            count = self.api.active_processes(job)
            if type(count) is not int or not 0 <= count <= 0xffffffff:
                raise ValueError('Win32 returned an invalid active-process count')
            last_active = count
            if last_active:
                operation = 'verify-owned-job-handle'
                if self.job != job:
                    raise ValueError('Held owned Job changed during cleanup')
                operation = 'terminate-owned-job'
                self.api.terminate_job(job)
                self.forced_tree_termination = True
                while last_active:
                    operation = 'read-cleanup-clock'
                    now = read_clock()
                    if now >= deadline:
                        reason = 'deadline-exhausted'
                        break
                    operation = 'verify-owned-job-handle'
                    if self.job != job:
                        raise ValueError('Held owned Job changed during cleanup')
                    operation = 'query-active-processes'
                    count = self.api.active_processes(job)
                    if type(count) is not int or not 0 <= count <= 0xffffffff:
                        raise ValueError('Win32 returned an invalid active-process count')
                    last_active = count
                    if last_active:
                        operation = 'read-cleanup-clock'
                        now = read_clock()
                        remaining = deadline - now
                        if remaining <= 0:
                            reason = 'deadline-exhausted'
                            break
                        operation = 'poll-owned-job'
                        time.sleep(min(0.05, remaining))
        except Exception as error:
            first_error = error
            reason = operation + '-failed'
            errors.append(dict(operation=operation, type=type(error).__name__, message=str(error)))
        finally:
            if self.job != job:
                reason = reason or 'owned-job-handle-changed'
                errors.append(dict(operation='close-owned-job', type='ValueError',
                                   message='Held owned Job changed; refusing an unowned/reused handle'))
            else:
                try:
                    self.api.close(job)
                    self.job = None
                    closed = True
                except Exception as error:
                    first_error = first_error or error
                    reason = reason or 'close-owned-job-failed'
                    errors.append(dict(operation='close-owned-job', type=type(error).__name__, message=str(error)))
        if last_active == 0 and closed and not errors:
            self.cleanup = dict(activeProcessesAfter=0, jobClosed=True,
                                forcedTreeTermination=self.forced_tree_termination)
            return self.cleanup
        self.cleanup = dict(status='incomplete', reason=reason or 'process-exit-unconfirmed',
                            lastActiveProcesses=last_active,
                            activeProcessesAfter=0 if last_active == 0 else None,
                            lastActiveCountObservedBeforeHandleClose=last_active is not None,
                            jobClosed=closed, forcedTreeTermination=self.forced_tree_termination,
                            killOnCloseRequested=closed, deadlineMonotonic=deadline, errors=errors)
        raise OwnedCleanupIncomplete(self.cleanup) from first_error


OS_RSS = dict(collector='Win32 GetProcessMemoryInfo', field='PeakWorkingSetSize',
              reportedUnit='bytes', multiplierToBytes=1, scope='owned JVM process lifetime')


def validate_execution_record(record, *, command, cwd, log_sha256, peak_record=None):
    """Pure verification of actual receipts; never manufactures a Win32 result."""
    import datetime
    identity = record.get('windowsProcess', {})
    peak = record.get('peakMemory', {})
    if (record.get('command') != list(command) or record.get('cwd') != str(cwd)
            or record.get('status') != 'passed' or record.get('returncode') != 0
            or record.get('pidRole') != 'owned JVM process' or not record.get('outputComplete')
            or record.get('logSha256') != log_sha256 or identity.get('pid') != record.get('pid')
            or type(record.get('pid')) is not int or record['pid'] <= 0
            or type(identity.get('pid')) is not int or identity['pid'] <= 0
            or type(identity.get('creationTime100ns')) is not int or identity['creationTime100ns'] <= 0
            or not identity.get('imagePath') or record.get('ownership') != dict(
                kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True)
            or record.get('cleanup') != dict(activeProcessesAfter=0, jobClosed=True,
                                             forcedTreeTermination=False)
            or peak.get('windowsProcess') != identity or peak.get('collector') != OS_RSS['collector']
            or peak.get('field') != OS_RSS['field'] or peak.get('reportedUnit') != 'bytes'
            or peak.get('queryPhase') != 'after-process-exit-before-handle-close'
            or peak.get('returncode') != 0 or type(peak.get('peakWorkingSetSizeBytes')) is not int
            or peak['peakWorkingSetSizeBytes'] <= 0 or (peak_record is not None and peak != peak_record)):
        raise ValueError('Windows execution or final kernel peak receipt does not match actual inputs')
    if (not command or not ntpath.isabs(str(command[0]))
            or ntpath.normcase(ntpath.normpath(identity['imagePath'])) !=
               ntpath.normcase(ntpath.normpath(str(command[0])))):
        raise ValueError('Observed Windows process image does not match the requested executable')
    started = datetime.datetime.fromisoformat(record['startedAtUtc'])
    completed = datetime.datetime.fromisoformat(record['completedAtUtc'])
    if (started.tzinfo is None or completed.tzinfo is None or completed <= started
            or record.get('elapsedSeconds', 0) <= 0):
        raise ValueError('Windows execution timestamps are invalid')
    return dict(windowsProcess=identity, peakWorkingSetSizeBytes=peak['peakWorkingSetSizeBytes'],
                returncode=0, cleanup=record['cleanup'])


def peak_record_bytes(raw):
    peak = json.loads(raw)
    identity = peak.get('windowsProcess', {})
    if (peak.get('collector') != OS_RSS['collector'] or peak.get('field') != OS_RSS['field']
            or peak.get('reportedUnit') != 'bytes' or peak.get('returncode') != 0
            or peak.get('queryPhase') != 'after-process-exit-before-handle-close'
            or type(peak.get('peakWorkingSetSizeBytes')) is not int
            or peak['peakWorkingSetSizeBytes'] <= 0 or identity.get('pid', 0) <= 0
            or identity.get('creationTime100ns', 0) <= 0 or not identity.get('imagePath')):
        raise ValueError('A successful actual final Windows kernel peak receipt is required')
    return peak['peakWorkingSetSizeBytes']


def run_diagnostic(command, log, rss, evidence_path, *, cwd, timeout=600):
    """Actual Windows JVM execution; kernel failure stays a failed retained receipt."""
    from command_evidence import run_with_evidence
    import hashlib
    record_path = run_with_evidence(command, cwd=cwd, directory=Path(log).parent,
                                    record_path=evidence_path, log_path=log, timeout=timeout,
                                    env=dict(os.environ, LC_ALL='C'), windows_peak=True)
    record = json.loads(record_path.read_text(encoding='utf-8'))
    digest = hashlib.sha256(Path(log).read_bytes()).hexdigest()
    validate_execution_record(record, command=command, cwd=cwd, log_sha256=digest)
    record['environmentOverrides'] = {'LC_ALL': 'C'}
    record_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8', newline='\n')
    Path(rss).write_text(json.dumps(record['peakMemory'], indent=2) + '\n', encoding='utf-8', newline='\n')
    return record
