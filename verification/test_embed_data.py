"""Exercise resource/notice generation without GDAL or a native toolchain."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / 'gdal4k-engine/tools/embed-data.py'
VIEWSHED_LICENSE = ROOT / 'gdal4k/native/serial-viewshed/LICENSE'
JSON_STRING = r'"(?:[^"\\]|\\.)*"'


class EmbedDataTest(unittest.TestCase):
    observed_configurations = set()

    def make_prefix(self, directory, copyright_bytes=None):
        prefix = Path(directory) / 'prefix'
        resources = {
            'tms_NZTM2000.json': b'{"fixture":"NZTM2000"}\r\n',
            'nested/data.bin': bytes(range(256)) + b'\x00\xff\r\n\n\x1a',
        }
        for name, data in resources.items():
            path = prefix / 'share/gdal' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if copyright_bytes is not None:
            notice = prefix / 'share/demo/copyright'
            notice.parent.mkdir(parents=True)
            notice.write_bytes(copyright_bytes)
        return prefix, resources

    def generate(self, prefix, output, web=False):
        environment = dict(os.environ, LC_ALL='C', PYTHONUTF8='0',
                           PYTHONCOERCECLOCALE='0', PYTHONWARNDEFAULTENCODING='1',
                           PYTHONWARNINGS='error::EncodingWarning')
        # Observe the effective interpreter settings. macOS may keep UTF-8 for
        # its C locale, while Linux must really exercise a non-UTF-8 locale.
        observation = subprocess.run(
            [sys.executable, '-c',
             'import json,locale,sys; print(json.dumps(dict('
             'utf8_mode=sys.flags.utf8_mode,encoding=locale.getencoding(),'
             'warn_default_encoding=sys.flags.warn_default_encoding,'
             'platform=sys.platform),ensure_ascii=True))'],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=30)
        self.assertEqual(0, observation.returncode,
                         observation.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(b'', observation.stderr)
        configuration = json.loads(observation.stdout.decode('ascii'))
        self.assertEqual(0, configuration['utf8_mode'])
        self.assertEqual(1, configuration['warn_default_encoding'])
        self.assertEqual(sys.platform, configuration['platform'])
        if configuration['platform'].startswith('linux'):
            self.assertNotEqual('utf8', configuration['encoding'].lower().replace('-', ''))
        observed = json.dumps(configuration, sort_keys=True, ensure_ascii=True)
        if observed not in self.observed_configurations:
            print('embed-data child configuration: ' + observed, flush=True)
            self.observed_configurations.add(observed)
        command = [sys.executable, str(GENERATOR), str(prefix), str(output)]
        if web:
            command.append('--web')
        return subprocess.run(command, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=False, timeout=30)

    def assert_generated(self, result, output):
        self.assertEqual(0, result.returncode,
                         result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(b'', result.stderr)
        raw = output.read_bytes()
        self.assertNotIn(b'\r', raw, 'Generated output must use LF on every host')
        return raw.decode('utf-8')

    def notices(self, source, web=False):
        pattern = (r'gdal4k_license_notices\(\)\s*\{\s*return\s+('
                   + JSON_STRING + r');\s*\}' if web else
                   r'static const char notices\[\]\s*=\s*(' + JSON_STRING + r');')
        matches = re.findall(pattern, source)
        self.assertEqual(1, len(matches), 'Expected exactly one notice string')
        return json.loads(matches[0])

    def assert_resource_bytes(self, source, expected):
        arrays = {}
        for index, body in re.findall(
                r'static const unsigned char resource_(\d+)\[\]\s*=\s*\{([^}]*)\};',
                source, re.S):
            values = [int(value.strip()) for value in body.split(',') if value.strip()]
            arrays[index] = bytes(values)
        entries = re.findall(
            r'\{(' + JSON_STRING + r'),\s*resource_(\d+),\s*sizeof\(resource_\2\)\},',
            source)
        actual = {json.loads(name): arrays[index] for name, index in entries}
        self.assertEqual(len(entries), len(actual), 'Duplicate resource names')
        self.assertEqual(len(expected), len(arrays))
        self.assertEqual(expected, actual)

    def test_native_utf8_notice_normalizes_newlines_without_changing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            notice = '\u00a9 Example\r\n\uc601\uc0c1 license\nBare CR\rLast line\r\n'
            prefix, resources = self.make_prefix(directory, notice.encode('utf-8'))
            output = Path(directory) / 'generated/resources.cpp'
            source = self.assert_generated(self.generate(prefix, output), output)
            self.assertEqual('=== demo ===\n\u00a9 Example\n\uc601\uc0c1 license\nBare CR\nLast line\n',
                             self.notices(source))
            self.assert_resource_bytes(source, resources)

    def test_web_utf8_notice_includes_serial_viewshed_license(self):
        with tempfile.TemporaryDirectory() as directory:
            notice = '\u00a9 Example\r\n\uc601\uc0c1 license\nBare CR\rLast line\r\n'
            prefix, _ = self.make_prefix(directory, notice.encode('utf-8'))
            output = Path(directory) / 'generated/resources.cpp'
            source = self.assert_generated(self.generate(prefix, output, web=True), output)
            serial_license = VIEWSHED_LICENSE.read_text(encoding='utf-8')
            self.assertEqual('=== demo ===\n\u00a9 Example\n\uc601\uc0c1 license\nBare CR\nLast line\n'
                             + '\n\n=== gdal4k serial Viewshed ===\n' + serial_license,
                             self.notices(source, web=True))
            self.assertNotIn('static const unsigned char resource_', source)
            self.assertIn('return "/vsigdal4k/resources";', source)

    def test_ascii_resources_without_notices_use_an_explicit_output_encoding(self):
        # No text input is read in this native case: it independently exercises
        # the output encoding boundary under EncodingWarning-as-error.
        with tempfile.TemporaryDirectory() as directory:
            prefix, resources = self.make_prefix(directory)
            output = Path(directory) / 'generated/resources.cpp'
            source = self.assert_generated(self.generate(prefix, output), output)
            self.assertEqual('', self.notices(source))
            self.assert_resource_bytes(source, resources)

    def test_invalid_utf8_notice_fails_before_creating_native_or_web_output(self):
        for web in (False, True):
            with self.subTest(web=web), tempfile.TemporaryDirectory() as directory:
                prefix, _ = self.make_prefix(directory, b'Copyright \xff invalid\n')
                output = Path(directory) / 'not-created/resources.cpp'
                result = self.generate(prefix, output, web=web)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('UnicodeDecodeError', result.stderr.decode('utf-8', errors='replace'))
                self.assertFalse(output.exists())
                self.assertFalse(output.parent.exists())


if __name__ == '__main__':
    unittest.main()
