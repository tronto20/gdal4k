"""An owned, pinned native PostGIS test server; never a shared service or installation."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name('postgis-fixture-lock.json')


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')


def _claim_output(path):
    path = Path(path).absolute()
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise ValueError('Refusing a nonempty or symlink fixture output: ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verify_archive(path, lock):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a real pinned Postgres.app archive')
    if path.stat().st_size != lock['bytes'] or _sha(path) != lock['sha256']:
        raise ValueError('Postgres.app archive size/SHA256 does not match the lock')


def _safe_dsn(user, database, port):
    if not all(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value) for value in [user, database]):
        raise ValueError('Unsafe owned fixture database/user identifier')
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError('Invalid private fixture port')
    return f'PG:host=127.0.0.1 port={port} dbname={database} user={user}'


def _server_environment(binary, work):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PG', 'DYLD_')) and key not in
           ['LD_LIBRARY_PATH', 'PROJ_LIB', 'PROJ_DATA', 'GDAL_DATA']}
    env.update(PATH=str(binary) + ':/usr/bin:/bin:/usr/sbin:/sbin', LC_ALL='C', LANG='C',
               PROJ_NETWORK='OFF', PROJ_USER_WRITABLE_DIRECTORY=str(work / 'proj-user'))
    return env


class _Commands:
    def __init__(self, output, work, receipt):
        self.output, self.work, self.receipt = output, work, receipt
        self.private_values = []
        (output / 'commands').mkdir()

    def sanitize(self, data):
        for value in self.private_values:
            data = data.replace(value.encode('utf-8'), b'[fixture-secret-redacted]')
        return data

    def run(self, label, argv, env=None, timeout=60, check=True):
        label = re.sub(r'[^a-zA-Z0-9_-]', '-', label)
        prefix = f"commands/{len(self.receipt['commands']):02d}-{label}"
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        command = [str(value) for value in argv]
        record = dict(label=label, argv=[self.sanitize(value.encode('utf-8')).decode('utf-8') for value in command], cwd=str(self.work),
                      startedAt=started, stdout=prefix + '.stdout', stderr=prefix + '.stderr')
        try:
            result = subprocess.run(command, cwd=self.work, env=env,
                                    capture_output=True, timeout=timeout)
            stdout, stderr = result.stdout, result.stderr
            record['returncode'] = result.returncode
        except subprocess.TimeoutExpired as failure:
            stdout, stderr = failure.stdout or b'', failure.stderr or b''
            record.update(returncode=None, timedOut=True)
            result = None
        for key, data in [('stdout', stdout), ('stderr', stderr)]:
            public = self.sanitize(data)
            if public != data:
                private = self.work / 'private-command-output'
                private.mkdir(mode=0o700, exist_ok=True)
                raw = private / Path(record[key]).name
                raw.write_bytes(data)
                raw.chmod(0o600)
                record['fixtureSecretRedacted'] = True
            path = self.output / record[key]
            path.write_bytes(public)
            record[key + 'Sha256'] = _sha(path)
        record['completedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.receipt['commands'].append(record)
        _write_json(self.output / 'fixture.json', self.receipt)
        if result is None or (check and result.returncode):
            raise RuntimeError(f'Owned PostGIS command {label} failed; inspect {prefix}')
        return result


def _inventory(directory):
    inventory = {}
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            item = {'mode': stat.S_IMODE(path.lstat().st_mode)}
            if path.is_symlink():
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError('Provider symlink escapes the complete app: ' + str(path))
                item.update(kind='symlink', target=os.readlink(path))
            elif path.is_file():
                item.update(kind='file', bytes=path.stat().st_size, sha256=_sha(path))
            elif path.is_dir():
                item.update(kind='directory')
            else:
                raise ValueError('Unexpected provider filesystem entry: ' + str(path))
            inventory[path.relative_to(directory).as_posix()] = item
    return inventory


def _copy_provider(archive, work, lock, commands):
    mount = work / 'mount'
    mount.mkdir()
    try:
        commands.run('mount-readonly', ['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse',
                     '-noautoopen', '-mountpoint', mount, '-plist', archive], timeout=90)
        source = mount / lock['appRelativePath']
        if source.is_symlink() or not source.is_dir():
            raise ValueError('Pinned app is absent from the mounted archive')
        expected = _inventory(source)
        provider = work / 'provider' / lock['appRelativePath']
        provider.parent.mkdir()
        shutil.copytree(source, provider, symlinks=True)
        copied = _inventory(provider)
        if copied != expected:
            raise ValueError('Owned app copy differs from the mounted provider')
        _write_json(work / 'provider-inventory.json', copied)
        return provider
    finally:
        if os.path.ismount(mount):
            commands.run('detach-exact-mount', ['/usr/bin/hdiutil', 'detach', mount], timeout=90)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _port_closed(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(('127.0.0.1', port)) != 0


def _owned_pid(work, pgdata, token):
    marker = json.loads((work / 'ownership.json').read_text(encoding='utf-8'))
    if marker != {'token': token, 'pgdata': str(pgdata)} or not pgdata.is_relative_to(work):
        raise ValueError('Refusing an unowned PostgreSQL data directory')
    pidfile = pgdata / 'postmaster.pid'
    if not pidfile.exists():
        return None
    lines = pidfile.read_text(encoding='utf-8').splitlines()
    if len(lines) < 4 or not lines[0].isdigit() or Path(lines[1]).resolve() != pgdata.resolve():
        raise ValueError('Postmaster PID file does not identify the owned cluster')
    return int(lines[0])


class _Server:
    def __init__(self, work, binary, lock, commands, receipt):
        self.work, self.binary, self.lock = work, binary, lock
        self.commands, self.receipt = commands, receipt
        self.pgdata, self.token = work / 'pgdata', secrets.token_hex(16)
        _write_json(work / 'ownership.json', {'token': self.token, 'pgdata': str(self.pgdata)})
        private = work / 'private'
        private.mkdir(mode=0o700)
        self.password = secrets.token_hex(24)
        commands.private_values.append(self.password)
        self.password_file = private / 'initdb-password'
        self.password_file.write_text(self.password + '\n', encoding='utf-8')
        self.password_file.chmod(0o600)
        self.user = 'g4k_' + secrets.token_hex(8)
        self.database = self.user
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.dsn = _safe_dsn(self.user, self.database, self.port)
        self.passfile = private / 'pgpass'
        self.passfile.write_text(f'127.0.0.1:{self.port}:*:{self.user}:{self.password}\n', encoding='utf-8')
        self.passfile.chmod(0o600)
        self.env = _server_environment(binary, work)
        self.env.update(PGPASSFILE=str(self.passfile), PGCONNECT_TIMEOUT='10')
        self.pid = None
        receipt['connection'] = {'dsn': self.dsn, 'passwordIncluded': False}
        receipt['server'] = {'dataDirectory': str(self.pgdata), 'port': self.port,
                             'listenAddresses': ['127.0.0.1']}

    def run(self, label, argv, **kwargs):
        return self.commands.run(label, argv, env=self.env, **kwargs)

    def sql(self, label, sql, database=None):
        command = [self.binary / 'psql', '-X', '-h', '127.0.0.1', '-p', str(self.port),
                   '-U', self.user, '-d', database or self.database, '-v', 'ON_ERROR_STOP=1', '-At', '-c', sql]
        return self.run(label, command).stdout.decode('utf-8').strip()

    def start(self):
        self.run('initdb', [self.binary / 'initdb', '-D', self.pgdata, '--username=' + self.user,
                 '--encoding=UTF8', '--locale=C', '--auth-local=trust', '--auth-host=scram-sha-256',
                 '--pwfile=' + str(self.password_file)], timeout=90)
        options = shlex.join(['-h', '127.0.0.1', '-p', str(self.port), '-k', '',
                             '-c', 'ssl=off', '-c', 'logging_collector=off'])
        self.run('pg-ctl-start', [self.binary / 'pg_ctl', '-D', self.pgdata, '-l',
                 self.commands.output / 'server.log', '-w', '-t', '45', '-o', options, 'start'], timeout=60)
        self.pid = _owned_pid(self.work, self.pgdata, self.token)
        if not self.pid or not _alive(self.pid):
            raise RuntimeError('Owned server has no live postmaster')
        self.receipt['server']['postmasterPid'] = self.pid
        self.sql('create-owned-database', f'CREATE DATABASE {self.database} OWNER {self.user};', 'postgres')
        self.sql('create-postgis-extension', 'CREATE EXTENSION postgis;')
        observed = json.loads(self.sql('server-identity', "SELECT json_build_object("
            "'dataDirectory',current_setting('data_directory'),'port',current_setting('port'),"
            "'serverVersion',current_setting('server_version'),'serverVersionNum',current_setting('server_version_num'),"
            "'encoding',current_setting('server_encoding'),'collation',(SELECT datcollate FROM pg_database "
            "WHERE datname=current_database()),'version',version(),'postgisFullVersion',PostGIS_Full_Version(),"
            "'postgisVersion',(SELECT extversion FROM pg_extension WHERE extname='postgis'))::text;"))
        if (Path(observed['dataDirectory']).resolve() != self.pgdata.resolve()
                or observed['port'] != str(self.port) or observed['encoding'] != 'UTF8'
                or int(observed['serverVersionNum']) != self.lock['serverVersionNum']
                or observed['postgisVersion'] != self.lock['postgisVersion']
                or 'aarch64-apple-darwin' not in observed['version']):
            raise RuntimeError('Actual private PostgreSQL/PostGIS identity differs from its lock')
        self.receipt['server'].update({key: observed[key] for key in
            ['serverVersion', 'postgisVersion', 'version', 'postgisFullVersion', 'encoding', 'collation']})
        self.receipt['server']['serverVersionNum'] = int(observed['serverVersionNum'])
        self.receipt['startup'] = dict(completed=True, healthy=True, dataDirectoryVerified=True,
                                      postgisExtensionVerified=True)
        table = 'availability_' + secrets.token_hex(8)
        if self.sql('owned-table-absent', f"SELECT to_regclass('public.{table}') IS NULL;") != 't':
            raise RuntimeError('Refusing to replace an existing availability table')
        table_owned = False
        try:
            table_owned = True
            self.sql('point-write-commit', f'BEGIN; CREATE TABLE {table}(value integer NOT NULL, '
                     f'geom geometry(Point,4326)); INSERT INTO {table} VALUES(42,ST_SetSRID(ST_Point(3,4),4326)); COMMIT;')
            point = json.loads(self.sql('point-new-connection-read', "SELECT json_build_object('value',value,"
                f"'x',ST_X(geom),'y',ST_Y(geom),'srid',ST_SRID(geom),'count',(SELECT count(*) FROM {table}))::text FROM {table};"))
            if point != {'value': 42, 'x': 3, 'y': 4, 'srid': 4326, 'count': 1}:
                raise RuntimeError('Owned point/field persistence roundtrip failed')
            self.receipt['roundTrip'] = dict(table=table, values=point, newConnectionVerified=True)
        finally:
            if table_owned:
                self.sql('drop-exact-owned-table', f'DROP TABLE IF EXISTS {table};')
                absent = self.sql('owned-table-absent-after', f"SELECT to_regclass('public.{table}') IS NULL;")
                if absent != 't':
                    raise RuntimeError('Owned availability table remains after cleanup')
                self.receipt.setdefault('roundTrip', {})['tableDeletedAndAbsentNewConnection'] = True

    def stop(self):
        pid = _owned_pid(self.work, self.pgdata, self.token)
        if self.pid is not None and pid not in (None, self.pid):
            raise RuntimeError('Owned postmaster identity changed; refusing to stop a different process')
        returncode = None
        if pid is not None and _alive(pid):
            command = self.run('verify-owned-postmaster', ['/bin/ps', '-p', str(pid), '-o', 'command=']).stdout.decode('utf-8')
            if str(self.binary / 'postgres') not in command or str(self.pgdata) not in command:
                raise RuntimeError('Refusing pg_ctl stop for a process outside the owned provider/PGDATA')
            stopped = self.run('pg-ctl-stop', [self.binary / 'pg_ctl', '-D', self.pgdata,
                               '-m', 'fast', '-w', '-t', '45', 'stop'], timeout=60, check=False)
            returncode = stopped.returncode
        tracked_pid = self.pid if self.pid is not None else pid
        exited = tracked_pid is None or not _alive(tracked_pid)
        closed = _port_closed(self.port)
        stopped = exited and closed and not (self.pgdata / 'postmaster.pid').exists()
        self.receipt['cleanup'] = dict(serverStopped=stopped, portClosed=closed,
            ownedPostmasterExited=exited, pgCtlStopReturncode=returncode)
        if (not stopped or (returncode is not None and returncode != 0)
                or (self.pid is not None and returncode is None)):
            raise RuntimeError('Owned PostgreSQL shutdown is incomplete; retained fixture evidence')


@contextmanager
def _server_scope(server):
    error = None
    try:
        server.start()
        yield
    except BaseException as failure:
        error = failure
        raise
    finally:
        try:
            server.stop()
        except BaseException as cleanup:
            if error is None:
                raise
            error.add_note('Owned server cleanup also failed: ' + str(cleanup))


@contextmanager
def fixture(output: Path, target='macos-arm64', archive: Path | None = None):
    """Keep one private server alive around source/consumer/probe work; retain public receipts."""
    lock_file = json.loads(LOCK.read_text(encoding='utf-8'))
    if lock_file['schemaVersion'] != 1 or target not in lock_file['providers']:
        raise ValueError('Unverified PostGIS fixture target: ' + target)
    if target != 'macos-arm64' or (platform.system(), platform.machine().lower()) != ('Darwin', 'arm64'):
        raise ValueError('Run the PostGIS fixture on native macOS ARM64')
    lock = lock_file['providers'][target]
    output = _claim_output(output)
    if shutil.disk_usage(ROOT).free < lock['bytes'] + 1024 ** 3:
        raise RuntimeError('Insufficient space for an owned app, cluster and retained evidence')
    parent = ROOT / 'verification/build/postgis-fixtures' / target
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='owned-', dir=parent)).resolve()
    work.chmod(0o700)
    source_hashes = {p.relative_to(ROOT).as_posix(): _sha(p) for p in [Path(__file__).resolve(), LOCK]}
    receipt = dict(schemaVersion=1, target=target, result='pending', sourceSha256=source_hashes,
                   archive={key: lock[key] for key in ['url', 'sha256', 'bytes']},
                   startup=dict(completed=False, healthy=False, dataDirectoryVerified=False,
                                postgisExtensionVerified=False), cleanup={}, commands=[])
    commands = _Commands(output, work, receipt)
    error = None
    completed = False
    try:
        if archive is None:
            archive = work / 'Postgres.dmg'
            partial = work / 'Postgres.dmg.partial'
            with urllib.request.urlopen(lock['url'], timeout=90) as response, partial.open('xb') as destination:
                shutil.copyfileobj(response, destination, 1024 * 1024)
            _verify_archive(partial, lock)
            partial.rename(archive)
        else:
            archive = Path(archive).absolute()
        _verify_archive(archive, lock)
        provider = _copy_provider(archive, work, lock, commands)
        binary = provider / lock['binaryRelativePath']
        binaries = {}
        for name in ['postgres', 'initdb', 'pg_ctl', 'psql']:
            path = binary / name
            if not path.is_file() or path.is_symlink():
                raise ValueError('Missing owned native fixture binary: ' + name)
            arches = commands.run('architecture-' + name, ['/usr/bin/lipo', '-archs', path]).stdout.decode('ascii').split()
            if 'arm64' not in arches:
                raise ValueError('Fixture binary has no ARM64 slice: ' + name)
            binaries[name] = dict(path=str(path), sha256=_sha(path), architectures=arches)
        receipt['provider'] = dict(binaries=binaries, pgMajor=lock['pgMajor'])
        server = _Server(work, binary, lock, commands, receipt)
        with _server_scope(server):
            identity = dict(target=target, archiveSha256=lock['sha256'], safeDSN=server.dsn)
            _write_json(output / 'fixture.json', receipt)
            yield dict(dsn=server.dsn, env={'PGPASSFILE': str(server.passfile), 'PGCONNECT_TIMEOUT': '10'},
                       identity=identity, output=str(output))
        completed = True
    except BaseException as failure:
        error = failure
        receipt['failure'] = dict(type=type(failure).__name__,
            message=commands.sanitize(str(failure).encode('utf-8')).decode('utf-8'))
        raise
    finally:
        unchanged = all(_sha(ROOT / name) == digest for name, digest in source_hashes.items())
        receipt.update(result='passed' if completed and unchanged else 'failed',
                       completedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       sourceUnchanged=unchanged, privateDataRetained=True)
        _write_json(output / 'fixture.json', receipt)
        if not unchanged and error is None:
            raise RuntimeError('Fixture manager/lock changed during the owned server lifetime')
