"""Owned Linux ARM64 PostGIS container from an exact official base and locked DEBs."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name('postgis-arm64-fixture-lock.json')
DOCKERFILE = Path(__file__).with_name('postgis-arm64.Dockerfile')
OWNER = 'dev.gdal4k.postgis-fixture'
DATA = '/var/lib/postgresql/data'
PRIVATE = '/run/gdal4k-private'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')


def validate_lock(lock):
    if lock.get('schemaVersion') != 1 or lock.get('target') != 'linux-arm64':
        raise ValueError('Unsupported owned ARM provider lock')
    digest = r'sha256:[0-9a-f]{64}'
    if (not re.fullmatch('docker.io/library/postgres@' + digest, lock['baseImage'])
            or lock['baseImage'].split('@')[1] != lock['baseManifestDigest']
            or not re.fullmatch(digest, lock['baseConfigDigest'])):
        raise ValueError('Expected an exact official PostgreSQL base digest')
    versions = {}
    for name, item in lock['basePackages'].items():
        versions[name] = (item['version'], item['architecture'])
    seen = set(versions)
    for item in lock['packages']:
        name = item['name']
        parsed = urllib.parse.urlsplit(item['url'])
        if (name in seen or parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.hostname not in ['deb.debian.org', 'apt.postgresql.org']
                or not parsed.path.endswith('.deb') or parsed.query or parsed.fragment
                or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                or type(item['bytes']) is not int or item['bytes'] <= 0):
            raise ValueError('Invalid or duplicate locked DEB input')
        seen.add(name)
        versions[name] = (item['version'], item['architecture'])
    for name, (version, architecture) in versions.items():
        if (not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name)
                or not re.fullmatch(r'[0-9A-Za-z.+:~_-]+', version)
                or architecture not in ['arm64', 'all']):
            raise ValueError('Invalid locked package name/version/architecture')
    if set(lock['providerBinaryPaths']) != {'postgres', 'initdb', 'pg_ctl', 'psql'}:
        raise ValueError('Expected all four actual base provider binaries')
    for name, path in lock['providerBinaryPaths'].items():
        expected = lock['providerBinaryInputs'][name]
        if (path != 'usr/lib/postgresql/17/bin/' + name
                or not re.fullmatch(r'[0-9a-f]{64}', expected['sha256'])
                or type(expected['bytes']) is not int or expected['bytes'] < 64):
            raise ValueError('Invalid pinned provider binary input')
    return ''.join(f'{name}\t{version}\t{architecture}\n'
                   for name, (version, architecture) in sorted(versions.items()))


def safe_dsn(user, port):
    if (not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', user)
            or type(port) is not int or not 0 < port < 65536):
        raise ValueError('Invalid owned loopback endpoint')
    return f'PG:host=127.0.0.1 port={port} dbname={user} user={user}'


def port_closed(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(('127.0.0.1', port)) != 0


def owned_container(value, container, image, token, volume):
    if (value.get('Id') != container or value.get('Image') != image
            or value.get('Config', {}).get('Labels', {}).get(OWNER) != token
            or not any(m.get('Type') == 'volume' and m.get('Name') == volume
                       and m.get('Destination') == DATA for m in value.get('Mounts', []))):
        raise ValueError('Refusing an unowned or mismatched provider container')
    return value


def owned_volume(value, name, token):
    if value.get('Name') != name or value.get('Labels', {}).get(OWNER) != token:
        raise ValueError('Refusing an unowned provider volume')
    return value


class Commands:
    def __init__(self, output, work, receipt, secret):
        self.output, self.work, self.receipt, self.secret = output, work, receipt, secret.encode()
        (output / 'commands').mkdir()

    def run(self, label, argv, *, input_data=None, timeout=60, check=True):
        prefix = f"commands/{len(self.receipt['commands']):03d}-{label}"
        record = dict(label=label, argv=[str(a).replace(self.secret.decode(), '[redacted]') for a in argv],
                      cwd=str(self.work), startedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      stdout=prefix + '.stdout', stderr=prefix + '.stderr')
        if input_data is not None:
            record['privateStdinOmitted'] = True
        try:
            result = subprocess.run([str(a) for a in argv], cwd=self.work, input=input_data,
                                    capture_output=True, timeout=timeout)
            record['returncode'] = result.returncode
            outputs = (result.stdout, result.stderr)
        except subprocess.TimeoutExpired as failure:
            result = None
            record.update(returncode=None, timedOut=True)
            outputs = (failure.stdout or b'', failure.stderr or b'')
        except OSError as failure:
            result = None
            record.update(returncode=None, executionFailureType=type(failure).__name__)
            outputs = (b'', str(failure).encode('utf-8', errors='replace'))
        for stream, data in zip(['stdout', 'stderr'], outputs):
            public = data.replace(self.secret, b'[fixture-secret-redacted]')
            if public != data:
                private = self.work / 'private-command-output'
                private.mkdir(mode=0o700, exist_ok=True)
                path = private / Path(record[stream]).name
                path.write_bytes(data)
                path.chmod(0o600)
                record['fixtureSecretRedacted'] = True
            path = self.output / record[stream]
            path.write_bytes(public)
            record[stream + 'Sha256'] = sha(path)
        record['completedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.receipt['commands'].append(record)
        write_json(self.output / 'fixture.json', self.receipt)
        if result is None or (check and result.returncode):
            raise RuntimeError('Owned ARM PostGIS command failed: ' + label + '; inspect ' + prefix)
        return result


class Provider:
    def __init__(self, output):
        self.output = Path(output).absolute()
        if (any(p.is_symlink() for p in [self.output, *self.output.parents])
                or (self.output.exists() and (not self.output.is_dir() or any(self.output.iterdir())))):
            raise ValueError('Refusing an occupied fixture output')
        private = ROOT / 'verification/build/postgis-arm64'
        if self.output.is_relative_to(private) or private.is_relative_to(self.output):
            raise ValueError('Public evidence and private provider files must be separate')
        self.output.mkdir(parents=True, exist_ok=True)
        if private.is_symlink() or not private.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('Expected an owned private provider workspace')
        private.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='owned-', dir=private))
        self.work.chmod(0o700)
        self.lock = json.loads(LOCK.read_text(encoding='utf-8'))
        self.expected_packages = validate_lock(self.lock)
        self.token = secrets.token_hex(12)
        self.user = 'g4k_' + secrets.token_hex(8)
        self.password = secrets.token_hex(32)
        self.container = self.image = self.volume = self.port = self.postmaster = None
        self.volume_name = 'gdal4k-pg-arm64-' + self.token
        self.receipt = dict(schemaVersion=1, target='linux-arm64', result='running',
                            providerLockSha256=sha(LOCK), sourceSha256={str(p.relative_to(ROOT)): sha(p)
                            for p in [Path(__file__), LOCK, DOCKERFILE]},
                            startup={}, cleanup={}, commands=[], provider={})
        self.commands = Commands(self.output, self.work, self.receipt, self.password)

    def docker(self, label, *args, **kwargs):
        return self.commands.run(label, ['docker', *args], **kwargs)

    def inspect_container(self):
        result = self.docker('inspect-owned-container', 'inspect', self.container)
        value = json.loads(result.stdout)[0]
        return owned_container(value, self.container, self.image, self.token, self.volume_name)

    def inspect_volume(self):
        result = self.docker('inspect-owned-volume', 'volume', 'inspect', self.volume_name)
        return owned_volume(json.loads(result.stdout)[0], self.volume_name, self.token)

    def execute(self, label, *argv, **kwargs):
        return self.docker(label, 'exec', '--user', 'postgres', self.container, *argv, **kwargs)

    def sql(self, label, sql):
        result = self.docker(label, 'exec', '--user', 'postgres', '--env',
                             'PGPASSFILE=' + PRIVATE + '/pgpass', self.container,
                             '/usr/lib/postgresql/17/bin/psql', '-XAt', '--no-password',
                             '--set=ON_ERROR_STOP=1',
                             '--host=127.0.0.1', '--port=5432', '--username=' + self.user,
                             '--dbname=' + self.user, '--command', sql)
        return result.stdout.decode('utf-8').strip()

    def prepare(self):
        if os.environ.get('DOCKER_HOST', 'unix://').startswith(('tcp:', 'ssh:', 'http:', 'https:')):
            raise ValueError('Owned ARM fixture requires a local Docker daemon')
        endpoint = json.loads(self.docker('docker-context', 'context', 'inspect', '--format',
                                         '{{json .Endpoints.docker.Host}}').stdout)
        if not endpoint.startswith('unix://'):
            raise ValueError('Owned ARM fixture refuses a remote Docker endpoint')
        server = json.loads(self.docker('docker-server', 'version', '--format', '{{json .Server}}').stdout)
        if server.get('Os') != 'linux' or server.get('Arch') not in ['arm64', 'aarch64']:
            raise ValueError('Docker server is not actual Linux ARM64')
        required = sum(p['bytes'] for p in self.lock['packages'])
        if shutil.disk_usage(self.work).free < required * 3 + 1024 ** 3:
            raise RuntimeError('Insufficient owned provider disk headroom')
        context = self.work / 'context'
        debs = context / 'debs'
        debs.mkdir(parents=True)
        downloaded = []
        for package in self.lock['packages']:
            path = debs / (package['sha256'] + '.deb')
            with urllib.request.urlopen(package['url'], timeout=90) as response, path.open('xb') as stream:
                shutil.copyfileobj(response, stream, 1024 * 1024)
            if path.stat().st_size != package['bytes'] or sha(path) != package['sha256']:
                raise ValueError('Locked provider DEB bytes differ: ' + package['name'])
            downloaded.append(dict(package, retainedPath=str(path)))
        (context / 'input-sha256.txt').write_text(''.join(p['sha256'] + '  ' + p['sha256'] + '.deb\n'
                                                        for p in self.lock['packages']), encoding='utf-8', newline='\n')
        (context / 'expected-packages.tsv').write_text(self.expected_packages, encoding='utf-8', newline='\n')
        shutil.copy2(DOCKERFILE, context / 'Dockerfile')
        if not DOCKERFILE.read_text().startswith('FROM ' + self.lock['baseImage'] + '\n'):
            raise ValueError('Provider Dockerfile base differs from lock')
        self.docker('pull-official-base', 'pull', '--platform', 'linux/arm64', self.lock['baseImage'], timeout=600)
        image = json.loads(self.docker('inspect-official-base', 'image', 'inspect', self.lock['baseImage']).stdout)[0]
        if (image['Id'] != self.lock['baseConfigDigest'] or image['Os'] != 'linux'
                or image['Architecture'] != 'arm64'):
            raise ValueError('Actual official base image differs from ARM lock')
        tag = 'gdal4k-arm64-postgis:' + self.token
        self.docker('build-offline-locked-provider', 'build', '--network=none', '--pull=false',
                    '--platform=linux/arm64', '--label', OWNER + '=' + self.token,
                    '-t', tag, context, timeout=900)
        built = json.loads(self.docker('inspect-built-provider', 'image', 'inspect', tag).stdout)[0]
        if built['Architecture'] != 'arm64' or built['Os'] != 'linux' or built['Config']['Labels'].get(OWNER) != self.token:
            raise ValueError('Built provider architecture/ownership differs')
        self.image = built['Id']
        self.receipt['provider'].update(baseImage=self.lock['baseImage'], baseConfig=self.lock['baseConfigDigest'],
                                        imageId=self.image, dockerServer=server, packages=downloaded,
                                        expectedPackageInventorySha256=sha(context / 'expected-packages.tsv'))

    def start(self):
        # Record the intended exact resource before creation so timeout cleanup
        # can reconcile a successful daemon-side create with a lost CLI reply.
        self.volume = self.volume_name
        self.docker('create-owned-volume', 'volume', 'create', '--label', OWNER + '=' + self.token, self.volume_name)
        self.inspect_volume()
        result = self.docker('create-owned-container', 'create', '--name', self.volume_name,
                             '--label', OWNER + '=' + self.token, '--mount',
                             'type=volume,src=' + self.volume_name + ',dst=' + DATA,
                             '--tmpfs', PRIVATE + ':rw,noexec,nosuid,mode=0700,uid=999,gid=999',
                             '--publish', '127.0.0.1::5432', '--entrypoint', '/bin/sh', self.image,
                             '-c', 'trap "exit 0" INT TERM; while :; do sleep 1; done')
        container_id = result.stdout.decode().strip()
        if not re.fullmatch(r'[0-9a-f]{64}', container_id):
            raise ValueError('Docker did not return an exact owned container ID')
        self.container = container_id
        self.inspect_container()
        self.docker('start-owned-container', 'start', self.container)
        container = self.inspect_container()
        ports = container['NetworkSettings']['Ports']['5432/tcp']
        if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
            raise ValueError('Provider port is not exclusively host loopback')
        self.port = int(ports[0]['HostPort'])
        dsn = safe_dsn(self.user, self.port)
        self.receipt.update(containerId=self.container, volumeName=self.volume_name,
                            connection=dict(dsn=dsn, passwordIncluded=False, hostLoopbackMapping=ports))
        self.docker('owned-data-permissions', 'exec', '--user', '0', self.container,
                    '/bin/sh', '-c', 'chown 999:999 ' + DATA + '; chmod 0700 ' + DATA)
        for name, contents in [('password', self.password), ('pgpass', '127.0.0.1:5432:*:' + self.user + ':' + self.password + '\n')]:
            self.docker('write-private-' + name, 'exec', '-i', '--user', 'postgres', self.container,
                        '/bin/sh', '-c', 'umask 077; cat > ' + PRIVATE + '/' + name,
                        input_data=contents.encode())
        self.docker('launch-owned-postgres', 'exec', '-d', '--user', 'postgres',
                    '--env', 'POSTGRES_USER=' + self.user, '--env', 'POSTGRES_DB=' + self.user,
                    '--env', 'POSTGRES_PASSWORD_FILE=' + PRIVATE + '/password',
                    '--env', 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C', '--env', 'PGDATA=' + DATA,
                    self.container, '/bin/sh', '-c',
                    'exec /usr/local/bin/docker-entrypoint.sh postgres -c listen_addresses=\'*\' -c ssl=off > /tmp/gdal4k-postgres.log 2>&1')
        for _ in range(90):
            result = self.execute('wait-owned-postgres', '/usr/lib/postgresql/17/bin/pg_isready', '-h', '127.0.0.1', '-p', '5432', check=False)
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Owned ARM PostGIS startup did not become healthy')
        self.sql('create-owned-extension', 'CREATE EXTENSION postgis;')
        identity = json.loads(self.sql('actual-server-identity', "SELECT json_build_object('serverVersionNum',current_setting('server_version_num')::int,'serverVersion',version(),'postgisVersion',postgis_lib_version(),'postgisFullVersion',postgis_full_version(),'dataDirectory',current_setting('data_directory'),'encoding',current_setting('server_encoding'),'collation',(SELECT datcollate FROM pg_database WHERE datname=current_database()),'listenAddresses',current_setting('listen_addresses'));"))
        if (identity['serverVersionNum'] != self.lock['serverVersionNum']
                or identity['postgisVersion'] != self.lock['postgisVersion']
                or identity['dataDirectory'] != DATA or identity['encoding'] != 'UTF8'
                or identity['collation'] != 'C' or identity['listenAddresses'] != '*'):
            raise ValueError('Actual owned ARM server identity differs from lock')
        self.postmaster = self.verify_postmaster()
        self.receipt['server'] = dict(identity, postmasterPid=self.postmaster, containerListenAddresses=['*'])
        self.check_provider_bytes()
        table = 'availability_' + secrets.token_hex(8)
        if self.sql('owned-table-absent', "SELECT to_regclass('public." + table + "') IS NULL;") != 't':
            raise ValueError('Refusing to replace an existing provider smoke table')
        try:
            self.sql('point-write-commit', f'BEGIN; CREATE TABLE {table}(value integer,geom geometry(Point,4326)); INSERT INTO {table} VALUES(42,ST_SetSRID(ST_Point(3,4),4326)); COMMIT;')
            values = json.loads(self.sql('point-new-connection-read', f"SELECT json_build_object('value',value,'x',ST_X(geom),'y',ST_Y(geom),'srid',ST_SRID(geom),'count',(SELECT count(*) FROM {table})) FROM {table};"))
            if values != dict(value=42, x=3, y=4, srid=4326, count=1):
                raise ValueError('Actual new-connection ARM provider POINT readback differs')
        finally:
            self.sql('drop-exact-owned-table', 'DROP TABLE IF EXISTS ' + table + ';')
        if self.sql('owned-table-absent-after', "SELECT to_regclass('public." + table + "') IS NULL;") != 't':
            raise ValueError('Exact owned provider table survived cleanup')
        self.receipt['roundTrip'] = dict(table=table, values=values, newConnectionVerified=True, tableDeletedAndAbsentNewConnection=True)
        self.receipt['startup'] = dict(completed=True, healthy=True, dataDirectoryVerified=True, postgisExtensionVerified=True)
        password = self.work / 'pgpass'
        password.write_text('127.0.0.1:' + str(self.port) + ':*:' + self.user + ':' + self.password + '\n', encoding='utf-8', newline='\n')
        password.chmod(0o600)
        return dict(dsn=dsn, env=dict(PGPASSFILE=str(password), PGCONNECT_TIMEOUT='5'),
                    identity=dict(target='linux-arm64', providerLockSha256=self.receipt['providerLockSha256'], safeDSN=dsn), output=str(self.output))

    def verify_postmaster(self):
        self.inspect_container()
        lines = self.execute('owned-postmaster-pid', '/bin/cat', DATA + '/postmaster.pid').stdout.decode().splitlines()
        if len(lines) < 4 or not lines[0].isdigit() or int(lines[0]) <= 1 or lines[1] != DATA or lines[3] != '5432':
            raise ValueError('Postmaster PID file differs from exact owned data directory')
        pid = int(lines[0])
        command = self.execute('owned-postmaster-command', '/bin/cat', '/proc/' + str(pid) + '/cmdline').stdout.split(b'\0')
        if not command or Path(os.fsdecode(command[0])).name != 'postgres':
            raise ValueError('Owned postmaster command is not PostgreSQL')
        # Official entrypoint uses PGDATA; an explicit -D must also agree.
        if b'-D' in command:
            index = command.index(b'-D') + 1
            if index >= len(command) or command[index] != DATA.encode():
                raise ValueError('Postmaster command points outside the owned cluster')
        return pid

    def check_provider_bytes(self):
        inventory = self.execute('actual-package-inventory', '/bin/cat', '/locked-actual-packages.tsv').stdout.decode()
        if inventory != self.expected_packages:
            raise ValueError('Actual provider package inventory differs from full lock')
        binaries = {}
        for name, path in self.lock['providerBinaryPaths'].items():
            result = self.execute('actual-binary-' + name, '/bin/cat', '/' + path)
            b = result.stdout
            if len(b) < 64 or b[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', b, 18)[0] != 183:
                raise ValueError('Provider binary is not ELF64 AArch64: ' + name)
            identity = dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest())
            if identity != self.lock['providerBinaryInputs'][name]:
                raise ValueError('Provider binary differs from actual pinned base bytes: ' + name)
            binaries[name] = dict(path='/' + path, **identity, architecture='AArch64', eMachine=183)
        self.receipt['provider'].update(binaries=binaries, actualPackageInventorySha256=hashlib.sha256(inventory.encode()).hexdigest(), packageCount=len(inventory.splitlines()))

    def cleanup(self):
        cleanup = self.receipt['cleanup']
        errors = []

        def attempt(label, action):
            try:
                return action()
            except BaseException as error:
                errors.append(dict(step=label, failureType=type(error).__name__))
                return None

        # A create can finish in the daemon even when its CLI reply times out.
        # Discovery never authorizes mutation: inspect_container must still bind
        # the exact ID, image, label and owned volume before stop/remove.
        if self.container is None and self.volume is not None:
            def discover():
                result = self.docker('find-owned-container', 'ps', '--all', '--no-trunc',
                                     '--filter', 'name=^/' + self.volume_name + '$', '--format', '{{.ID}}')
                ids = result.stdout.decode().splitlines()
                if len(ids) > 1 or any(not re.fullmatch(r'[0-9a-f]{64}', cid) for cid in ids):
                    raise ValueError('Ambiguous owned provider container discovery')
                if ids:
                    self.container = ids[0]
            attempt('discover-owned-container', discover)
        if self.container:
            container = attempt('inspect-owned-container', self.inspect_container)
            if container is not None and container['State']['Running']:
                def graceful_stop():
                    pid = self.execute('postmaster-before-stop', '/bin/cat', DATA + '/postmaster.pid', check=False)
                    if pid.returncode != 0:
                        if self.postmaster is not None:
                            raise ValueError('Previously verified postmaster lost its PID file')
                        return
                    actual = self.verify_postmaster()
                    if self.postmaster is not None and actual != self.postmaster:
                        raise ValueError('Owned postmaster identity changed before shutdown')
                    self.execute('graceful-owned-postgres-stop', '/usr/lib/postgresql/17/bin/pg_ctl', '-D', DATA, '-m', 'fast', '-w', '-t', '30', 'stop')
                    dead = self.execute('owned-postmaster-exited', '/bin/sh', '-c', 'kill -0 ' + str(actual), check=False)
                    if dead.returncode == 0:
                        raise RuntimeError('Owned postmaster survived graceful stop')
                    cleanup.update(serverStopped=True, ownedPostmasterExited=True, pgCtlStopReturncode=0)
                attempt('graceful-owned-postgres-stop', graceful_stop)
                attempt('collect-owned-server-log', lambda: self.execute('public-server-log', '/bin/cat', '/tmp/gdal4k-postgres.log', check=False))

                def stop_container():
                    self.inspect_container()
                    self.docker('stop-owned-container', 'stop', '--time', '20', self.container, timeout=40)
                attempt('stop-owned-container', stop_container)
            if container is not None:
                def remove_container():
                    stopped = self.inspect_container()
                    if stopped['State']['Running']:
                        raise RuntimeError('Exact owned provider container is still running')
                    cleanup.update(containerExited=True, containerExitCode=stopped['State']['ExitCode'], serverStopped=True)
                    self.docker('remove-owned-container', 'rm', self.container)
                    result = self.docker('confirm-owned-container-absent', 'ps', '--all', '--no-trunc',
                                         '--filter', 'id=' + self.container, '--format', '{{.ID}}')
                    if result.stdout.strip():
                        raise RuntimeError('Exact owned provider container survived removal')
                    cleanup['containerRemoved'] = True
                    if stopped['State']['ExitCode'] != 0:
                        raise RuntimeError('Owned provider container did not exit cleanly')
                attempt('remove-owned-container', remove_container)
        if self.volume:
            def remove_volume():
                result = self.docker('find-owned-volume', 'volume', 'ls', '--format', '{{.Name}}')
                if self.volume_name in result.stdout.decode().splitlines():
                    self.inspect_volume()
                    self.docker('remove-owned-volume', 'volume', 'rm', self.volume_name)
                result = self.docker('confirm-owned-volume-absent', 'volume', 'ls', '--format', '{{.Name}}')
                if self.volume_name in result.stdout.decode().splitlines():
                    raise RuntimeError('Exact owned provider volume survived removal')
                cleanup['volumeRemoved'] = True
            attempt('remove-owned-volume', remove_volume)
        if self.port is not None:
            cleanup['portClosed'] = port_closed(self.port)
            if not cleanup['portClosed']:
                errors.append(dict(step='verify-owned-port-closed', failureType='RuntimeError'))
        cleanup['ownedProviderStopped'] = not errors
        if errors:
            cleanup['failures'] = errors
            raise RuntimeError('Owned ARM PostGIS cleanup failed; inspect recorded cleanup steps')

    def remove_private_secrets(self):
        paths = [self.work / 'pgpass']
        private_output = self.work / 'private-command-output'
        if private_output.exists():
            if private_output.is_symlink() or not private_output.is_dir():
                raise ValueError('Private command output ownership changed')
            paths += list(private_output.iterdir())
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('Refusing unexpected private credential path')
            if path.exists():
                path.unlink()
        if private_output.exists():
            private_output.rmdir()
        self.receipt['cleanup']['privateCredentialFilesRemoved'] = True


@contextmanager
def fixture(output):
    if platform.system() != 'Linux' or platform.machine().lower() not in ['aarch64', 'arm64']:
        raise ValueError('Owned ARM provider may only run on actual Linux ARM64')
    provider = Provider(output)
    failure = None
    try:
        provider.prepare()
        descriptor = provider.start()
        yield descriptor
        provider.receipt['result'] = 'passed'
    except BaseException as error:
        failure = error
        provider.receipt['result'] = 'failed'
        provider.receipt['failureType'] = type(error).__name__
        raise
    finally:
        try:
            provider.cleanup()
        except BaseException as error:
            provider.receipt['result'] = 'failed'
            provider.receipt['cleanupFailureType'] = type(error).__name__
            if failure is not None:
                failure.add_note('Owned ARM provider cleanup also failed; inspect fixture receipt')
            else:
                raise
        finally:
            private_failure = None
            try:
                provider.remove_private_secrets()
            except BaseException as error:
                private_failure = error
                provider.receipt['result'] = 'failed'
                provider.receipt['privateCleanupFailureType'] = type(error).__name__
                if failure is not None:
                    failure.add_note('Owned ARM provider private credential cleanup also failed')
            provider.receipt['sourceUnchanged'] = all((ROOT / name).is_file() and sha(ROOT / name) == digest
                                                     for name, digest in provider.receipt['sourceSha256'].items())
            changed = not provider.receipt['sourceUnchanged']
            if changed:
                provider.receipt['result'] = 'failed'
                provider.receipt['sourceFailureType'] = 'ValueError'
            provider.receipt['completedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            write_json(provider.output / 'fixture.json', provider.receipt)
            if private_failure is not None and failure is None and provider.receipt.get('cleanupFailureType') is None:
                raise RuntimeError('Owned ARM provider private credential cleanup failed') from private_failure
            if changed and failure is None and provider.receipt.get('cleanupFailureType') is None:
                raise ValueError('Owned ARM provider source changed during execution')
