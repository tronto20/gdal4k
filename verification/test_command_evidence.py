"""Exercise real child output, failure, and process-tree timeout preservation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from command_evidence import run_with_evidence
from consumer_evidence import preserve_consumer_evidence


class CommandEvidenceTest(unittest.TestCase):
    def run_child(self, root, source, timeout=10):
        run_with_evidence([sys.executable, '-c', source], cwd=root,
                          directory=root / 'evidence', timeout=timeout)

    def record(self, root, number=1):
        return json.loads((root / 'evidence' / f'{number:03d}.json').read_text())

    def test_output_retained_and_later_command_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.run_child(root, 'import os; os.write(1, b"stdout\\n"); os.write(2, b"stderr\\xff\\n")')
            self.run_child(root, 'print("second")')
            self.assertEqual((root / 'evidence/001.log').read_bytes(), b'stdout\nstderr\xff\n')
            self.assertEqual((root / 'evidence/002.log').read_bytes().replace(b'\r\n', b'\n'), b'second\n')
            for number in [1, 2]:
                record = self.record(root, number)
                self.assertEqual((record['status'], record['returncode']), ('passed', 0))
                self.assertTrue(record['outputComplete'])

    def test_nonzero_exit_keeps_output_and_cannot_be_reported_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.run_child(root, 'print("before failure", flush=True); raise SystemExit(17)')
            self.assertEqual(caught.exception.returncode, 17)
            record = self.record(root)
            self.assertEqual((record['status'], record['returncode']), ('failed', 17))
            self.assertTrue(record['outputComplete'])
            self.assertIn(b'before failure', (root / 'evidence/001.log').read_bytes())

    def test_child_environment_is_isolated_and_not_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = 'GDAL4K_CHILD_ENV_TEST'
            original = os.environ.get(marker)
            env = dict(os.environ, **{marker: 'private-unit-value'})
            receipt = run_with_evidence([sys.executable, '-c',
                                        'import os; assert len(os.environ["GDAL4K_CHILD_ENV_TEST"]) == 18; print("child-env-ok")'],
                                        cwd=root, directory=root/'evidence', timeout=10, env=env)
            self.assertEqual(original, os.environ.get(marker))
            self.assertEqual(root/'evidence/001.json', receipt)
            self.assertNotIn('private-unit-value', receipt.read_text())
            self.assertNotIn('environment', json.loads(receipt.read_text()))

    def test_timeout_terminates_owned_descendant_and_retains_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The descendant inherits stdout. Killing only the parent leaves the
            # output pipe open for 120s and cannot produce outputComplete=true.
            source = ('import subprocess, sys, time; '
                      'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
                      'print("descendant started", child.pid, flush=True); time.sleep(120)')
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child(root, source, timeout=3)
            record = self.record(root)
            self.assertEqual(record['status'], 'timed_out')
            self.assertIsNotNone(record['returncode'])
            self.assertTrue(record['outputComplete'])
            self.assertLess(record['elapsedSeconds'], 10)
            self.assertIn(b'descendant started', (root / 'evidence/001.log').read_bytes())
            if os.name == 'nt':
                self.assertEqual(record['cleanupReturncode'], 0)

    def test_consumer_timeout_preserves_partial_xml_before_temporary_directory_removal(self):
        with tempfile.TemporaryDirectory() as retained:
            destination = Path(retained) / 'consumer-evidence'
            with tempfile.TemporaryDirectory() as temporary:
                consumer = Path(temporary)
                xml = consumer / 'build/test-results/nativeTest/TEST-stalled.xml'
                xml.parent.mkdir(parents=True)
                contents = b'<testsuite tests="1"><testcase name="finished-before-stall"/></testsuite>'
                xml.write_bytes(contents)
                with self.assertRaises(subprocess.TimeoutExpired):
                    try:
                        run_with_evidence([sys.executable, '-c', 'print("before stall", flush=True); import time; time.sleep(120)'],
                                          cwd=consumer, directory=destination/'commands', timeout=1)
                    finally:
                        preserve_consumer_evidence(consumer, destination, sys.exc_info()[1])
            self.assertEqual((destination/'test-results/nativeTest/TEST-stalled.xml').read_bytes(), contents)
            self.assertEqual(json.loads((destination/'failure.json').read_text())['exception'], 'TimeoutExpired')
            self.assertEqual(json.loads((destination/'commands/001.json').read_text())['status'], 'timed_out')
            self.assertIn(b'before stall', (destination/'commands/001.log').read_bytes())


if __name__ == '__main__':
    unittest.main()
