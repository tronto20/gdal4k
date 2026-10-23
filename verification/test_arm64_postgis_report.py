"""Isolated synthetic receipts test report bindings; no Docker, network or server executes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('arm64_postgis_report_test_subject',
    ROOT / 'verification/driver-profile/arm64-postgis-report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
OWNER = 'dev.gdal4k.postgis-fixture'
DATA = '/var/lib/postgresql/data'


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Scenario:
    """Small unit-only lock and real manager code copied to two isolated source roots."""
    def __init__(self, folder):
        self.root, self.snapshot = folder / 'source', folder / 'snapshot'
        self.folder = self.snapshot / 'verification/results/native-postgis-profile'
        (self.folder / 'commands').mkdir(parents=True)
        self.lock = json.loads((ROOT / REPORT.NAMES[1]).read_text())
        self.lock['basePackages'] = {'unit-base': {'version': '1.0', 'architecture': 'arm64'}}
        self.lock['packages'] = [{'name': 'unit-postgis', 'version': '1.0', 'architecture': 'arm64',
                                 'url': 'https://apt.postgresql.org/pub/repos/apt/pool/unit.deb',
                                 'bytes': 7, 'sha256': digest(b'unitdeb'), 'repository': 'unit-only', 'source': 'unit'}]
        self.binary = {}
        for index, name in enumerate(self.lock['providerBinaryPaths']):
            data = bytearray(64)
            data[:6] = b'\x7fELF\x02\x01'
            struct.pack_into('<H', data, 18, 183)
            data[-1] = index
            self.binary[name] = bytes(data)
        self.lock['providerBinaryInputs'] = {name: {'bytes': len(data), 'sha256': digest(data)}
                                             for name, data in self.binary.items()}
        for tree in [self.root, self.snapshot]:
            for name in REPORT.NAMES:
                target = tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if name.endswith('-lock.json'):
                    target.write_text(json.dumps(self.lock, indent=2) + '\n')
                else:
                    shutil.copyfile(ROOT / name, target)
        manager_spec = importlib.util.spec_from_file_location('arm64_postgis_unit_lock_manager', self.snapshot / REPORT.NAMES[0])
        manager = importlib.util.module_from_spec(manager_spec)
        manager_spec.loader.exec_module(manager)
        self.inventory = manager.validate_lock(self.lock).encode()
        self.cid, self.token = 'a' * 64, 'c' * 24
        self.image = 'sha256:' + 'b' * 64
        self.volume = 'gdal4k-pg-arm64-' + self.token
        self.dsn = 'PG:host=127.0.0.1 port=54321 dbname=g4k_unit user=g4k_unit'
        self.identity = {'serverVersionNum': 170011, 'serverVersion': 'PostgreSQL17.11 unit-only',
                         'postgisVersion': '3.6.4', 'postgisFullVersion': 'unit-only',
                         'dataDirectory': DATA, 'encoding': 'UTF8', 'collation': 'C', 'listenAddresses': '*'}
        self.values = {'value': 42, 'x': 3, 'y': 4, 'srid': 4326, 'count': 1}
        self.receipt = {'schemaVersion': 1, 'target': 'linux-arm64', 'result': 'passed', 'sourceUnchanged': True,
            'completedAt': '2026-10-23T00:01:00+00:00',
            'sourceSha256': {name: REPORT.sha(self.root / name) for name in REPORT.NAMES},
            'providerLockSha256': REPORT.sha(self.snapshot / REPORT.NAMES[1]),
            'provider': {'baseImage': self.lock['baseImage'], 'baseConfig': self.lock['baseConfigDigest'],
                         'imageId': self.image, 'dockerServer': {'Os': 'linux', 'Arch': 'arm64'},
                         'expectedPackageInventorySha256': digest(self.inventory), 'actualPackageInventorySha256': digest(self.inventory),
                         'packageCount': 2, 'packages': [dict(self.lock['packages'][0], retainedPath='/unit-private/debs/'+digest(b'unitdeb')+'.deb')],
                         'binaries': {name: dict(self.lock['providerBinaryInputs'][name],
                             path='/'+self.lock['providerBinaryPaths'][name], architecture='AArch64', eMachine=183) for name in self.binary}},
            'connection': {'dsn': self.dsn, 'passwordIncluded': False, 'hostLoopbackMapping': [{'HostIp': '127.0.0.1', 'HostPort': '54321'}]},
            'server': dict(self.identity, postmasterPid=42, containerListenAddresses=['*']),
            'startup': dict(completed=True, healthy=True, dataDirectoryVerified=True, postgisExtensionVerified=True),
            'roundTrip': {'table': 'availability_'+'d'*16, 'values': self.values,
                          'newConnectionVerified': True, 'tableDeletedAndAbsentNewConnection': True},
            'cleanup': dict(serverStopped=True, ownedPostmasterExited=True, containerExited=True, containerRemoved=True,
                            volumeRemoved=True, portClosed=True, ownedProviderStopped=True, privateCredentialFilesRemoved=True,
                            pgCtlStopReturncode=0, containerExitCode=0),
            'containerId': self.cid, 'volumeName': self.volume, 'commands': []}
        self.command('docker-context', ['context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'], json.dumps('unix:///var/run/docker.sock').encode())
        self.command('docker-server', ['version', '--format', '{{json .Server}}'], json.dumps(self.receipt['provider']['dockerServer']).encode())
        self.command('pull-official-base', ['pull', '--platform', 'linux/arm64', self.lock['baseImage']])
        self.command('inspect-official-base', ['image', 'inspect', self.lock['baseImage']], json.dumps([{'Id': self.lock['baseConfigDigest'], 'Os': 'linux', 'Architecture': 'arm64'}]).encode())
        self.command('build-offline-locked-provider', ['build', '--network=none', '--pull=false', '--platform=linux/arm64',
                     '--label', OWNER+'='+self.token, '-t', 'gdal4k-arm64-postgis:'+self.token, '/unit-private/context'])
        self.command('inspect-built-provider', ['image', 'inspect', 'gdal4k-arm64-postgis:'+self.token], json.dumps([{
                     'Id': self.image, 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'Labels': {OWNER: self.token}}}]).encode())
        self.command('create-owned-volume', ['volume', 'create', '--label', OWNER+'='+self.token, self.volume], (self.volume+'\n').encode())
        self.command('inspect-owned-volume', ['volume', 'inspect', self.volume],
                     json.dumps([{'Name': self.volume, 'Labels': {OWNER: self.token}}]).encode())
        self.command('create-owned-container', ['create', '--name', self.volume, '--label', OWNER+'='+self.token,
                     '--mount', 'type=volume,src='+self.volume+',dst='+DATA, '--tmpfs', '/run/gdal4k-private:rw,noexec,nosuid,mode=0700,uid=999,gid=999',
                     '--publish', '127.0.0.1::5432', '--entrypoint', '/bin/sh', self.image, '-c', 'trap "exit 0" INT TERM; while :; do sleep 1; done'], (self.cid+'\n').encode())
        container = {'Id': self.cid, 'Image': self.image, 'Config': {'Labels': {OWNER: self.token}},
                     'Mounts': [{'Type': 'volume', 'Name': self.volume, 'Destination': DATA}], 'State': {'Running': False, 'ExitCode': 0},
                     'NetworkSettings': {'Ports': {'5432/tcp': self.receipt['connection']['hostLoopbackMapping']}}}
        self.command('inspect-owned-container', ['inspect', self.cid], json.dumps([container]).encode())
        self.command('start-owned-container', ['start', self.cid])
        container['State']['Running'] = True
        self.command('inspect-owned-container', ['inspect', self.cid], json.dumps([container]).encode())
        self.command('launch-owned-postgres', ['exec', '-d', '--user', 'postgres', '--env', 'POSTGRES_USER=g4k_unit',
                     '--env', 'POSTGRES_DB=g4k_unit', '--env', 'POSTGRES_PASSWORD_FILE=/run/gdal4k-private/password',
                     '--env', 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C', '--env', 'PGDATA='+DATA,
                     self.cid, '/bin/sh', '-c', "exec /usr/local/bin/docker-entrypoint.sh postgres -c listen_addresses='*' -c ssl=off > /tmp/gdal4k-postgres.log 2>&1"])
        self.command('wait-owned-postgres', ['exec', '--user', 'postgres', self.cid, '/usr/lib/postgresql/17/bin/pg_isready', '-h', '127.0.0.1', '-p', '5432'])
        sql = ['exec', '--user', 'postgres', '--env', 'PGPASSFILE=/run/gdal4k-private/pgpass', self.cid,
               '/usr/lib/postgresql/17/bin/psql', '-XAt', '--no-password', '--set=ON_ERROR_STOP=1',
               '--host=127.0.0.1', '--port=5432', '--username=g4k_unit', '--dbname=g4k_unit', '--command']
        self.command('actual-server-identity', sql+['SELECT identity unit-only'], json.dumps(self.identity).encode())
        pidfile = ('42\n'+DATA+'\n1792713600\n5432\n/var/run/postgresql\n*\n0 0\nready\n').encode()
        self.command('owned-postmaster-pid', ['exec', '--user', 'postgres', self.cid, '/bin/cat', DATA+'/postmaster.pid'],
                     pidfile)
        self.command('owned-postmaster-command', ['exec', '--user', 'postgres', self.cid, '/bin/cat', '/proc/42/cmdline'],
                     b'postgres\0-c\0listen_addresses=*\0-c\0ssl=off\0')
        self.command('point-write-commit', sql+['BEGIN; CREATE TABLE availability_'+'d'*16+'(value integer); INSERT INTO availability_'+'d'*16+' VALUES(42); COMMIT;'])
        self.command('point-new-connection-read', sql+['SELECT unit-only'], json.dumps(self.values).encode())
        self.command('drop-exact-owned-table', sql+['DROP TABLE IF EXISTS availability_'+'d'*16+';'])
        self.command('owned-table-absent-after', sql+["SELECT to_regclass('public.availability_"+'d'*16+"') IS NULL;"], b't\n')
        self.command('actual-package-inventory', ['exec', '--user', 'postgres', self.cid, '/bin/cat', '/locked-actual-packages.tsv'], self.inventory)
        for name, data in self.binary.items():
            self.command('actual-binary-'+name, ['exec', '--user', 'postgres', self.cid, '/bin/cat', '/'+self.lock['providerBinaryPaths'][name]], data)
        self.command('postmaster-before-stop', ['exec', '--user', 'postgres', self.cid, '/bin/cat', DATA+'/postmaster.pid'], pidfile)
        self.command('owned-postmaster-pid', ['exec', '--user', 'postgres', self.cid, '/bin/cat', DATA+'/postmaster.pid'], pidfile)
        self.command('owned-postmaster-command', ['exec', '--user', 'postgres', self.cid, '/bin/cat', '/proc/42/cmdline'],
                     b'postgres\0-c\0listen_addresses=*\0-c\0ssl=off\0')
        self.command('graceful-owned-postgres-stop', ['exec', '--user', 'postgres', self.cid, '/usr/lib/postgresql/17/bin/pg_ctl', '-D', DATA, '-m', 'fast', '-w', '-t', '30', 'stop'])
        self.command('owned-postmaster-exited', ['exec', '--user', 'postgres', self.cid, '/bin/sh', '-c', 'kill -0 42'], code=1)
        self.command('stop-owned-container', ['stop', '--time', '20', self.cid])
        container['State']['Running'] = False
        self.command('inspect-owned-container', ['inspect', self.cid], json.dumps([container]).encode())
        self.command('remove-owned-container', ['rm', self.cid])
        self.command('confirm-owned-container-absent', ['ps', '--all', '--no-trunc', '--filter', 'id='+self.cid, '--format', '{{.ID}}'])
        self.command('remove-owned-volume', ['volume', 'rm', self.volume])
        self.command('confirm-owned-volume-absent', ['volume', 'ls', '--format', '{{.Name}}'])
        config = self.snapshot / 'verification/results/arm64-runtime-configuration.json'
        config.write_text(json.dumps({'postgresqlDsn': self.dsn, 'target': 'linux-arm64'}))
        self.save()

    def command(self, label, argv, stdout=b'', stderr=b'', code=0):
        index = len(self.receipt['commands'])
        prefix = f'commands/{index:03d}-{label}'
        record = {'label': label, 'argv': ['docker', *argv], 'cwd': '/unit-private', 'returncode': code,
                  'startedAt': f'2026-10-23T00:00:{index:02d}+00:00', 'completedAt': f'2026-10-23T00:00:{index:02d}+00:00'}
        for stream, data in [('stdout', stdout), ('stderr', stderr)]:
            record[stream] = prefix+'.'+stream
            (self.folder / record[stream]).write_bytes(data)
            record[stream+'Sha256'] = digest(data)
        self.receipt['commands'].append(record)

    def set_stdout(self, label, value):
        command = next(c for c in self.receipt['commands'] if c['label'] == label)
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        (self.folder / command['stdout']).write_bytes(data)
        command['stdoutSha256'] = digest(data)

    def save(self):
        (self.folder / 'fixture.json').write_text(json.dumps(self.receipt, indent=2)+'\n')

    def check(self):
        self.save()
        with mock.patch.object(REPORT, 'ROOT', self.root):
            return REPORT.bindings(self.snapshot, 'linux-arm64')


class Arm64PostgisReportTest(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(subprocess, 'run', side_effect=AssertionError('No process execution in report unit tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def scenario(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Scenario(Path(temporary.name))

    def test_positive_copied_real_manager_with_unit_only_lock_and_bound_logs(self):
        s = self.scenario()
        result = s.check()
        self.assertEqual(s.receipt['providerLockSha256'], result['providerLockSha256'])
        self.assertEqual(2, result['provider']['packageCount'])
        self.assertEqual(len(s.receipt['commands'])*2, len(result['commandLogSha256']))

    def test_result_lifecycle_and_cleanup_incompleteness_fail_closed(self):
        for field in ['result', 'sourceUnchanged', 'startup', 'cleanup', 'private', 'exit']:
            with self.subTest(field=field):
                s = self.scenario()
                if field == 'result': s.receipt['result'] = 'failed'
                if field == 'sourceUnchanged': s.receipt['sourceUnchanged'] = False
                if field == 'startup': s.receipt['startup']['healthy'] = False
                if field == 'cleanup': s.receipt['cleanup']['volumeRemoved'] = False
                if field == 'private': s.receipt['cleanup']['privateCredentialFilesRemoved'] = False
                if field == 'exit': s.receipt['cleanup']['containerExitCode'] = 137
                with self.assertRaises(AssertionError): s.check()

    def test_current_source_and_snapshot_source_must_both_match_receipt(self):
        for tree in ['root', 'snapshot']:
            with self.subTest(tree=tree):
                s = self.scenario()
                path = getattr(s, tree) / REPORT.NAMES[0]
                path.write_text(path.read_text()+'\n# changed\n')
                with self.assertRaisesRegex(AssertionError, 'source changed'): s.check()

    def test_modified_raw_logs_fail_even_if_receipt_claims_success(self):
        s = self.scenario()
        command = next(c for c in s.receipt['commands'] if c['label'] == 'actual-server-identity')
        (s.folder / command['stdout']).write_bytes(b'modified')
        with self.assertRaisesRegex(AssertionError, 'public log changed'): s.check()

    def test_rehashed_package_binary_and_point_log_semantics_must_still_agree(self):
        for label, data in [('actual-package-inventory', b'unlocked\t1\tarm64\n'),
                            ('actual-binary-postgres', b'wrong binary'),
                            ('point-new-connection-read', {'value': 99, 'x': 3, 'y': 4, 'srid': 4326, 'count': 1}),
                            ('owned-table-absent-after', b'f\n'),
                            ('confirm-owned-container-absent', b'container-remains\n')]:
            with self.subTest(label=label):
                s = self.scenario()
                s.set_stdout(label, data)
                with self.assertRaises(AssertionError): s.check()

    def test_unbound_private_file_and_escaping_or_symlinked_log_rejected(self):
        s = self.scenario()
        (s.folder / 'pgpass').write_text('private data must not be an artifact')
        with self.assertRaisesRegex(AssertionError, 'Unexpected private'): s.check()
        for kind in ['absolute', 'parent', 'symlink']:
            with self.subTest(kind=kind):
                s = self.scenario()
                command = s.receipt['commands'][0]
                if kind == 'absolute': command['stdout'] = str(s.folder / command['stdout'])
                if kind == 'parent': command['stdout'] = 'commands/../outside.stdout'
                if kind == 'symlink':
                    original = s.folder / command['stdout']
                    outside = s.root / 'external-log'
                    shutil.copyfile(original, outside)
                    original.unlink(); original.symlink_to(outside)
                with self.assertRaises(AssertionError): s.check()

    def test_lifecycle_command_failure_or_missing_command_rejected(self):
        for change in ['missing', 'timeout', 'failed']:
            with self.subTest(change=change):
                s = self.scenario()
                command = next(c for c in s.receipt['commands'] if c['label'] == 'graceful-owned-postgres-stop')
                if change == 'missing':
                    for stream in ['stdout', 'stderr']: (s.folder / command[stream]).unlink()
                    s.receipt['commands'].remove(command)
                if change == 'timeout': command['timedOut'] = True
                if change == 'failed': command['returncode'] = 1
                with self.assertRaises(AssertionError): s.check()

    def test_runtime_endpoint_and_loopback_mapping_cannot_drift(self):
        s = self.scenario()
        s.receipt['connection']['hostLoopbackMapping'][0]['HostIp'] = '0.0.0.0'
        with self.assertRaises(AssertionError): s.check()
        s = self.scenario()
        s.receipt['connection']['dsn'] = s.dsn.replace('54321', '54322')
        s.receipt['connection']['hostLoopbackMapping'][0]['HostPort'] = '54322'
        with self.assertRaises(AssertionError): s.check()

    def test_raw_docker_server_and_base_image_identity_are_bound_to_provider(self):
        for label, data in [('docker-server', {'Os': 'linux', 'Arch': 'amd64'}),
                            ('inspect-official-base', [{'Id': 'sha256:'+'f'*64, 'Os': 'linux', 'Architecture': 'arm64'}]),
                            ('inspect-built-provider', [{'Id': 'sha256:'+'f'*64, 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'Labels': {OWNER: 'c'*24}}}])]:
            with self.subTest(label=label):
                s = self.scenario()
                s.set_stdout(label, data)
                with self.assertRaises(AssertionError): s.check()

    def test_created_container_and_volume_ids_must_match_terminal_receipt(self):
        for field in ['containerId', 'volumeName']:
            with self.subTest(field=field):
                s = self.scenario()
                s.receipt[field] = 'f'*64 if field == 'containerId' else 'gdal4k-pg-arm64-'+'e'*24
                with self.assertRaises(AssertionError): s.check()

    def test_terminal_raw_container_state_must_support_cleanup_claim(self):
        for field, value in [('Running', True), ('ExitCode', 137)]:
            with self.subTest(field=field):
                s = self.scenario()
                command = [c for c in s.receipt['commands'] if c['label'] == 'inspect-owned-container'][-1]
                path = s.folder / command['stdout']
                raw = json.loads(path.read_bytes())
                raw[0]['State'][field] = value
                data = json.dumps(raw).encode()
                path.write_bytes(data)
                command['stdoutSha256'] = digest(data)
                with self.assertRaises(AssertionError): s.check()

    def test_postmaster_identity_and_shutdown_argv_bind_to_actual_pidfile(self):
        for field in ['receiptPid', 'pidfileData', 'cmdlineName', 'cmdlineData', 'pgctlData', 'killPid']:
            with self.subTest(field=field):
                s = self.scenario()
                if field == 'receiptPid': s.receipt['server']['postmasterPid'] = 99
                if field == 'pidfileData': s.set_stdout('owned-postmaster-pid', b'42\n/foreign/data\n0\n5432\n')
                if field == 'cmdlineName': s.set_stdout('owned-postmaster-command', b'foreign-server\0')
                if field == 'cmdlineData': s.set_stdout('owned-postmaster-command', b'postgres\0-D\0/foreign/data\0')
                if field == 'pgctlData':
                    command = next(c for c in s.receipt['commands'] if c['label'] == 'graceful-owned-postgres-stop')
                    command['argv'] = [v.replace(DATA, '/foreign/data') for v in command['argv']]
                if field == 'killPid':
                    command = next(c for c in s.receipt['commands'] if c['label'] == 'owned-postmaster-exited')
                    command['argv'][-1] = 'kill -0 99'
                with self.assertRaises((AssertionError, ValueError)): s.check()


if __name__ == '__main__':
    unittest.main()
