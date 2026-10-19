"""Owned loopback HTTP/TLS fault fixture for the optional native HTTP profile."""
from contextlib import contextmanager
import hashlib
import http.server
import json
from pathlib import Path
import re
import ssl
import struct
import subprocess
import threading
import time

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = False
    def __init__(self, image):
        self.image = image
        self.records = []
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        parts = path.strip('/').split('/')
        if len(parts) != 3:
            self.send_error(404); return
        case, key, name = parts
        with self.server.lock:
            previous = [r for r in self.server.records if r['case'] == case and r['key'] == key and r['name'] == 'fixture.tif']
            count = len(previous) + 1
        content = None
        is_stats = case == 'stats'
        if is_stats:
            # The target case is encoded in the filename, independent of this stats request.
            target = name.removesuffix('.geojson')
            with self.server.lock:
                history = [r.copy() for r in self.server.records if r['case'] == target and r['key'] == key and r['name'] == 'fixture.tif']
            props = dict(requests=len(history), bodyBytes=sum(r['bodyBytes'] for r in history),
                         maxRange=max((r['requestedBytes'] for r in history), default=0),
                         status503=sum(r['status'] == 503 for r in history),
                         status401=sum(r['status'] == 401 for r in history),
                         ifRange=sum(r['ifRange'] is not None for r in history),
                         modifiedRange=sum(r['ifRange'] == 'Mon, 19 Oct 2026 00:00:00 GMT' for r in history))
            content = json.dumps(dict(type='FeatureCollection', features=[dict(type='Feature', properties=props, geometry=None)])).encode()
        elif case == 'sidecar' and name == 'fixture.tif.aux.xml':
            content = b'<PAMDataset><Metadata><MDI key="HTTP_SIDECAR">present</MDI></Metadata></PAMDataset>'
        exists = content is not None or name == 'fixture.tif' or (case == 'cache' and name in ['cache0.tif', 'cache1.tif', 'cache2.tif'])
        size = len(content) if content is not None else self.server.image.stat().st_size
        start, end, status = 0, size - 1, 200
        request_range = self.headers.get('Range')
        if not exists:
            status = 404
        elif request_range:
            match = re.fullmatch(r'bytes=(\d+)-(\d+)', request_range)
            if match and 0 <= int(match[1]) <= int(match[2]) < size:
                start, end, status = int(match[1]), int(match[2]), 206
            else: status = 416
        main = name == 'fixture.tif'
        if main:
            if case in ['retry', 'backoff'] and count == 1: status = 503
            if case == 'denied': status = 401
            if case == 'redirect': status = 302
            if case == 'full': status = 200; start, end = 0, size - 1
        length = end - start + 1 if exists and status in [200, 206] else 0
        record = dict(case=case, key=key, name=name, status=status, range=request_range,
                      requestedBytes=end - start + 1 if request_range else 0,
                      ifRange=self.headers.get('If-Range'), bodyBytes=0)
        with self.server.lock: self.server.records.append(record)
        try:
            if main and case in ['slow', 'timeout']: time.sleep(2)
            self.send_response(status)
            self.send_header('Content-Length', str(length + (1 if main and case == 'length' else 0)))
            if status == 302: self.send_header('Location', 'http://127.0.0.1:1/must-not-follow')
            if status == 503: self.send_header('Retry-After', '5' if case == 'backoff' else '0')
            if status == 206:
                total = size + 1 if main and case == 'size-change' and count >= 3 else size
                self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
            if case != 'no-validator':
                tag = '"changed"' if main and case == 'identity' and count >= 3 else '"stable"'
                if is_stats: tag = '"' + hashlib.sha256(content).hexdigest() + '"'
                self.send_header('ETag', 'W/' + tag if case == 'weak' else tag)
                self.send_header('Last-Modified', 'Mon, 19 Oct 2026 00:00:00 GMT')
            if main and case == 'encoded': self.send_header('Content-Encoding', 'gzip')
            self.end_headers()
            if main and case == 'truncated': return
            if length:
                if content is not None:
                    self.wfile.write(content[start:end + 1]); record['bodyBytes'] = length
                else:
                    with self.server.image.open('rb') as file:
                        file.seek(start)
                        while record['bodyBytes'] < length:
                            if main and case == 'slow-body' and request_range != 'bytes=0-0' and record['bodyBytes'] == 0:
                                self.wfile.write(file.read(1)); record['bodyBytes'] += 1; time.sleep(2)
                            block = file.read(min(65536, length - record['bodyBytes']))
                            if not block: break
                            self.wfile.write(block); record['bodyBytes'] += len(block)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass


def certificate(output, name, ca=None, hostname='localhost'):
    config = output / f'{name}.cnf'
    config.write_text('[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=' + hostname +
                      '\n[v3]\nsubjectAltName=DNS:' + hostname + (',IP:127.0.0.1' if hostname == 'localhost' else '') +
                      '\nbasicConstraints=critical,CA:' + ('FALSE' if ca else 'TRUE') +
                      '\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\n' +
                      ('keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' if ca else 'keyUsage=critical,keyCertSign,cRLSign\n'))
    key, cert = output / f'{name}.key', output / f'{name}.crt'
    command = ['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-config', str(config)]
    if ca:
        csr = output / f'{name}.csr'
        subprocess.run(command + ['-out', str(csr)], check=True, capture_output=True)
        subprocess.run(['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(ca[1]), '-CAkey', str(ca[0]),
                        '-CAcreateserial', '-days', '2', '-out', str(cert), '-extfile', str(config), '-extensions', 'v3'], check=True, capture_output=True)
    else:
        subprocess.run(command + ['-x509', '-days', '2', '-out', str(cert), '-extensions', 'v3'], check=True, capture_output=True)
    key.chmod(0o600)
    return key, cert


@contextmanager
def fixture(output):
    output.mkdir(parents=True, exist_ok=False)
    image = output / 'fixture.tif'
    # Separate georeferenced profile fixture for VRT leases; original performance
    # fixture writer and all frozen baseline bytes remain unchanged.
    tiles = 64; arrays = 8 + 2 + 14 * 12 + 4; extra = arrays + tiles * 8; data = extra + 72
    entries = [(256, 4, 1, 2048), (257, 4, 1, 2048), (258, 3, 1, 8), (259, 3, 1, 1),
               (262, 3, 1, 1), (277, 3, 1, 1), (284, 3, 1, 1), (322, 4, 1, 256), (323, 4, 1, 256),
               (324, 4, tiles, arrays), (325, 4, tiles, arrays + tiles * 4), (339, 3, 1, 1),
               (33550, 12, 3, extra), (33922, 12, 6, extra + 24)]
    with image.open('xb') as file:
        file.write(b'II' + struct.pack('<HIH', 42, 8, len(entries)))
        for entry in sorted(entries): file.write(struct.pack('<HHII', *entry))
        file.write(struct.pack('<I', 0))
        file.write(struct.pack('<64I', *(data + i * 65536 for i in range(tiles))))
        file.write(struct.pack('<64I', *([65536] * tiles)))
        file.write(struct.pack('<9d', 1, 1, 0, 0, 0, 0, 100, 200, 0))
        assert file.tell() == data
        block = bytes((x * 31 + y * 17 + (x ^ y)) & 255 for y in range(256) for x in range(256))
        for _ in range(tiles): file.write(block)
    ca = certificate(output, 'ca', hostname='gdal4k-fixture-ca')
    trusted = certificate(output, 'trusted', ca)
    untrusted = certificate(output, 'untrusted')
    wrong_name = certificate(output, 'wrong-name', ca, hostname='wrong.invalid')
    servers, threads = [], []
    urls = {}
    try:
        for name, cert in [('http', None), ('https', trusted), ('untrusted', untrusted), ('wrong-name', wrong_name)]:
            server = Server(image)
            if cert:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(str(cert[1]), str(cert[0]))
                server.socket = context.wrap_socket(server.socket, server_side=True)
            servers.append((name, server))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
            urls[name] = ('http' if cert is None else 'https') + f'://127.0.0.1:{server.server_port}'
        yield dict(urls=urls, ca=str(ca[1]), output=str(output))
    finally:
        for _, server in servers: server.shutdown()
        for thread in threads: thread.join()
        for name, server in servers:
            server.server_close() # Join owned request threads before recording complete response evidence.
            (output / f'{name}-requests.json').write_text(json.dumps(server.records, indent=2) + '\n')
