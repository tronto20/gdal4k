"""Validate the owned fault fixture itself; this does not validate the native HTTP backend."""
import http.client
import importlib.util
import json
from pathlib import Path
import ssl
import struct
import tempfile
import unittest
from urllib.parse import urlsplit

spec = importlib.util.spec_from_file_location('http_fixture', Path(__file__).parent / 'driver-profile/http-fixture.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class HttpFixtureTest(unittest.TestCase):
    def test_real_ranges_faults_geotransform_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'fixture'
            with module.fixture(output) as fixture:
                image = output / 'fixture.tif'
                with image.open('rb') as file:
                    self.assertEqual(b'II*\x00\x08\x00\x00\x00', file.read(8))
                    count = struct.unpack('<H', file.read(2))[0]
                    entries = {v[0]: v for v in (struct.unpack('<HHII', file.read(12)) for _ in range(count))}
                    file.seek(entries[33550][3]); self.assertEqual((1., 1., 0.), struct.unpack('<3d', file.read(24)))
                    file.seek(entries[33922][3]); self.assertEqual((0., 0., 0., 100., 200., 0.), struct.unpack('<6d', file.read(48)))
                def request(origin, case, context=None, headers=None):
                    url = urlsplit(origin)
                    connection = http.client.HTTPSConnection(url.hostname, url.port, context=context, timeout=5) if url.scheme == 'https' else http.client.HTTPConnection(url.hostname, url.port, timeout=5)
                    try:
                        connection.request('GET', f'/{case}/unit/fixture.tif', headers=headers or {'Range': 'bytes=0-0'})
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                    finally: connection.close()
                status, headers, body = request(fixture['urls']['http'], 'ok')
                self.assertEqual(206, status); self.assertEqual(b'I', body)
                self.assertEqual('bytes 0-0/' + str(image.stat().st_size), headers['Content-Range'])
                self.assertEqual(503, request(fixture['urls']['http'], 'retry')[0])
                self.assertEqual(206, request(fixture['urls']['http'], 'retry')[0])
                self.assertEqual(401, request(fixture['urls']['http'], 'denied')[0])
                self.assertEqual(302, request(fixture['urls']['http'], 'redirect')[0])
                context = ssl.create_default_context(cafile=fixture['ca'])
                self.assertEqual(206, request(fixture['urls']['https'], 'ok', context)[0])
                for name in ['untrusted', 'wrong-name']:
                    with self.assertRaises(ssl.SSLCertVerificationError): request(fixture['urls'][name], 'ok', context)
            records = json.loads((output / 'http-requests.json').read_text())
            self.assertEqual(5, len(records)); self.assertEqual(1, sum(r['status'] == 503 for r in records))
            self.assertEqual([], json.loads((output / 'untrusted-requests.json').read_text()))
            self.assertEqual([], json.loads((output / 'wrong-name-requests.json').read_text()))


if __name__ == '__main__': unittest.main()
