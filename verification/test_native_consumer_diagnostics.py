"""Portable ownership, launch, quota and scheduling controls, never Win32 proof."""
import hashlib
import ast
import io
import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest import mock

import native_consumer_diagnostics as diag
import windows_dump_quota as quota
import command_evidence as commands


class FakeOwned:
    def __init__(self, members):
        self.members = members
        self.requests = []

    def member_identities(self):
        return list(self.members)

    def member_identity(self, pid, expected=None):
        values = [x for x in self.members if x['pid'] == pid]
        if not values: raise ProcessLookupError(pid)
        row = values[0]
        if expected is not None and not diag.same_identity(row, expected): raise ValueError('reused PID')
        self.requests.append((pid, expected))
        return dict(row)


class NativeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-observer-control-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.consumer = self.root / 'consumer'; self.consumer.mkdir()
        self.binary = self.consumer / 'build/bin/mingwX64/debugTest/test.exe'
        self.binary.parent.mkdir(parents=True); self.binary.write_bytes(b'fixture-not-executable')
        self.evidence = self.root / 'evidence'; self.evidence.mkdir()
        self.directory = self.evidence / 'retention/native'; self.directory.mkdir(parents=True)
        self.java = self.root / 'jdk/bin/java.exe'; self.java.parent.mkdir(parents=True); self.java.write_bytes(b'fake-java')
        self.clock = [0.0]
        self.context = dict(consumerRoot=str(self.consumer), evidenceRoot=str(self.evidence), budgetBytes=diag.TOTAL_CEILING,
                            completionDeadlineMonotonic=3585, sourceBindings=[dict(path='src/Test.kt',sha256='a'*64)])
        diag.write_record(self.directory/'context.json', self.context)
        self.token = dict(status='passed', sourceSha256=diag.sha(quota.__file__), actualWindowsOwnedChildSelfCheck=True)
        diag.write_record(self.directory/'callback-quota-ready.json', self.token)
        self.owner = dict(pid=101, creationTime100ns=11, imagePath=str(self.java))
        diag.write_record(self.directory/'gradle-owner.json', dict(gradlePid=101, javaExecutable=str(self.java),
                          javaExecutableSha256=diag.sha(self.java), workingDirectory=str(self.consumer),
                          actualConfigurationProcessIdentity=self.owner))
        self.native = dict(pid=102, creationTime100ns=22, imagePath=str(self.binary))
        self.fake = FakeOwned([self.owner, self.native])
        self.patches = [mock.patch.object(diag, 'private_directory'), mock.patch.object(diag, 'private_file'),
                        mock.patch.object(quota, 'require_self_check', side_effect=lambda value:value)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def launch(self, **changes):
        value = dict(executable=str(self.binary), task='mingwX64Test',gradlePid=101,workingDirectory=str(self.consumer),args=[],includePatterns=[],excludePatterns=[])
        value.update(changes);diag.write_launch(self.directory,value)

    def observer(self):
        return diag.NativeConsumerWatchdog(self.fake,self.directory,budget=diag.TOTAL_CEILING,deadline=3585,
                    functional_deadline=3540,callback_self_check=self.token,clock=lambda:self.clock[0])

    def test_explicit_budget_and_total_ceiling(self):
        for value in [None,True,1,diag.TOTAL_CEILING+1]:
            with self.subTest(value=value),self.assertRaises(ValueError):diag.valid_budget(value)
        self.assertEqual(diag.valid_budget(diag.TOTAL_CEILING),536870912)

    def test_critical_executable_is_linked_to_canonical_bin_once(self):
        self.launch();value=diag.read_record(self.directory/'launch-intent.json')
        path=Path(value['retainedExecutable']);self.assertEqual(path,self.evidence/'bin/mingwX64/debugTest/test.exe')
        self.assertEqual(path.stat().st_ino,self.binary.stat().st_ino)
        self.assertEqual(value['additionalPayloadAllocationBytes'],0)
        self.assertGreater(value['conservativeChargedBytes'],self.binary.stat().st_size)
        self.assertFalse((self.directory/'consumer-native.exe').exists())

    def test_missing_selfcheck_cannot_launch(self):
        (self.directory/'callback-quota-ready.json').unlink()
        with self.assertRaises(FileNotFoundError):self.launch()

    def test_stale_selfcheck_source_cannot_launch(self):
        p=self.directory/'callback-quota-ready.json';p.unlink();diag.write_record(p,{**self.token,'sourceSha256':'0'*64})
        with self.assertRaises(ValueError):self.launch()

    def test_executable_outside_own_build_bin_refused(self):
        outside=self.root/'outside.exe';outside.write_bytes(b'x')
        with self.assertRaises(ValueError):self.launch(executable=str(outside))

    def test_budget_charge_refuses_before_payload_link(self):
        with mock.patch.object(diag,'CAPTURE_RESERVE',diag.TOTAL_CEILING):
            with self.assertRaises(ValueError):self.launch()
        self.assertFalse((self.evidence/'bin').exists())

    def test_record_no_overwrite_and_byte_cap_before_write(self):
        p=self.directory/'one.json';diag.write_record(p,{'x':1})
        with self.assertRaises(ValueError):diag.write_record(p,{'x':2})
        large=self.directory/'large.json'
        with self.assertRaises(ValueError):diag.write_record(large,{'x':'x'*(diag.MAX_RECORD+1)})
        self.assertFalse(large.exists())

    def test_owner_pid_reuse_refused_before_any_capture(self):
        self.fake.members[0]={**self.owner,'creationTime100ns':999}
        watch=self.observer()
        with mock.patch.object(diag.native,'_start_capture') as capture:
            self.clock[0]=60;watch.poll();self.assertFalse(capture.called)
        self.assertIn('reused PID',watch.events[-1]['message'])

    def test_owner_java_sha_change_refused(self):
        self.java.write_bytes(b'replaced')
        watch=self.observer();self.clock[0]=60;watch.poll()
        self.assertTrue(any(e['kind']=='target-refused' for e in watch.events))

    def test_compilation_fallback_reserves_native_round(self):
        self.fake.members=[self.owner];watch=self.observer()
        session=mock.Mock();session.poll.return_value=None
        with mock.patch.object(diag.native,'_start_capture',return_value=session) as capture, mock.patch.object(watch,'_gradle_wait_text'):
            self.clock[0]=60;watch.poll();self.assertEqual(capture.call_count,1)
            self.clock[0]=120;watch.poll();self.assertEqual(capture.call_count,1)
            self.launch();self.fake.members.append(self.native)
            self.clock[0]=180;watch.poll();self.assertEqual(capture.call_count,1)
            self.clock[0]=240;watch.poll();self.assertEqual(capture.call_count,2)
        self.assertEqual(watch.rounds,2)

    def test_native_launch_and_discovery_start_own_stall_interval(self):
        self.launch();watch=self.observer()
        with mock.patch.object(diag.native,'_start_capture') as capture:
            self.clock[0]=200;watch.poll();self.assertFalse(capture.called)
            self.clock[0]=259;watch.poll();self.assertFalse(capture.called)
            self.clock[0]=260;watch.poll();self.assertTrue(capture.called)
            self.assertFalse(capture.call_args.kwargs['require_gdal'])
            self.assertFalse(capture.call_args.kwargs['copy_native'])
            self.assertEqual(capture.call_args.kwargs['limit'],20)

    def test_existing_test_progress_defers_capture(self):
        self.launch();watch=self.observer();watch.poll()
        with mock.patch.object(diag.native,'_start_capture') as capture:
            self.clock[0]=59;watch.observe(b'consumer.Test.method[mingwX64] STARTED\n')
            self.clock[0]=60;watch.poll();self.assertFalse(capture.called)
        self.assertEqual(watch.events[-1]['kind'],'existing-test-progress')

    def test_insufficient_completion_reserve_refuses_capture(self):
        self.launch();watch=self.observer();watch.poll();watch.deadline=70
        with mock.patch.object(diag.native,'_start_capture') as capture:
            self.clock[0]=60;watch.poll();self.assertFalse(capture.called)
        self.assertTrue(any(e['kind']=='capture-refused' for e in watch.events))

    def test_progress_journal_aggregate_refuses_before_growth(self):
        watch=self.observer();before=watch.events_path.stat().st_size
        with mock.patch.object(diag,'MAX_EVENT_BYTES',10):
            with self.assertRaises(ValueError):watch.observe(b'C > method STARTED\n')
        self.assertEqual(watch.events_path.stat().st_size,before)

    def test_direct_metadata_file_count_cap(self):
        for i in range(diag.MAX_DIRECT_RECORDS-len(list(self.directory.glob('*.json')))):
            diag.write_record(self.directory/f'fill{i}.json',{})
        p=self.directory/'overflow.json'
        with self.assertRaises(ValueError):diag.write_record(p,{})
        self.assertFalse(p.exists())

    def test_gradle_wait_cannot_extend_functional_timeout(self):
        watch=self.observer();self.java.with_name('jcmd.exe').write_bytes(b'fake-tool')
        watch.functional_deadline=10
        with self.assertRaises(ValueError):watch._gradle_wait_text(self.owner,self.directory)

    def test_configuration_owner_rejects_other_parent(self):
        with self.assertRaises(ValueError):diag.write_owner(self.directory,dict(gradlePid=999,javaExecutable=str(self.java),workingDirectory=str(self.consumer)))

    def test_completion_summary_does_not_duplicate_collector_bodies(self):
        self.launch();watch=self.observer();watch.poll();value=watch.close()
        self.assertTrue(value['launchIdentityRetained'])
        self.assertIn('eventsSha256',value);self.assertNotIn('events',value)

    def test_jcmd_holds_exact_target_handle_across_attach(self):
        watch=self.observer();self.java.with_name('jcmd.exe').write_bytes(b'fake-tool')
        calls=[]
        api=mock.Mock();api.open_process.side_effect=lambda pid:(calls.append(('open',pid)) or 123)
        api.close.side_effect=lambda handle:calls.append(('close',handle))
        self.fake.api=api;self.fake.job=456
        with mock.patch.object(diag.native,'_verify_inherited',side_effect=lambda *a:calls.append(('verify',a[1]))), mock.patch.object(watch,'_run_gradle_wait_text',side_effect=lambda *a:calls.append(('attach',a[-1]))):
            watch._gradle_wait_text(self.owner,self.directory)
        self.assertEqual(calls,[('open',101),('verify',123),('attach',123),('close',123)])

    def test_native_startup_failure_keeps_primary_and_deadline_cleanup(self):
        primary=ValueError('observer-init-primary')
        process=mock.Mock();process.stdout=io.BytesIO()
        process.wait.side_effect=subprocess.TimeoutExpired(['fake'],1)
        owned=mock.Mock();owned.process=process;owned.identity={'pid':5};owned.close_before.side_effect=RuntimeError('cleanup-secondary')
        directory=self.root/'commands'
        with mock.patch.object(commands,'_windows_host',return_value=True), mock.patch('windows_process.OwnedProcess',return_value=owned), mock.patch.object(diag,'NativeConsumerWatchdog',side_effect=primary):
            with self.assertRaises(ValueError) as seen:
                commands.run_with_evidence(['fake'],cwd=self.root,directory=directory,timeout=0.01,
                    windows_native_diagnostics=self.directory,native_diagnostic_budget=diag.TOTAL_CEILING,
                    completion_deadline=time.monotonic()+20,native_callback_self_check=self.token)
        self.assertIs(seen.exception,primary);self.assertFalse(owned.close.called)
        value=json.loads((directory/'001.json').read_text());self.assertEqual(value['status'],'failed')
        self.assertEqual(len(value['startupCleanupFailures']),2)
        self.assertLessEqual(process.wait.call_args.kwargs['timeout'],16)

    def test_repeated_native_observe_errors_are_bounded(self):
        class Stream:
            def __init__(self):self.left=40
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def close(self):pass
            def read1(self,n):
                if not self.left:return b''
                self.left-=1;return b'fixture progress\n'
        process=mock.Mock();process.stdout=Stream();process.pid=7;process.returncode=0;process.poll.return_value=0
        process.wait.side_effect=lambda **kw:time.sleep(0.05)
        owned=mock.Mock();owned.process=process;owned.identity={'pid':7};owned.close_before.return_value={'activeProcessesAfter':0}
        watcher=mock.Mock();watcher.observe.side_effect=ValueError('e'*10000);watcher.launch={};watcher.close.return_value={}
        directory=self.root/'errors'
        with mock.patch.object(commands,'_windows_host',return_value=True),mock.patch('windows_process.OwnedProcess',return_value=owned),mock.patch.object(diag,'NativeConsumerWatchdog',return_value=watcher),mock.patch('sys.stdout.buffer.write'),mock.patch('sys.stdout.buffer.flush'):
            with self.assertRaises(RuntimeError):commands.run_with_evidence(['fake'],cwd=self.root,directory=directory,timeout=0.01,
                windows_native_diagnostics=self.directory,native_diagnostic_budget=diag.TOTAL_CEILING,completion_deadline=time.monotonic()+20)
        value=json.loads((directory/'001.json').read_text());self.assertEqual(len(value['nativeConsumerDiagnosticErrors']),16)
        self.assertEqual(value['nativeConsumerDiagnosticErrorsOmitted'],24)
        self.assertTrue(all(len(x['message'])<=1024 for x in value['nativeConsumerDiagnosticErrors']))
        self.assertEqual(value['status'],'failed')

    def test_ci_forwards_budget_without_changing_existing_timeout(self):
        source=Path(__file__).with_name('ci.py').read_text();tree=ast.parse(source)
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='consumer')
        calls=[];namespace=dict(COMMAND_TIMEOUT=3600,WINDOWS_NATIVE_DIAGNOSTICS=True,NATIVE_DIAGNOSTIC_BUDGET=diag.TOTAL_CEILING,
                               ROOT=self.root,REPO=self.root/'maven',sys=mock.Mock(executable='python'),time=mock.Mock(monotonic=lambda:0),run=lambda *args:calls.append(args))
        exec(compile(ast.Module(body=[function],type_ignores=[]),'source-ci-consumer','exec'),namespace)
        namespace['consumer']('mingwX64');args=list(calls[0])
        self.assertEqual(args[args.index('--command-timeout-seconds')+1],3540)
        self.assertEqual(args[args.index('--completion-deadline-monotonic')+1],3585)
        self.assertEqual(args[args.index('--native-diagnostic-budget-bytes')+1],536870912)
        self.assertIn('--windows-native-consumer-diagnostics',args)


if __name__=='__main__':unittest.main()
