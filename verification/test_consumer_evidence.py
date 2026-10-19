"""Failure evidence must survive the unrelated consumer's temporary directory."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from consumer_evidence import preserve_consumer_evidence


class ConsumerEvidenceTest(unittest.TestCase):
    def test_failed_process_results_survive_temporary_consumer_removal(self):
        with tempfile.TemporaryDirectory() as retained:
            destination = Path(retained) / 'evidence'
            with tempfile.TemporaryDirectory() as temporary:
                consumer = Path(temporary)
                xml = consumer / 'build/test-results/jvmTest/TEST-consumer.xml'
                xml.parent.mkdir(parents=True)
                contents = b'<testsuite tests="1" failures="1"><testcase name="fixture"><failure>actual failure</failure></testcase></testsuite>'
                xml.write_bytes(contents)
                binary = xml.parent / 'binary/output.bin'
                binary.parent.mkdir()
                binary.write_bytes(b'partial Gradle test output')
                (consumer / 'build/jvm-runtime-files.txt').write_text('selected-classifier.jar\n')
                try:
                    subprocess.run([sys.executable, '-c', 'raise SystemExit(17)'], check=True)
                except subprocess.CalledProcessError as failure:
                    preserve_consumer_evidence(consumer, destination, failure)
            self.assertEqual((destination / 'test-results/jvmTest/TEST-consumer.xml').read_bytes(), contents)
            self.assertEqual((destination / 'test-results/jvmTest/binary/output.bin').read_bytes(), b'partial Gradle test output')
            self.assertEqual((destination / 'jvm-runtime-files.txt').read_text(), 'selected-classifier.jar\n')
            record = json.loads((destination / 'failure.json').read_text())
            self.assertEqual((record['result'], record['returncode']), ('failed', 17))
            self.assertFalse((destination / 'result.json').exists())

    def test_failure_without_xml_keeps_build_and_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            consumer = root / 'consumer'
            consumer.mkdir()
            (consumer / 'build.gradle.kts').write_text('diagnostic consumer build\n')
            preserve_consumer_evidence(consumer, root / 'evidence', RuntimeError('before XML'))
            self.assertEqual((root / 'evidence/build.gradle.kts').read_text(), 'diagnostic consumer build\n')
            self.assertEqual(json.loads((root / 'evidence/failure.json').read_text())['exception'], 'RuntimeError')
            self.assertFalse((root / 'evidence/result.json').exists())

    def test_preservation_refuses_destination_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'consumer/build').mkdir(parents=True)
            (root / 'consumer/build/jvm-runtime-files.txt').write_text('new evidence')
            (root / 'evidence').mkdir()
            protected = root / 'protected.txt'
            protected.write_text('preserve me')
            (root / 'evidence/jvm-runtime-files.txt').symlink_to(protected)
            with self.assertRaisesRegex(RuntimeError, 'symlinked consumer evidence'):
                preserve_consumer_evidence(root / 'consumer', root / 'evidence')
            self.assertEqual(protected.read_text(), 'preserve me')

    def test_failure_cannot_leave_a_previous_success_as_current_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'consumer').mkdir()
            (root / 'evidence').mkdir()
            previous = root / 'evidence/result.json'
            previous.write_text('{"result":"passed"}\n')
            preserve_consumer_evidence(root / 'consumer', root / 'evidence', RuntimeError('later invocation'))
            self.assertFalse(previous.exists())
            self.assertEqual(json.loads((root / 'evidence/result-before-failure-0.json').read_text())['result'], 'passed')
            self.assertEqual(json.loads((root / 'evidence/failure.json').read_text())['result'], 'failed')


if __name__ == '__main__':
    unittest.main()
