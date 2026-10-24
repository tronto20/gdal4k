"""Owned loopback HTTP/TLS fault fixture for the optional native HTTP profile."""
from contextlib import contextmanager
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import ssl
import struct
import subprocess
import threading
import time


def windows_process_api():
    # This module is also loaded by tools/driver-profile.py with importlib; its
    # caller's sys.path does not necessarily include verification/.
    path = Path(__file__).resolve().parents[1] / 'windows_process.py'
    spec = importlib.util.spec_from_file_location('native_http_windows_process', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def windows_certificate_tool(output):
    """Use only the versioned, hash-pinned Windows fixture generator wheels."""
    import importlib.metadata
    import importlib.util
    import platform
    import sys
    if os.name != 'nt' or platform.machine().lower() != 'amd64' or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Pinned Windows certificate generator requires actual CPython3.12 Windows x64')
    root = Path(__file__).resolve().parents[2]
    lock_path = root / 'verification/fixtures/windows-tls-generator-lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    versions = {name: importlib.metadata.version(name) for name in lock['packages']}
    assert versions == {name: value['version'] for name, value in lock['packages'].items()}
    binaries = {}
    for module in ['cryptography.hazmat.bindings._rust', '_cffi_backend', 'pycparser']:
        path = Path(importlib.util.find_spec(module).origin)
        binaries[module] = dict(filename=path.name, bytes=path.stat().st_size,
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    receipt = dict(schemaVersion=1, tool='pyca/cryptography', versions=versions,
                   lockSha256=hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                   python=dict(version=sys.version, executable=Path(sys.executable).name,
                               sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()),
                   actualInstalledModules=binaries,
                   scope='Fixture certificates only; default Schannel, peer/host/revocation verification and system trust store unchanged')
    path = output / 'certificate-tool.json'
    if path.exists():
        assert json.loads(path.read_text(encoding='utf-8')) == receipt
    else:
        path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
    return receipt


def windows_certificate(output, name, ca, hostname):
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    process = windows_process_api()
    process.verify_private_path(output, directory=True)
    windows_certificate_tool(output)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    issuer_key = serialization.load_pem_private_key(ca[0].read_bytes(), password=None) if ca else key
    issuer = x509.load_pem_x509_certificate(ca[1].read_bytes()).subject if ca else subject
    is_ca = name == 'ca'
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.DNSName(hostname)] + ([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))] if hostname == 'localhost' else [])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
               .not_valid_after(now + datetime.timedelta(days=2))
               .add_extension(x509.SubjectAlternativeName(san), critical=False)
               .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
               .add_extension(x509.KeyUsage(digital_signature=not is_ca, content_commitment=False,
                                           key_encipherment=not is_ca, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=is_ca, crl_sign=is_ca,
                                           encipher_only=None, decipher_only=None), critical=True))
    if not is_ca:
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    cert = builder.sign(issuer_key, hashes.SHA256())
    key_path, cert_path = output / f'{name}.key', output / f'{name}.crt'
    with key_path.open('xb') as stream:
        acl = process.private_file(key_path)  # Protect the empty file before private bytes exist.
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    with cert_path.open('xb') as stream:
        stream.write(cert_bytes)
    public = dict(name=name, subject=subject.rfc4514_string(), issuer=issuer.rfc4514_string(),
                  certificateSha256=hashlib.sha256(cert_bytes).hexdigest(), privateKeyAcl=acl,
                  san=[hostname] + (['127.0.0.1'] if hostname == 'localhost' else []),
                  ca=is_ca, trustedIssuerProvided=ca is not None)
    (output / f'{name}-generator.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8', newline='\n')
    return key_path, cert_path

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
    if os.name == 'nt':
        return windows_certificate(output, name, ca, hostname)
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
    if os.name == 'nt':
        windows_process_api().private_directory(output)
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
