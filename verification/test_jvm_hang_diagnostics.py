"""Exercise the premain diagnostics with real JDK21 worker processes, no GDAL."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import zipfile

from verification.jvm_hang_diagnostics import agent_jvm_argument, prepare_jvm_hang_diagnostics


SMOKE = '''import java.util.concurrent.CountDownLatch;
import java.util.concurrent.locks.ReentrantLock;

public class Smoke {
    static final Object MONITOR = new Object();
    static final Object DEEP = new Object();
    static final ReentrantLock SYNC = new ReentrantLock();
    static final CountDownLatch OWNER_READY = new CountDownLatch(1);
    static final CountDownLatch SYNC_READY = new CountDownLatch(1);
    static final CountDownLatch DEEP_READY = new CountDownLatch(1);
    static final CountDownLatch RELEASE = new CountDownLatch(1);
    static boolean released;
    static void await(CountDownLatch latch) {
        try { latch.await(); } catch (InterruptedException error) { throw new RuntimeException(error); }
    }
    static void deep(int depth) {
        if (depth > 0) { deep(depth - 1); return; }
        synchronized (DEEP) {
            DEEP_READY.countDown();
            while (!released) try { DEEP.wait(); } catch (InterruptedException error) { throw new RuntimeException(error); }
        }
    }
    public static void main(String[] args) throws Exception {
        Thread owner = new Thread(() -> { synchronized (MONITOR) { OWNER_READY.countDown(); await(RELEASE); } }, "diag-monitor-owner");
        owner.start(); await(OWNER_READY);
        Thread blocked = new Thread(() -> { synchronized (MONITOR) {} }, "diag-blocked");
        blocked.start();
        Thread syncOwner = new Thread(() -> { SYNC.lock(); try { SYNC_READY.countDown(); await(RELEASE); } finally { SYNC.unlock(); } }, "diag-sync-owner");
        syncOwner.start(); await(SYNC_READY);
        Thread syncWaiter = new Thread(() -> { SYNC.lock(); try {} finally { SYNC.unlock(); } }, "diag-sync-waiter");
        syncWaiter.start();
        Thread deep = new Thread(() -> deep(24), "diag-deep-waiter");
        deep.start(); await(DEEP_READY);
        Thread.sleep(1200);
        synchronized (DEEP) { released = true; DEEP.notifyAll(); }
        RELEASE.countDown();
        for (Thread thread : new Thread[] {owner, blocked, syncOwner, syncWaiter, deep}) thread.join();
        System.out.println("SMOKE_DONE");
    }
}
'''


class JvmHangDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='gdal4k hang diagnostics ')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.prepared = prepare_jvm_hang_diagnostics(cls.root, interval_seconds=0.05)
        receipt = json.loads(cls.prepared['receipt'].read_text())
        home = Path(receipt['javaHome'])
        suffix = '.exe' if os.name == 'nt' else ''
        cls.java, javac = [home / 'bin' / (name + suffix) for name in ['java', 'javac']]
        smoke = cls.root / 'Smoke.java'
        smoke.write_text(SMOKE, encoding='utf-8')
        exit_source = cls.root / 'FastExit.java'
        exit_source.write_text('public class FastExit { public static void main(String[] args) { System.out.println("FAST_EXIT"); } }\n', encoding='utf-8')
        cls.classes = cls.root / 'smoke-classes'
        cls.classes.mkdir()
        subprocess.run([str(javac), '--release', '21', '-d', str(cls.classes), str(smoke), str(exit_source)],
                       check=True, capture_output=True)

    def test_premain_manifest_default_interval_and_retained_exact_build_bytes(self):
        argument = agent_jvm_argument(self.prepared['agent_jar'], self.prepared['output_dir'])
        self.assertIn('intervalMillis=60000;', argument)
        with zipfile.ZipFile(self.prepared['agent_jar']) as jar:
            manifest = jar.read('META-INF/MANIFEST.MF').decode()
            self.assertIn('Premain-Class: dev.gdal4k.verification.JvmHangDiagnosticsAgent', manifest)
            self.assertNotIn('Agent-Class:', manifest)
            self.assertIn('Can-Redefine-Classes: false', manifest)
            self.assertIn('Can-Retransform-Classes: false', manifest)
        retained = self.prepared['retained_dir']
        bindings = json.loads((retained / 'artifact-bindings.json').read_text())
        self.assertEqual({'JvmHangDiagnosticsAgent.java', 'jvm-hang-diagnostics-agent.jar', 'build-receipt.json'}, set(bindings))
        for name, identity in bindings.items():
            data = (retained / name).read_bytes()
            self.assertEqual(identity, {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.prepared['agent_jar'].read_bytes(), (retained / 'jvm-hang-diagnostics-agent.jar').read_bytes())
        original_receipt = self.prepared['agent_jar'].parent / 'build-receipt.json'
        self.assertEqual(original_receipt.read_bytes(), self.prepared['receipt'].read_bytes())

    def test_actual_worker_records_full_stack_locks_pid_and_exits_with_daemon(self):
        process = subprocess.Popen([str(self.java), self.prepared['jvm_arg'], '-cp', str(self.classes), 'Smoke'],
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(0, process.returncode, stderr)
        self.assertIn('SMOKE_DONE', stdout)
        self.assertIn('GDAL4K_JVM_HANG_AGENT PREMAIN_START', stdout)
        self.assertIn('GDAL4K_JVM_HANG_AGENT PREMAIN_COMPLETE', stdout)
        self.assertIn('GDAL4K_JVM_HANG_AGENT SNAPSHOT_COMPLETE', stdout)
        self.assertNotIn('GDAL4K_JVM_HANG_AGENT ERROR', stdout)
        dumps = list((self.prepared['output_dir'] / ('worker-' + str(process.pid))).glob('thread-dump-*.txt'))
        self.assertGreaterEqual(len(dumps), 2)
        text = '\n'.join(path.read_text(encoding='utf-8') for path in dumps)
        self.assertIn('workerPid=' + str(process.pid), text)
        self.assertRegex(text, r'utc=\d{4}-\d\d-\d\dT[^ ]+Z')
        self.assertRegex(text, r'blockedCount=\d+ waitedCount=\d+')
        # JVM-managed native waits may report false: retain the real MXBean
        # flag rather than inferring it from a Native Method stack frame.
        self.assertTrue(re.search(r'\binNative=(?:true|false)\b', text))
        blocks = re.findall(r'THREAD [^\n]*name="diag-deep-waiter"[^\n]*\n.*?(?=\nTHREAD|\nEND_SNAPSHOT)', text, re.S)
        self.assertTrue(any(block.count('Smoke.deep(') >= 20 for block in blocks), 'Expected full stack beyond ThreadInfo.toString eight-frame truncation')
        blocked = re.findall(r'THREAD [^\n]*name="diag-blocked"[^\n]*\n.*?(?=\nTHREAD|\nEND_SNAPSHOT)', text, re.S)
        self.assertTrue(any('state=BLOCKED' in block and 'lockOwnerName=diag-monitor-owner' in block for block in blocked))
        owners = re.findall(r'THREAD [^\n]*name="diag-monitor-owner"[^\n]*\n.*?(?=\nTHREAD|\nEND_SNAPSHOT)', text, re.S)
        self.assertTrue(any('LOCKED_MONITORS count=1' in block and 'stackDepth=' in block for block in owners))
        sync = re.findall(r'THREAD [^\n]*name="diag-sync-owner"[^\n]*\n.*?(?=\nTHREAD|\nEND_SNAPSHOT)', text, re.S)
        self.assertTrue(any('LOCKED_SYNCHRONIZERS count=1' in block and 'ReentrantLock' in block for block in sync))

    def test_invalid_agent_option_logs_error_without_aborting_worker(self):
        bad = self.prepared['jvm_arg'].replace('intervalMillis=50;', 'intervalMillis=0;')
        result = subprocess.run([str(self.java), bad, '-cp', str(self.classes), 'FastExit'],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('GDAL4K_JVM_HANG_AGENT ERROR', result.stdout)
        self.assertIn('phase=premain', result.stdout)
        self.assertIn('FAST_EXIT', result.stdout)


if __name__ == '__main__':
    unittest.main()
