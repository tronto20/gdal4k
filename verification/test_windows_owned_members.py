"""Portable bounded Win32-boundary checks and an explicit Windows-only smoke.

Mocked tests do not establish actual Windows process or kernel acceptance.
The kernel test, where available, launches only its own small Python process.
"""
import copy
import ctypes as C
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import windows_process as windows


def identity(pid, created=1000):
    return dict(pid=pid, creationTime100ns=created, imagePath=rf'C:\owned\process-{pid}.exe')


class JobListKernel:
    def __init__(self, rows):
        self.rows = list(rows)
        self.last_error = 0
        self.calls = []

    def query(self, job, kind, buffer, size, returned):
        self.calls.append(dict(job=job, kind=kind, size=size))
        row = self.rows.pop(0)
        assigned, count = row['assigned'], row['count']
        struct.pack_into('<II', buffer, 0, assigned, count)
        for index, pid in enumerate(row.get('ids', [])):
            (windows.SIZE_T * 1).from_buffer(buffer, 8 + index * C.sizeof(windows.SIZE_T))[0] = pid
        C.cast(returned, C.POINTER(windows.DWORD)).contents.value = row.get('returned', 8 + count * C.sizeof(windows.SIZE_T))
        self.last_error = row.get('error', 0)
        return not self.last_error


def query_api(rows):
    kernel = JobListKernel(rows)
    api = object.__new__(windows.Win32)
    api.k = SimpleNamespace(QueryInformationJobObject=kernel.query)
    return api, kernel


class JobPidListTests(unittest.TestCase):
    def call(self, rows, bound=4096):
        api, kernel = query_api(rows)
        with patch.object(windows.C, 'get_last_error', side_effect=lambda: kernel.last_error, create=True), \
                patch.object(windows.C, 'WinError', side_effect=lambda n: OSError(n, 'mock Win32 error'), create=True):
            return api.job_process_ids(77, bound), kernel

    def test_pointer_sized_ids_and_complete_empty_list(self):
        ids, kernel = self.call([dict(assigned=3, count=3, ids=[1, 123, 0xfffffffe])])
        self.assertEqual([1, 123, 0xfffffffe], ids)
        self.assertEqual(dict(job=77, kind=3, size=8 + 16 * C.sizeof(windows.SIZE_T)), kernel.calls[0])
        self.assertEqual([], self.call([dict(assigned=0, count=0)])[0])

    def test_error_more_data_retries_full_list_and_rejects_partial_success(self):
        ids, kernel = self.call([dict(assigned=20, count=16, ids=list(range(1, 17)), error=234),
                                 dict(assigned=20, count=20, ids=list(range(1, 21)))])
        self.assertEqual(list(range(1, 21)), ids)
        self.assertEqual(8 + 32 * C.sizeof(windows.SIZE_T), kernel.calls[1]['size'])
        ids, kernel = self.call([dict(assigned=2, count=1, ids=[1]), dict(assigned=2, count=2, ids=[1, 2])])
        self.assertEqual([1, 2], ids)
        self.assertEqual(2, len(kernel.calls))

    def test_invalid_bound_cannot_allocate_or_query(self):
        for value in [0, -1, True, 4097, 1.5]:
            api, kernel = query_api([])
            with self.subTest(value=value), self.assertRaises(ValueError): api.job_process_ids(77, value)
            self.assertFalse(kernel.calls)

    def test_declared_count_over_limit_and_impossible_count_refused(self):
        for row in [dict(assigned=4097, count=0), dict(assigned=1, count=2), dict(assigned=20, count=17)]:
            with self.subTest(row=row), self.assertRaises(ValueError): self.call([row])

    def test_duplicate_zero_or_non_dword_pid_refused(self):
        bad = [[1, 1], [0, 1]]
        if C.sizeof(windows.SIZE_T) > 4: bad.append([1, 0x100000000])
        for ids in bad:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.call([dict(assigned=len(ids), count=len(ids), ids=ids)])

    def test_bad_success_length_and_non_more_data_error_refused(self):
        for returned in [7, 8, 100000]:
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                self.call([dict(assigned=1, count=1, ids=[1], returned=returned)])
        with self.assertRaises(OSError) as caught:
            self.call([dict(assigned=0, count=0, error=5)])
        self.assertEqual(5, caught.exception.errno)

    def test_list_growth_at_cap_never_silently_returns_partial(self):
        with self.assertRaises(ValueError):
            self.call([dict(assigned=2, count=1, ids=[1], error=234)], bound=2)
        with self.assertRaises(ValueError):
            self.call([dict(assigned=4096, count=0, error=234), dict(assigned=4096, count=0, error=234)])

    def test_actual_membership_boolean_is_read_from_held_handles(self):
        api = object.__new__(windows.Win32)
        def check(handle, job, present):
            self.assertEqual((123, 456), (handle, job))
            C.cast(present, C.POINTER(C.c_int)).contents.value = 1
            return 1
        api.k = SimpleNamespace(IsProcessInJob=check)
        self.assertTrue(api.in_job(123, 456))

    def test_held_handle_api_errors_are_not_assumed_nonmembership_or_exit(self):
        api = object.__new__(windows.Win32)
        api.k = SimpleNamespace(IsProcessInJob=lambda *args: 0, GetProcessId=lambda handle: 0)
        with patch.object(windows.C, 'get_last_error', return_value=5, create=True), \
                patch.object(windows.C, 'WinError', side_effect=lambda n: OSError(n, 'mock denied'), create=True):
            with self.assertRaises(OSError): api.in_job(123, 456)
            with self.assertRaises(OSError): api.process_id(123)


class OwnedMemberTests(unittest.TestCase):
    def owned(self):
        root = identity(100)
        api = MagicMock()
        api.exited.return_value = False
        api.in_job.return_value = True
        api.job_process_ids.return_value = [101, 102]
        api.open_process.side_effect = lambda pid: pid
        api.process_id.side_effect = lambda handle: handle
        api.identity_from_handle.side_effect = lambda handle, pid: dict(root) if handle == 100 else identity(pid)
        owned = object.__new__(windows.OwnedProcess)
        owned.api, owned.job = api, 77
        owned.identity = root
        owned.process = SimpleNamespace(pid=100, _handle=100, poll=lambda: None)
        return owned, api

    def test_members_use_held_handles_and_every_handle_is_closed(self):
        owned, api = self.owned()
        self.assertEqual([identity(101), identity(102)], owned.member_identities())
        self.assertEqual([(101,), (102,)], [call.args for call in api.open_process.call_args_list])
        self.assertEqual([(101,), (102,)], [call.args for call in api.close.call_args_list])
        self.assertTrue(all(call.args[0] in [100, 101, 102] and call.args[1] == 77 for call in api.in_job.call_args_list))

    def test_requested_pid_must_be_enumerated_before_open(self):
        owned, api = self.owned()
        with self.assertRaises(ValueError): owned.member_identity(999)
        api.open_process.assert_not_called()
        for bad in [True, 0, -1, 0x100000000]:
            with self.subTest(bad=bad), self.assertRaises(ValueError): owned.member_identity(bad)

    def test_foreign_reused_pid_refused_before_image_query(self):
        owned, api = self.owned()
        api.in_job.side_effect = lambda handle, job: handle == 100
        with self.assertRaises(ValueError): owned.member_identity(101)
        self.assertEqual([(100, 100)], [call.args for call in api.identity_from_handle.call_args_list])
        api.close.assert_called_once_with(101)

    def test_expected_creation_image_or_pid_change_refused(self):
        for expected in [identity(101, created=999), {**identity(101), 'imagePath': r'C:\foreign.exe'}, identity(102)]:
            owned, api = self.owned()
            with self.subTest(expected=expected), self.assertRaises(ValueError): owned.member_identity(101, expected)
            if expected['pid'] == 101: api.close.assert_called_once_with(101)
            else: api.open_process.assert_not_called()

    def test_stable_expected_identity_and_case_only_image_are_accepted(self):
        owned, api = self.owned()
        self.assertEqual(identity(101), owned.member_identity(101, {**identity(101), 'imagePath': identity(101)['imagePath'].upper()}))
        api.close.assert_called_once_with(101)

    def test_signaled_listed_process_omitted_but_live_access_error_propagates(self):
        owned, api = self.owned()
        api.exited.side_effect = lambda handle: handle == 101
        self.assertEqual([identity(102)], owned.member_identities())
        self.assertEqual([(101,), (102,)], [call.args for call in api.close.call_args_list])
        owned, api = self.owned()
        api.open_process.side_effect = PermissionError('query denied')
        with self.assertRaises(PermissionError): owned.member_identities()

    def test_raced_image_failure_is_exit_only_with_signaled_same_handle(self):
        for signaled in [False, True]:
            owned, api = self.owned()
            def image(handle, pid):
                if handle == 100: return identity(100)
                raise PermissionError('image query raced or denied')
            api.identity_from_handle.side_effect = image
            member_checks = iter([False, signaled])
            api.exited.side_effect = lambda handle: False if handle == 100 else next(member_checks)
            with self.subTest(signaled=signaled), self.assertRaises(ProcessLookupError if signaled else PermissionError):
                owned.member_identity(101)
            api.close.assert_called_once_with(101)

    def test_identity_change_on_same_held_handle_is_refused(self):
        owned, api = self.owned()
        member_ids = iter([identity(101), identity(101, created=1001)])
        api.identity_from_handle.side_effect = lambda handle, pid: identity(100) if handle == 100 else next(member_ids)
        with self.assertRaises(ValueError): owned.member_identity(101)
        api.close.assert_called_once_with(101)

    def test_root_exit_after_query_prevents_returning_a_stale_tree_observation(self):
        owned, api = self.owned()
        polls = iter([None, 0]); owned.process.poll = lambda: next(polls)
        with self.assertRaises(ValueError): owned.member_identity(101)
        api.close.assert_called_once_with(101)

    def test_membership_change_after_identity_query_is_refused(self):
        owned, api = self.owned(); checks = iter([True, False])
        api.in_job.side_effect = lambda handle, job: True if handle == 100 else next(checks)
        with self.assertRaises(ValueError): owned.member_identity(101)
        api.close.assert_called_once_with(101)

    def test_root_held_handle_pid_must_match_the_original_identity(self):
        owned, api = self.owned(); api.process_id.return_value = 999; api.process_id.side_effect = None
        with self.assertRaises(ValueError): owned.member_identities()
        api.job_process_ids.assert_not_called()

    def test_closed_exited_or_unowned_root_cannot_enumerate(self):
        for alteration in ['closed', 'exited', 'foreign', 'reused']:
            owned, api = self.owned()
            if alteration == 'closed': owned.job = None
            elif alteration == 'exited': owned.process.poll = lambda: 0
            elif alteration == 'foreign': api.in_job.return_value = False
            else: api.identity_from_handle.return_value = identity(100, created=1001); api.identity_from_handle.side_effect = None
            with self.subTest(alteration=alteration), self.assertRaises(ValueError): owned.member_identities()
            api.job_process_ids.assert_not_called()

    def test_incomplete_member_list_or_invalid_identity_fail_closed(self):
        owned, api = self.owned(); api.job_process_ids.side_effect = ValueError('partial list')
        with self.assertRaises(ValueError): owned.member_identities()
        api.open_process.assert_not_called()
        for malformed in [{**identity(101), 'pid': True}, {**identity(101), 'creationTime100ns': 0}, {**identity(101), 'imagePath': 'relative.exe'}]:
            owned, api = self.owned()
            api.identity_from_handle.side_effect = lambda handle, pid: identity(100) if handle == 100 else malformed
            with self.subTest(malformed=malformed), self.assertRaises(ValueError): owned.member_identity(101)
            api.close.assert_called_once_with(101)


class BoundedCleanupTests(unittest.TestCase):
    class Clock:
        def __init__(self, value=10.0):
            self.value = value
            self.sleeps = []
        def __call__(self):
            return self.value
        def sleep(self, amount):
            self.sleeps.append(amount)
            self.value += amount

    def owned(self, counts):
        owned = object.__new__(windows.OwnedProcess)
        owned.api = MagicMock()
        owned.api.active_processes.side_effect = counts
        owned.job, owned.cleanup, owned.forced_tree_termination = 77, None, False
        return owned

    def test_observed_zero_returns_existing_success_shape_without_termination(self):
        owned = self.owned([0]); clock = self.Clock()
        self.assertEqual(dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False),
                         owned.close_before(11, clock=clock))
        owned.api.terminate_job.assert_not_called()
        owned.api.close.assert_called_once_with(77)
        self.assertIsNone(owned.job)
        self.assertEqual(owned.cleanup, owned.close_before(0, clock=clock))

    def test_active_tree_termination_and_actual_zero_are_distinct_observations(self):
        owned = self.owned([2, 0]); clock = self.Clock()
        self.assertEqual(dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=True),
                         owned.close_before(11, clock=clock))
        owned.api.terminate_job.assert_called_once_with(77)
        self.assertEqual(2, owned.api.active_processes.call_count)
        self.assertFalse(clock.sleeps)

    def test_zero_or_past_deadline_closes_handle_without_claiming_exit(self):
        for deadline in [0, -1, 10]:
            owned = self.owned([3]); clock = self.Clock()
            with self.subTest(deadline=deadline), patch.object(windows.time, 'sleep', side_effect=clock.sleep), \
                    self.assertRaises(windows.OwnedCleanupIncomplete) as caught:
                owned.close_before(deadline, clock=clock)
            self.assertEqual(3, caught.exception.cleanup['lastActiveProcesses'])
            self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])
            self.assertTrue(caught.exception.cleanup['jobClosed'])
            self.assertTrue(caught.exception.cleanup['forcedTreeTermination'])
            self.assertEqual('deadline-exhausted', caught.exception.cleanup['reason'])
            self.assertEqual(1, owned.api.active_processes.call_count)
            owned.api.terminate_job.assert_called_once_with(77)
            owned.api.close.assert_called_once_with(77)
            self.assertFalse(clock.sleeps)
            with self.assertRaises(windows.OwnedCleanupIncomplete): owned.close_before(deadline, clock=clock)

    def test_finite_deadline_caps_poll_sleep_and_leaves_actual_last_count(self):
        owned = self.owned([4, 2]); owned.api.active_processes.side_effect = None; owned.api.active_processes.return_value = 2
        clock = self.Clock()
        with patch.object(windows.time, 'sleep', side_effect=clock.sleep), self.assertRaises(windows.OwnedCleanupIncomplete) as caught:
            owned.close_before(10.075, clock=clock)
        self.assertAlmostEqual(10.075, clock.value)
        self.assertTrue(all(0 < value <= 0.05 for value in clock.sleeps))
        self.assertEqual(2, caught.exception.cleanup['lastActiveProcesses'])
        self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])
        owned.api.close.assert_called_once_with(77)

    def test_nonfinite_deadline_or_initial_clock_refused_before_any_job_call(self):
        for deadline in [float('nan'), float('inf'), -float('inf'), True, None, '11', 10**1000]:
            owned = self.owned([1])
            with self.subTest(deadline=repr(deadline)), self.assertRaises(ValueError): owned.close_before(deadline)
            self.assertEqual([], owned.api.mock_calls)
            self.assertEqual(77, owned.job)
        owned = self.owned([1])
        with self.assertRaises(ValueError): owned.close_before(12, clock=lambda: float('nan'))
        self.assertEqual([], owned.api.mock_calls)

    def test_query_failure_still_closes_own_job_and_preserves_unknown_count(self):
        denied = PermissionError('query denied'); owned = self.owned([denied])
        with self.assertRaises(windows.OwnedCleanupIncomplete) as caught: owned.close_before(11, clock=self.Clock())
        self.assertIs(denied, caught.exception.__cause__)
        self.assertIsNone(caught.exception.cleanup['lastActiveProcesses'])
        self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])
        self.assertFalse(caught.exception.cleanup['lastActiveCountObservedBeforeHandleClose'])
        self.assertTrue(caught.exception.cleanup['jobClosed'])
        self.assertFalse(caught.exception.cleanup['forcedTreeTermination'])
        owned.api.close.assert_called_once_with(77)

    def test_close_failure_keeps_handle_and_error_instead_of_claiming_closed(self):
        denied = PermissionError('close denied'); owned = self.owned([0]); owned.api.close.side_effect = denied
        with self.assertRaises(windows.OwnedCleanupIncomplete) as caught: owned.close_before(11, clock=self.Clock())
        self.assertIs(denied, caught.exception.__cause__)
        self.assertEqual(0, caught.exception.cleanup['lastActiveProcesses'])
        self.assertFalse(caught.exception.cleanup['jobClosed'])
        self.assertEqual(77, owned.job)
        self.assertEqual('close-owned-job', caught.exception.cleanup['errors'][0]['operation'])

    def test_failed_termination_is_not_reported_as_forced_success(self):
        denied = PermissionError('terminate denied'); owned = self.owned([2]); owned.api.terminate_job.side_effect = denied
        with self.assertRaises(windows.OwnedCleanupIncomplete) as caught: owned.close_before(11, clock=self.Clock())
        self.assertFalse(caught.exception.cleanup['forcedTreeTermination'])
        self.assertEqual(2, caught.exception.cleanup['lastActiveProcesses'])
        self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])
        self.assertTrue(caught.exception.cleanup['killOnCloseRequested'])
        owned.api.close.assert_called_once_with(77)

    def test_query_error_after_termination_preserves_actual_previous_count(self):
        denied = PermissionError('later query denied'); owned = self.owned([2, denied])
        with self.assertRaises(windows.OwnedCleanupIncomplete) as caught: owned.close_before(11, clock=self.Clock())
        self.assertEqual(2, caught.exception.cleanup['lastActiveProcesses'])
        self.assertTrue(caught.exception.cleanup['forcedTreeTermination'])
        self.assertEqual('query-active-processes', caught.exception.cleanup['errors'][0]['operation'])
        self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])

    def test_concurrently_changed_job_handle_is_never_terminated_or_closed(self):
        owned = self.owned([2])
        def change_job(job):
            owned.job = 999
            return 2
        owned.api.active_processes.side_effect = change_job
        with self.assertRaises(windows.OwnedCleanupIncomplete) as caught: owned.close_before(11, clock=self.Clock())
        owned.api.terminate_job.assert_not_called()
        owned.api.close.assert_not_called()
        self.assertFalse(caught.exception.cleanup['jobClosed'])
        self.assertEqual(999, owned.job)

    def test_clock_failure_or_backwards_value_still_closes_owned_handle(self):
        for values in [[10, float('nan')], [10, 9]]:
            owned = self.owned([1]); clock = iter(values)
            with self.subTest(values=values), self.assertRaises(windows.OwnedCleanupIncomplete) as caught:
                owned.close_before(11, clock=lambda: next(clock))
            owned.api.close.assert_called_once_with(77)
            self.assertEqual('read-cleanup-clock', caught.exception.cleanup['errors'][0]['operation'])
            self.assertIsNone(caught.exception.cleanup['activeProcessesAfter'])


@unittest.skipUnless(os.name == 'nt', 'Actual Windows owned Job kernel test unavailable on this host')
class ActualWindowsOwnedMembers(unittest.TestCase):
    def test_actual_owned_child_enumerated_and_foreign_parent_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (root / 'child.log').open('wb') as log:
                owned = windows.OwnedProcess([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=root, stdout=log, stderr=subprocess.STDOUT)
                try:
                    members = owned.member_identities()
                    current = [row for row in members if row['pid'] == owned.identity['pid']]
                    self.assertEqual([owned.identity], current)
                    self.assertEqual(owned.identity, owned.member_identity(owned.identity['pid'], owned.identity))
                    with self.assertRaises(ValueError): owned.member_identity(os.getpid())
                finally:
                    owned.terminate_tree(); owned.process.wait(timeout=30); owned.close()


if __name__ == '__main__':
    unittest.main()
