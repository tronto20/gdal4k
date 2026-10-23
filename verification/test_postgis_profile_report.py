"""Validate owned PostGIS evidence with inert byte fixtures; no database is started."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    'postgis_profile_report', Path(__file__).parent / 'driver-profile/report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def executable(cpu=0x0100000C):
    return struct.pack('<IIIIIIII', 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0)


class OwnedPostgisReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / 'source'
        self.folder = self.snapshot / 'verification/results/native-postgis-profile'
        self.folder.mkdir(parents=True)
        self.manager = self.snapshot / 'verification/driver-profile/postgis-fixture.py'
        self.manager.parent.mkdir(parents=True)
        self.manager.write_text('# inert manager identity for evidence unit tests\n')
        self.lock = self.manager.with_name('postgis-fixture-lock.json')
        provider = dict(url='https://example.invalid/pinned-postgres.dmg', sha256='a' * 64,
                        bytes=1234, pgMajor=17, serverVersionNum=170011, postgisVersion='3.5.6',
                        appRelativePath='Postgres.app', binaryRelativePath='Contents/Versions/17/bin')
        self.lock.write_text(json.dumps(dict(schemaVersion=1, providers={'macos-arm64': provider})))
        self.bin = self.root / 'private/provider/Postgres.app/Contents/Versions/17/bin'
        self.bin.mkdir(parents=True)
        binaries = {}
        for name in ['postgres', 'initdb', 'pg_ctl', 'psql']:
            path = self.bin / name
            path.write_bytes(executable())
            binaries[name] = dict(path=str(path), sha256=digest(path), architectures=['arm64'])
        commands = []
        for index, argv in enumerate([[str(self.bin / 'initdb')],
                                      [str(self.bin / 'pg_ctl'), 'start'],
                                      [str(self.bin / 'psql'), '-c', 'SELECT postgis_lib_version()'],
                                      [str(self.bin / 'pg_ctl'), 'stop']]):
            command = dict(label=f'command-{index}', argv=argv, cwd=str(self.root), returncode=0,
                           startedAt='2026-10-23T00:00:00Z', completedAt='2026-10-23T00:00:01Z')
            for stream in ['stdout', 'stderr']:
                relative = f'commands/{index}.{stream}'
                path = self.folder / relative
                path.parent.mkdir(exist_ok=True)
                path.write_text('safe command output\n' if stream == 'stdout' else '')
                command[stream] = relative
                command[stream + 'Sha256'] = digest(path)
            commands.append(command)
        self.receipt = dict(
            schemaVersion=1, target='macos-arm64', result='passed',
            sourceSha256={'verification/driver-profile/postgis-fixture.py': digest(self.manager),
                          'verification/driver-profile/postgis-fixture-lock.json': digest(self.lock)},
            archive={key: provider[key] for key in ['url', 'sha256', 'bytes']},
            connection=dict(dsn='PG:host=127.0.0.1 port=51234 dbname=gdal4k user=gdal4k', passwordIncluded=False),
            provider=dict(pgMajor=17, binaries=binaries),
            server=dict(dataDirectory=str(self.root / 'private/pgdata'), port=51234,
                        listenAddresses=['127.0.0.1'], postmasterPid=12345,
                        serverVersionNum=170011, serverVersion='PostgreSQL 17.11', postgisVersion='3.5.6'),
            startup=dict(completed=True, healthy=True, dataDirectoryVerified=True, postgisExtensionVerified=True),
            cleanup=dict(serverStopped=True, portClosed=True, ownedPostmasterExited=True, pgCtlStopReturncode=0),
            commands=commands)
        self.identity = dict(profile='combined', target='macos-arm64',
                             postgresqlFixture=dict(target='macos-arm64', archiveSha256=provider['sha256'],
                                                    dsn=self.receipt['connection']['dsn']))
        self.write_identity()
        self.write_receipt()

    def write_identity(self):
        (self.snapshot.parent / 'inputs.json').write_text(json.dumps(self.identity), encoding='utf-8')

    def write_receipt(self):
        (self.folder / 'fixture.json').write_text(json.dumps(self.receipt))

    def check(self):
        self.write_receipt()
        return report.postgis_fixture_bindings(self.snapshot, 'macos-arm64', self.manager, self.lock)

    def test_terminal_receipt_binds_current_sources_binaries_and_public_logs(self):
        result = self.check()
        self.assertEqual(4, result['commandCount'])
        self.assertEqual(8, len(result['commandLogSha256']))
        self.assertTrue(result['cleanup']['ownedPostmasterExited'])
        self.assertEqual(['arm64'], result['provider']['binaries']['postgres']['architectures'])

    def test_terminal_receipt_must_match_intended_endpoint_target_and_archive(self):
        expected = self.identity['postgresqlFixture']
        for key, value in [('dsn', expected['dsn'].replace('51234', '51235')),
                           ('target', 'linux-amd64'), ('archiveSha256', 'b' * 64)]:
            with self.subTest(key=key):
                self.identity['postgresqlFixture'] = dict(expected, **{key: value})
                self.write_identity()
                with self.assertRaisesRegex(AssertionError, 'intended fixture identity'):
                    self.check()
        self.identity['postgresqlFixture'] = expected
        self.write_identity()
        # A valid terminal receipt from another owned local server is still stale
        # for this source snapshot, even if its own public fields agree internally.
        self.receipt['connection']['dsn'] = expected['dsn'].replace('51234', '51235')
        self.receipt['server']['port'] = 51235
        with self.assertRaisesRegex(AssertionError, 'intended fixture identity'):
            self.check()

    def test_missing_intended_fixture_identity_is_rejected(self):
        del self.identity['postgresqlFixture']
        self.write_identity()
        with self.assertRaisesRegex(AssertionError, 'Missing intended'):
            self.check()

    def test_requires_terminal_cleanup_and_actual_successful_stop(self):
        for field in ['serverStopped', 'portClosed', 'ownedPostmasterExited']:
            with self.subTest(field=field):
                self.receipt['cleanup'][field] = False
                with self.assertRaises(AssertionError):
                    self.check()
                self.receipt['cleanup'][field] = True
        self.receipt['commands'][-1]['returncode'] = 1
        with self.assertRaisesRegex(AssertionError, 'successful owned PostGIS command'):
            self.check()

    def test_rejects_password_remote_host_duplicate_keys_and_connection_options(self):
        good = self.receipt['connection']['dsn']
        for dsn in [good + ' password=private', good.replace('127.0.0.1', 'database.example'),
                    good + ' host=127.0.0.1', good + " options='-c search_path=public'",
                    good.replace('51234', '0')]:
            with self.subTest(dsn=dsn):
                self.receipt['connection']['dsn'] = dsn
                with self.assertRaises(AssertionError):
                    self.check()

    def test_rejects_mutated_source_archive_and_server_versions(self):
        for section, key, bad in [('archive', 'sha256', 'b' * 64),
                                  ('server', 'serverVersionNum', 160011),
                                  ('server', 'postgisVersion', '3.4.0')]:
            with self.subTest(section=section, key=key):
                original = self.receipt[section][key]
                self.receipt[section][key] = bad
                with self.assertRaises(AssertionError):
                    self.check()
                self.receipt[section][key] = original
        self.manager.write_text('# changed after fixture preparation\n')
        with self.assertRaisesRegex(AssertionError, 'source changed'):
            self.check()

    def test_rejects_changed_provider_bytes_and_architecture_receipts(self):
        path = self.bin / 'postgres'
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(AssertionError, 'binary changed'):
            self.check()
        self.receipt['provider']['binaries']['postgres']['sha256'] = digest(path)
        self.receipt['provider']['binaries']['postgres']['architectures'] = ['x86_64']
        with self.assertRaises(AssertionError):
            self.check()

    def test_rejects_tampered_or_credential_bearing_command_logs(self):
        command = self.receipt['commands'][0]
        path = self.folder / command['stdout']
        path.write_text('changed log\n')
        with self.assertRaisesRegex(AssertionError, 'log changed'):
            self.check()
        path.write_text('password=private\n')
        command['stdoutSha256'] = digest(path)
        with self.assertRaisesRegex(AssertionError, 'Unsanitized'):
            self.check()

    def test_rejects_command_logs_outside_public_evidence(self):
        self.receipt['commands'][0]['stdout'] = '../private/passfile'
        with self.assertRaises(AssertionError):
            self.check()

    def test_universal_macho_requires_real_matching_arm64_slice(self):
        path = self.root / 'universal'
        arm = executable()
        intel = executable(0x01000007)
        path.write_bytes(struct.pack('>II', 0xCAFEBABE, 2) +
                         struct.pack('>IIIII', 0x01000007, 0, 48, len(intel), 0) +
                         struct.pack('>IIIII', 0x0100000C, 0, 80, len(arm), 0) + intel + arm)
        self.assertEqual(['arm64', 'x86_64'], report.mach_o_architectures(path))
        path.write_bytes(path.read_bytes()[:-len(arm)] + intel)
        with self.assertRaisesRegex(AssertionError, 'slice disagrees'):
            report.mach_o_architectures(path)


if __name__ == '__main__':
    unittest.main()
