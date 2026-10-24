"""TLS generator boundaries; native Windows checks are not Schannel acceptance."""
import datetime
import importlib.util
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
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
        crl_url = 'http://127.0.0.1:18080/crl/owned/fixture-ca.crl'
        with patch.object(fixture, 'os', SimpleNamespace(name='nt')), \
                patch.object(fixture, 'windows_certificate', return_value=('key', 'cert')) as generate, \
                patch.object(fixture.subprocess, 'run') as external:
            self.assertEqual(('key', 'cert'), fixture.certificate(Path('owned'), 'wrong-name', ca,
                                                                 'wrong.invalid', crl_url=crl_url))
            generate.assert_called_once_with(Path('owned'), 'wrong-name', ca, 'wrong.invalid', crl_url=crl_url)
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


class PublicCrlServerTest(unittest.TestCase):
    """Real loopback transport controls with opaque bytes; no TLS/CRL claim."""

    def setUp(self):
        self.server = fixture.CrlServer('owned-unit-token')
        self.thread = None
        self.addCleanup(self.close_server)

    def close_server(self):
        try:
            if self.thread is not None:
                self.server.shutdown()
                self.thread.join(timeout=5)
        finally:
            self.server.server_close()
        if self.thread is not None:
            self.assertFalse(self.thread.is_alive())
        self.assertEqual(-1, self.server.socket.fileno())

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05})
        self.thread.start()

    def request(self, method, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read(16385)
        finally:
            connection.close()

    def test_only_fixed_public_body_is_served_and_records_omit_request_text(self):
        body = b'opaque public bytes, not a cryptographic fixture'
        self.server.publish(body)
        self.start()
        status, headers, actual = self.request('GET', self.server.path)
        self.assertEqual((200, body), (status, actual))
        self.assertEqual(str(len(body)), headers['Content-Length'])
        self.assertEqual('application/pkix-crl', headers['Content-Type'])
        status, headers, actual = self.request('HEAD', self.server.path)
        self.assertEqual((200, b''), (status, actual))
        self.assertEqual(str(len(body)), headers['Content-Length'])
        for method, path, expected in [
            ('GET', '/private-ca.key', 404),
            ('GET', self.server.path + '?secret-crl-query-token', 404),
            ('POST', self.server.path, 405),
        ]:
            with self.subTest(method=method, expected=expected):
                self.assertEqual((expected, b''), self.request(method, path)[::2])
        with self.server.lock:
            records = [dict(record) for record in self.server.records]
        self.assertEqual([200, 200, 404, 404, 405], [r['status'] for r in records])
        self.assertEqual(['GET', 'HEAD', 'GET', 'GET', 'POST'], [r['method'] for r in records])
        self.assertEqual([len(body), 0, 0, 0, 0], [r['bodyBytes'] for r in records])
        for record in records:
            self.assertLessEqual({'method', 'pathKind', 'status', 'bodyBytes'}, set(record))
            self.assertLessEqual(set(record), {'method', 'pathKind', 'status', 'bodyBytes', 'writeIncomplete'})
            self.assertIn(record['pathKind'], {'public-crl', 'other'})
        encoded = json.dumps(records)
        for private in ['/private-ca.key', 'secret-crl-query-token', self.server.path]:
            self.assertNotIn(private, encoded)

    def test_body_publication_and_record_inventory_are_bounded(self):
        with self.assertRaises((ValueError, RuntimeError)):
            self.server.publish(b'')
        with self.assertRaises((ValueError, RuntimeError)):
            self.server.publish(b'x' * 16385)
        self.server.publish(b'x' * 16384)
        with self.assertRaises((ValueError, RuntimeError)):
            self.server.publish(b'replacement')
        self.start()
        for _ in range(128):
            self.assertEqual(200, self.request('HEAD', self.server.path)[0])
        self.assertEqual((503, b''), self.request('GET', self.server.path)[::2])
        with self.server.lock:
            self.assertEqual(128, len(self.server.records))

    def test_partial_headers_have_absolute_connection_deadline(self):
        self.server.publish(b'public')
        self.start()
        started = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=4) as connection:
            connection.sendall(('GET ' + self.server.path + ' HTTP/1.0\r\nX-Unfinished: ').encode('ascii'))
            stopped = threading.Event()
            def drip_headers():
                while not stopped.wait(0.2):
                    try:
                        connection.sendall(b'a')
                    except OSError:
                        return
            sender = threading.Thread(target=drip_headers)
            sender.start()
            try:
                self.assertEqual(b'', connection.recv(1))
            finally:
                stopped.set()
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 3.5)

    def test_generation_failure_closes_real_bound_crl_listener(self):
        # Only the Windows selection/ACL and certificate failure are modeled;
        # socket binding and cleanup use the actual implementation on this host.
        real_server = fixture.CrlServer
        captured = []
        def bound_server(token):
            server = real_server(token)
            captured.append(server)
            self.addCleanup(server.server_close)
            return server
        windows_os = SimpleNamespace(**vars(os))
        windows_os.name = 'nt'
        acl = SimpleNamespace(private_directory=lambda _: {'daclProtected': True})
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(fixture, 'os', windows_os), \
                patch.object(fixture, 'windows_process_api', return_value=acl), \
                patch.object(fixture, 'CrlServer', side_effect=bound_server), \
                patch.object(fixture, 'certificate', side_effect=RuntimeError('owned certificate generation failed')):
            with self.assertRaisesRegex(RuntimeError, 'owned certificate generation failed'):
                with fixture.fixture(Path(temporary) / 'owned-http'):
                    self.fail('Generation failure must not yield a fixture')
            cleanup = json.loads((Path(temporary) / 'owned-http/revocation-cleanup.json').read_text())
            self.assertEqual({'crlListenerClosed', 'crlServerThreadStopped',
                              'tlsListenersClosed', 'tlsServerThreadsStopped'}, set(cleanup))
            self.assertTrue(all(cleanup.values()))
        self.assertEqual(1, len(captured))
        self.assertEqual(-1, captured[0].socket.fileno())


@unittest.skipUnless(os.name == 'nt', 'Actual pinned Windows generator and DACL run only on Windows')
class ActualWindowsCertificateTest(unittest.TestCase):
    def test_real_owned_ca_san_chain_rejection_and_private_key_acl(self):
        from windows_process import verify_private_path
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'owned-http'
            with fixture.fixture(output) as settings:
                self.assertEqual({'http', 'https', 'untrusted', 'wrong-name'}, set(settings['urls']))
                revocation = settings['windowsRevocation']
                crl_url = revocation['crlUrl']
                crl_endpoint = urlsplit(crl_url)
                self.assertEqual(('http', '127.0.0.1'), (crl_endpoint.scheme, crl_endpoint.hostname))
                self.assertRegex(crl_endpoint.path, r'^/crl/[0-9a-f]{32}/fixture-ca\.crl$')
                self.assertNotEqual(crl_endpoint.port, urlsplit(settings['urls']['http']).port)
                certs = {name: x509.load_pem_x509_certificate((output / (name + '.crt')).read_bytes())
                         for name in ['ca', 'trusted', 'revoked', 'wrong-name', 'untrusted']}
                ca = certs['ca']
                crl_bytes = Path(revocation['crlFile']).read_bytes()
                crl = x509.load_der_x509_crl(crl_bytes)
                now = datetime.datetime.now(datetime.timezone.utc)
                self.assertLessEqual(crl.last_update_utc, now)
                self.assertLess(now, crl.next_update_utc)
                ca.public_key().verify(crl.signature, crl.tbs_certlist_bytes,
                                       padding.PKCS1v15(), crl.signature_hash_algorithm)
                self.assertEqual(ca.subject, crl.issuer)
                ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
                crl_aki = crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
                self.assertEqual(ca_ski, crl_aki)
                self.assertEqual(1, crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number)
                self.assertEqual([certs['revoked'].serial_number], [entry.serial_number for entry in crl])
                self.assertLessEqual(next(iter(crl)).revocation_date_utc, now)
                self.assertNotIn(certs['trusted'].serial_number, [entry.serial_number for entry in crl])
                for name in ['ca', 'trusted', 'revoked', 'wrong-name']:
                    cert = certs[name]
                    self.assertLessEqual(cert.not_valid_before_utc, now)
                    self.assertLess(now, cert.not_valid_after_utc)
                    self.assertEqual(ca.subject, cert.issuer)
                    ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                           padding.PKCS1v15(), cert.signature_hash_algorithm)
                    self.assertEqual(ca_ski, cert.extensions.get_extension_for_class(
                        x509.AuthorityKeyIdentifier).value.key_identifier)
                    points = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
                    self.assertEqual([[crl_url]], [[uri.value for uri in point.full_name] for point in points])
                manifest = json.loads(Path(revocation['manifest']).read_text(encoding='utf-8'))
                self.assertEqual(hashlib.sha256(crl_bytes).hexdigest(), manifest['crl']['sha256'])
                self.assertEqual(len(crl_bytes), manifest['crl']['bytes'])
                self.assertEqual(crl_url, manifest['crl']['url'])
                self.assertEqual(Path(revocation['crlFile']).name, manifest['crl']['file'])
                self.assertEqual(crl.issuer.rfc4514_string(), manifest['crl']['issuer'])
                self.assertEqual(crl.last_update_utc.isoformat(), manifest['crl']['thisUpdateUtc'])
                self.assertEqual(crl.next_update_utc.isoformat(), manifest['crl']['nextUpdateUtc'])
                self.assertEqual(1, manifest['crl']['number'])
                self.assertEqual(crl_aki.hex(), manifest['crl']['authorityKeyIdentifierHex'])
                self.assertEqual([format(certs['revoked'].serial_number, 'x')], manifest['crl']['revokedSerialHex'])
                self.assertTrue(manifest['crl']['signatureVerified'])
                self.assertEqual({'ca', 'trusted', 'revoked', 'wrong-name', 'untrusted'}, set(manifest['certificates']))
                for name, cert in certs.items():
                    public = manifest['certificates'][name]
                    self.assertEqual(format(cert.serial_number, 'x'), public['serialHex'])
                    self.assertEqual(hashlib.sha256(cert.public_bytes(serialization.Encoding.PEM)).hexdigest(),
                                     public['certificateSha256'])
                    self.assertEqual(None if name == 'untrusted' else crl_url, public['crlDistributionPoint'])
                self.assertEqual(hashlib.sha256((ROOT/'verification/driver-profile/http-fixture.py').read_bytes()).hexdigest(),
                                 manifest['serverSourceSha256'])
                # This request proves the fixture serves its retained DER bytes;
                # Python HTTP is not a Schannel revocation-fetch observation.
                connection = http.client.HTTPConnection('127.0.0.1', crl_endpoint.port, timeout=5)
                try:
                    connection.request('GET', crl_endpoint.path)
                    response = connection.getresponse()
                    self.assertEqual(200, response.status)
                    self.assertEqual('application/pkix-crl', response.getheader('Content-Type'))
                    self.assertEqual(str(len(crl_bytes)), response.getheader('Content-Length'))
                    self.assertEqual(crl_bytes, response.read(16385))
                finally:
                    connection.close()
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
                keys = list(output.glob('*.key'))
                self.assertEqual({'ca.key', 'trusted.key', 'revoked.key', 'wrong-name.key', 'untrusted.key'},
                                 {key.name for key in keys})
                for key in keys:
                    self.assertTrue(verify_private_path(key)['daclProtected'])
                tool = json.loads((output/'certificate-tool.json').read_text(encoding='utf-8'))
                self.assertEqual('50.0.2', tool['versions']['cryptography'])
                self.assertEqual({name + '-generator.json' for name in certs},
                                 {path.name for path in output.glob('*-generator.json')})
            self.assertEqual([], json.loads((output/'untrusted-requests.json').read_text(encoding='utf-8')))
            self.assertEqual([], json.loads((output/'wrong-name-requests.json').read_text(encoding='utf-8')))
            cleanup = json.loads(Path(revocation['cleanupFile']).read_text(encoding='utf-8'))
            for key in ['crlListenerClosed', 'crlServerThreadStopped', 'tlsListenersClosed', 'tlsServerThreadsStopped']:
                self.assertTrue(cleanup[key])
            requests = json.loads((output/'crl-requests.json').read_text(encoding='utf-8'))
            self.assertTrue(any(record['method'] == 'GET' and record['pathKind'] == 'public-crl'
                                and record['status'] == 200 and record['bodyBytes'] == len(crl_bytes)
                                for record in requests))
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', crl_endpoint.port), timeout=1)


if __name__ == '__main__':
    unittest.main()
