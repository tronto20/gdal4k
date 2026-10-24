"""TLS generator boundaries; native Windows checks are not Schannel acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_http_fixture_test', ROOT/'verification/driver-profile/http-fixture.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class CertificateToolBoundaryTest(unittest.TestCase):
    def test_importlib_tool_entrypoint_resolves_helper_without_verification_on_sys_path(self):
        verification = str(ROOT/'verification')
        isolated = [str(ROOT/'gdal4k-engine/tools')] + [p for p in sys.path if p != verification]
        with patch.object(sys, 'path', isolated):
            helper = fixture.windows_process_api()
        self.assertEqual((ROOT/'verification/windows_process.py').resolve(), Path(helper.__file__).resolve())
        self.assertTrue(callable(helper.verify_private_path))
        self.assertTrue(callable(helper.private_directory))

    def test_windows_branch_passes_exact_ca_and_name_without_external_openssl(self):
        ca = (Path('ca.key'), Path('ca.crt'))
        with patch.object(fixture, 'os', SimpleNamespace(name='nt')), \
                patch.object(fixture, 'windows_certificate', return_value=('key', 'cert')) as generate, \
                patch.object(fixture.subprocess, 'run') as external:
            self.assertEqual(('key', 'cert'), fixture.certificate(Path('owned'), 'wrong-name', ca, 'wrong.invalid'))
            generate.assert_called_once_with(Path('owned'), 'wrong-name', ca, 'wrong.invalid')
            external.assert_not_called()

    def test_lock_and_requirements_pin_only_actual_cpython312_windows_wheels(self):
        lock = json.loads((ROOT/'verification/fixtures/windows-tls-generator-lock.json').read_text(encoding='utf-8'))
        requirements = (ROOT/'verification/driver-profile/windows-tls-requirements.txt').read_text(encoding='utf-8')
        self.assertEqual({'cryptography', 'cffi', 'pycparser'}, set(lock['packages']))
        self.assertIn('--only-binary=:all:', requirements)
        for name, record in lock['packages'].items():
            self.assertIn(f'{name}=={record["version"]} --hash=sha256:{record["sha256"]}', requirements)
            self.assertTrue(record['metadataUrl'].startswith('https://pypi.org/pypi/'))
            self.assertTrue(record['url'].startswith('https://files.pythonhosted.org/'))
            self.assertRegex(record['sha256'], r'^[0-9a-f]{64}$')
        self.assertIn('cp312-cp312-win_amd64', lock['packages']['cffi']['filename'])


@unittest.skipUnless(os.name == 'nt', 'Actual pinned Windows generator and DACL run only on Windows')
class ActualWindowsCertificateTest(unittest.TestCase):
    def test_real_owned_ca_san_chain_rejection_and_private_key_acl(self):
        from windows_process import verify_private_path
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'owned-http'
            with fixture.fixture(output) as settings:
                context = ssl.create_default_context(cafile=settings['ca'])
                for name in ['https', 'untrusted', 'wrong-name']:
                    endpoint = urlsplit(settings['urls'][name])
                    if name == 'https':
                        with socket.create_connection(('127.0.0.1', endpoint.port), timeout=10) as raw:
                            with context.wrap_socket(raw, server_hostname='localhost') as secure:
                                self.assertIn(('DNS', 'localhost'), secure.getpeercert()['subjectAltName'])
                                self.assertIn(('IP Address', '127.0.0.1'), secure.getpeercert()['subjectAltName'])
                    else:
                        with socket.create_connection(('127.0.0.1', endpoint.port), timeout=10) as raw:
                            with self.assertRaises(ssl.SSLCertVerificationError):
                                context.wrap_socket(raw, server_hostname='localhost')
                # The two negative fixtures must otherwise be valid TLS server
                # leaves, so rejection isolates trust or hostname respectively.
                for name, hostname, trusted_ca in [
                    ('untrusted', 'localhost', output/'untrusted.crt'),
                    ('wrong-name', 'wrong.invalid', Path(settings['ca'])),
                ]:
                    endpoint = urlsplit(settings['urls'][name])
                    valid_context = ssl.create_default_context(cafile=str(trusted_ca))
                    with socket.create_connection(('127.0.0.1', endpoint.port), timeout=10) as raw:
                        with valid_context.wrap_socket(raw, server_hostname=hostname) as secure:
                            self.assertIn(('DNS', hostname), secure.getpeercert()['subjectAltName'])
                for key in output.glob('*.key'):
                    self.assertTrue(verify_private_path(key)['daclProtected'])
                tool = json.loads((output/'certificate-tool.json').read_text(encoding='utf-8'))
                self.assertEqual('50.0.2', tool['versions']['cryptography'])
                self.assertEqual(4, len(list(output.glob('*-generator.json'))))
            self.assertEqual([], json.loads((output/'untrusted-requests.json').read_text(encoding='utf-8')))
            self.assertEqual([], json.loads((output/'wrong-name-requests.json').read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
