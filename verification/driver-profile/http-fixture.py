"""Owned loopback HTTP/TLS fault fixture for the optional native HTTP profile."""
from contextlib import contextmanager
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
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


def windows_certificate(output, name, ca, hostname, crl_url=None):
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
    if crl_url:
        point = x509.DistributionPoint(full_name=[x509.UniformResourceIdentifier(crl_url)],
                                       relative_name=None, reasons=None, crl_issuer=None)
        builder = builder.add_extension(x509.CRLDistributionPoints([point]), critical=False)
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
                  ca=is_ca, trustedIssuerProvided=ca is not None,
                  serialHex=format(cert.serial_number, 'x'), crlDistributionPoint=crl_url)
    (output / f'{name}-generator.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf-8', newline='\n')
    return key_path, cert_path


def windows_crl(output, ca, revoked, crl_url):
    """One immutable public CRL; neither CA import nor revocation bypass."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    windows_process_api().verify_private_path(output, directory=True)
    windows_certificate_tool(output)
    key = serialization.load_pem_private_key(ca[0].read_bytes(), password=None)
    issuer = x509.load_pem_x509_certificate(ca[1].read_bytes())
    leaf = x509.load_pem_x509_certificate(revoked[1].read_bytes())
    now = datetime.datetime.now(datetime.timezone.utc)
    entry = (x509.RevokedCertificateBuilder().serial_number(leaf.serial_number)
             .revocation_date(now - datetime.timedelta(minutes=1))
             .add_extension(x509.CRLReason(x509.ReasonFlags.key_compromise), critical=False).build())
    crl = (x509.CertificateRevocationListBuilder().issuer_name(issuer.subject)
           .last_update(now - datetime.timedelta(minutes=1)).next_update(now + datetime.timedelta(days=1))
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
           .add_extension(x509.CRLNumber(1), critical=False).add_revoked_certificate(entry)
           .sign(key, hashes.SHA256()))
    issuer.public_key().verify(crl.signature, crl.tbs_certlist_bytes, padding.PKCS1v15(), crl.signature_hash_algorithm)
    data = crl.public_bytes(serialization.Encoding.DER)
    path = output / 'fixture-ca.crl'
    with path.open('xb') as stream:
        stream.write(data)
    return dict(file=path.name, url=crl_url, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                issuer=crl.issuer.rfc4514_string(), thisUpdateUtc=crl.last_update_utc.isoformat(),
                nextUpdateUtc=crl.next_update_utc.isoformat(), number=1,
                authorityKeyIdentifierHex=crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier.hex(),
                revokedSerialHex=[format(item.serial_number, 'x') for item in crl],
                signatureAlgorithm=crl.signature_algorithm_oid.dotted_string, signatureVerified=True)


class CrlServer(http.server.ThreadingHTTPServer):
    """Separate loopback endpoint exposing only one bounded public DER object."""
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, token):
        self.path = '/crl/' + token + '/fixture-ca.crl'
        self.body = None
        self.records = []
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(('127.0.0.1', 0), CrlHandler)

    def publish(self, data):
        if self.body is not None or not 0 < len(data) <= 16384:
            raise ValueError('Exactly one immutable CRL of at most16KiB is required')
        self.body = bytes(data)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
        # A held socket object and absolute deadline also bound partial headers.
        timer = threading.Timer(2, expire)
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            timer.join(timeout=2)
            self.slots.release()


class CrlHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.respond(True)

    def do_HEAD(self):
        self.respond(False)

    def do_POST(self):
        self.respond(False, method_allowed=False)

    def respond(self, send_body, method_allowed=True):
        self.close_connection = True
        matched = self.path == self.server.path
        with self.server.lock:
            number = len(self.server.records) + 1
            status = 503 if number > 128 or self.server.body is None else 405 if not method_allowed else 404 if not matched else 200
            record = dict(method=self.command, pathKind='public-crl' if matched else 'other', status=status, bodyBytes=0)
            if number <= 128:
                self.server.records.append(record)
        data = self.server.body if status == 200 else b''
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', 'application/pkix-crl')
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.send_header('Connection', 'close')
            self.end_headers()
            if send_body and data:
                self.wfile.write(data)
                record['bodyBytes'] = len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            record['writeIncomplete'] = True


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


def certificate(output, name, ca=None, hostname='localhost', crl_url=None):
    if os.name == 'nt':
        if crl_url:
            return windows_certificate(output, name, ca, hostname, crl_url=crl_url)
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
    servers, threads = [], []
    started_servers = set()
    crl_server, crl_thread, crl_started = None, None, False
    urls = {}
    try:
        revoked, revocation, crl_info = None, None, None
        if os.name == 'nt':
            # Bind first to put the exact fresh URI in both issuer and leaf CDPs.
            # Generation failures below are covered by this same finally block.
            crl_server = CrlServer(secrets.token_hex(16))
            crl_url = f'http://127.0.0.1:{crl_server.server_port}{crl_server.path}'
            ca = certificate(output, 'ca', hostname='gdal4k-fixture-ca', crl_url=crl_url)
            trusted = certificate(output, 'trusted', ca, crl_url=crl_url)
            untrusted = certificate(output, 'untrusted')
            wrong_name = certificate(output, 'wrong-name', ca, hostname='wrong.invalid', crl_url=crl_url)
            revoked = certificate(output, 'revoked', ca, crl_url=crl_url)
            crl_info = windows_crl(output, ca, revoked, crl_url)
            crl_server.publish((output / crl_info['file']).read_bytes())
            crl_thread = threading.Thread(target=lambda: crl_server.serve_forever(poll_interval=0.05), name='owned-public-crl')
            crl_thread.start()
            crl_started = True
        else:
            ca = certificate(output, 'ca', hostname='gdal4k-fixture-ca')
            trusted = certificate(output, 'trusted', ca)
            untrusted = certificate(output, 'untrusted')
            wrong_name = certificate(output, 'wrong-name', ca, hostname='wrong.invalid')
        cases = [('http', None), ('https', trusted), ('untrusted', untrusted), ('wrong-name', wrong_name)]
        if revoked:
            cases.append(('revoked', revoked))
        revoked_base = None
        for name, cert in cases:
            server = Server(image)
            servers.append((name, server))  # Track the bound socket before TLS setup can fail.
            if cert:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(str(cert[1]), str(cert[0]))
                server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start(); threads.append(thread); started_servers.add(name)
            base = ('http' if cert is None else 'https') + f'://127.0.0.1:{server.server_port}'
            if name == 'revoked':
                revoked_base = base
            else:
                urls[name] = base
        settings = dict(urls=urls, ca=str(ca[1]), output=str(output))
        if crl_info:
            manifest = dict(schemaVersion=1, crl=crl_info,
                            certificates={name: json.loads((output / (name + '-generator.json')).read_text(encoding='utf-8'))
                                          for name in ['ca', 'trusted', 'untrusted', 'wrong-name', 'revoked']},
                            server=dict(host='127.0.0.1', port=crl_server.server_port,
                                        owner=windows_process_api().process_identity(os.getpid()),
                                        maxBodyBytes=16384, maxConnections=4, connectionDeadlineSeconds=2,
                                        maxRecordedRequests=128),
                            serverSourceSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            path = output / 'revocation-fixture.json'
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(manifest, indent=2) + '\n')
            settings['windowsRevocation'] = dict(crlUrl=crl_info['url'], revokedBase=revoked_base,
                                                 crlFile=str(output / crl_info['file']), manifest=str(path),
                                                 cleanupFile=str(output / 'revocation-cleanup.json'))
        yield settings
    finally:
        # Keep the CRL endpoint alive while any TLS request is finishing.
        try:
            for name, server in servers:
                if name in started_servers:
                    server.shutdown()
            for thread in threads: thread.join()
            for _, server in servers:
                server.server_close() # Close every listener before evidence writes can fail.
            for name, server in servers:
                (output / f'{name}-requests.json').write_text(json.dumps(server.records, indent=2) + '\n')
        finally:
            if crl_server is not None:
                try:
                    if crl_started:
                        crl_server.shutdown()
                        crl_thread.join(timeout=5)
                        if crl_thread.is_alive():
                            raise RuntimeError('Owned CRL server did not stop')
                finally:
                    crl_server.server_close()
                    (output / 'crl-requests.json').write_text(json.dumps(crl_server.records, indent=2) + '\n',
                                                               encoding='utf-8', newline='\n')
                    cleanup = dict(crlListenerClosed=crl_server.socket.fileno() == -1,
                                   crlServerThreadStopped=not crl_started or not crl_thread.is_alive(),
                                   tlsListenersClosed=all(server.socket.fileno() == -1 for _, server in servers),
                                   tlsServerThreadsStopped=all(not thread.is_alive() for thread in threads))
                    (output / 'revocation-cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n',
                                                                    encoding='utf-8', newline='\n')
