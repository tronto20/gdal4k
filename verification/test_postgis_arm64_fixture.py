"""Pure ARM provider lock, ownership and failure tests; never invoke Docker or PostgreSQL."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('postgis_arm64_fixture',
    Path(__file__).parent / 'driver-profile/postgis-arm64-fixture.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


def completed(stdout=b'', returncode=0, stderr=b''):
    return subprocess.CompletedProcess(['mocked-docker'], returncode, stdout, stderr)


class PostgisArm64FixtureTest(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(FIXTURE.subprocess, 'run', side_effect=AssertionError('Unmocked process execution'))
        guard.start()
        self.addCleanup(guard.stop)

    def lock(self):
        return json.loads(FIXTURE.LOCK.read_text(encoding='utf-8'))

    def test_full_lock_inventory_and_empty_conffiles_header_regression(self):
        lock = self.lock()
        rows = FIXTURE.validate_lock(lock).splitlines()
        self.assertEqual(144, len(lock['basePackages']))
        self.assertEqual(83, len(lock['packages']))
        self.assertEqual(227, len(rows))
        for name, version in [('debian-archive-keyring', '2023.3+deb12u2'),
                              ('libaudit-common', '1:3.0.9-1'), ('libsemanage-common', '3.4-1')]:
            self.assertIn(name + '\t' + version + '\tall', rows)
            broken = copy.deepcopy(lock)
            broken['basePackages'][name]['version'] += ' /etc/conffile 0123456789abcdef'
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'name/version/architecture'):
                FIXTURE.validate_lock(broken)

    def test_lock_rejects_unofficial_unpinned_and_ambiguous_inputs(self):
        changes = [lambda j: j.update(baseImage='docker.io/library/postgres:17'),
                   lambda j: j['packages'][0].update(url='https://other.invalid/example.deb'),
                   lambda j: j['packages'][0].update(url='https://user:secret@apt.postgresql.org/example.deb'),
                   lambda j: j['packages'][0].update(sha256='x' * 64),
                   lambda j: j['packages'][0].update(bytes=True),
                   lambda j: j['packages'][0].update(architecture='amd64'),
                   lambda j: j['packages'][0].update(version='1.0\nforged\tvalue'),
                   lambda j: j['packages'].append(copy.deepcopy(j['packages'][0])),
                   lambda j: j['providerBinaryPaths'].update(psql='usr/bin/foreign')]
        for change in changes:
            lock = self.lock()
            change(lock)
            with self.subTest(change=change), self.assertRaises(ValueError):
                FIXTURE.validate_lock(lock)

    def test_safe_dsn_rejects_injection_and_non_integer_ports(self):
        self.assertEqual('PG:host=127.0.0.1 port=54321 dbname=g4k_test user=g4k_test', FIXTURE.safe_dsn('g4k_test', 54321))
        for user, port in [('g4k_test password=x', 54321), ('g4k_test', True), ('g4k_test', 0), ('g4k_test', 65536)]:
            with self.subTest(user=user, port=port), self.assertRaises(ValueError):
                FIXTURE.safe_dsn(user, port)

    def test_non_arm_host_is_rejected_before_any_provider_or_process(self):
        with mock.patch.object(FIXTURE.platform, 'system', return_value='Darwin'), \
             mock.patch.object(FIXTURE.platform, 'machine', return_value='arm64'), \
             mock.patch.object(FIXTURE, 'Provider') as provider:
            with self.assertRaisesRegex(ValueError, 'actual Linux ARM64'):
                with FIXTURE.fixture('/unused'):
                    self.fail('Mac must never yield an ARM provider')
            provider.assert_not_called()

    def test_container_and_volume_identity_guard_all_mutation_dimensions(self):
        good = {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64,
                'Config': {'Labels': {FIXTURE.OWNER: 'token'}},
                'Mounts': [{'Type': 'volume', 'Name': 'owned', 'Destination': FIXTURE.DATA}]}
        self.assertIs(good, FIXTURE.owned_container(good, good['Id'], good['Image'], 'token', 'owned'))
        for field in ['id', 'image', 'label', 'volume', 'destination']:
            bad = copy.deepcopy(good)
            if field == 'id': bad['Id'] = 'c' * 64
            if field == 'image': bad['Image'] = 'sha256:' + 'c' * 64
            if field == 'label': bad['Config']['Labels'][FIXTURE.OWNER] = 'foreign'
            if field == 'volume': bad['Mounts'][0]['Name'] = 'foreign'
            if field == 'destination': bad['Mounts'][0]['Destination'] = '/foreign'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'unowned'):
                FIXTURE.owned_container(bad, good['Id'], good['Image'], 'token', 'owned')
        with self.assertRaisesRegex(ValueError, 'unowned'):
            FIXTURE.owned_volume({'Name': 'owned', 'Labels': {FIXTURE.OWNER: 'foreign'}}, 'owned', 'token')

    def test_commands_omit_private_stdin_redact_echo_and_record_timeout_or_oserror(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, work = root / 'public', root / 'private'
            output.mkdir(); work.mkdir()
            receipt = {'commands': [], 'cleanup': {}}
            commands = FIXTURE.Commands(output, work, receipt, 'fixture-private-secret')
            child = completed(b'echo fixture-private-secret', stderr=b'fixture-private-secret')
            with mock.patch.object(FIXTURE.subprocess, 'run', return_value=child) as run:
                commands.run('echo', ['docker', 'exec', '-i', 'owned'], input_data=b'fixture-private-secret')
            self.assertEqual(b'fixture-private-secret', run.call_args.kwargs['input'])
            record = receipt['commands'][0]
            self.assertTrue(record['privateStdinOmitted'])
            self.assertTrue(record['fixtureSecretRedacted'])
            retained = work / 'private-command-output/000-echo.stdout'
            self.assertEqual(0o600, retained.stat().st_mode & 0o777)
            for path in output.rglob('*'):
                if path.is_file(): self.assertNotIn(b'fixture-private-secret', path.read_bytes())
            for failure in [subprocess.TimeoutExpired(['docker'], 1, output=b'partial', stderr=b'fixture-private-secret'),
                            FileNotFoundError('missing docker fixture-private-secret')]:
                with self.subTest(failure=type(failure).__name__), \
                     mock.patch.object(FIXTURE.subprocess, 'run', side_effect=failure), \
                     self.assertRaisesRegex(RuntimeError, 'inspect commands/'):
                    commands.run('failed', ['docker'])
            self.assertTrue(receipt['commands'][1]['timedOut'])
            self.assertEqual('FileNotFoundError', receipt['commands'][2]['executionFailureType'])
            (work / 'pgpass').write_text('fixture-private-secret')
            provider = object.__new__(FIXTURE.Provider)
            provider.work, provider.receipt = work, receipt
            provider.remove_private_secrets()
            self.assertFalse((work / 'pgpass').exists())
            self.assertFalse((work / 'private-command-output').exists())
            self.assertTrue(receipt['cleanup']['privateCredentialFilesRemoved'])

    def cleanup_provider(self, *, foreign=False, stop_failure=False, list_failure=False):
        provider = object.__new__(FIXTURE.Provider)
        provider.container, provider.image, provider.volume = 'a' * 64, 'sha256:' + 'b' * 64, 'owned'
        provider.volume_name, provider.token, provider.port, provider.postmaster = 'owned', 'token', 54321, 42
        provider.receipt = {'cleanup': {}}
        state = {'running': True, 'container': True, 'volume': True}
        calls = []
        def inspect_container():
            calls.append('inspect')
            if foreign: raise ValueError('unowned container')
            return {'State': {'Running': state['running'], 'ExitCode': 0}}
        def execute(label, *args, **kwargs):
            calls.append(label)
            if label == 'graceful-owned-postgres-stop' and stop_failure: raise RuntimeError('stop failed')
            return completed(b'42\n' if label == 'postmaster-before-stop' else b'', 1 if label == 'owned-postmaster-exited' else 0)
        def docker(label, *args, **kwargs):
            calls.append(label)
            if label == 'stop-owned-container': state['running'] = False
            if label == 'remove-owned-container': state['container'] = False
            if label == 'confirm-owned-container-absent':
                if list_failure: raise RuntimeError('daemon read failed')
                return completed(provider.container.encode() if state['container'] else b'')
            if label in ['find-owned-volume', 'confirm-owned-volume-absent']:
                return completed(b'owned\n' if state['volume'] else b'')
            if label == 'remove-owned-volume':
                if state['container']: raise RuntimeError('volume is still in use')
                state['volume'] = False
            return completed()
        provider.inspect_container = mock.Mock(side_effect=inspect_container)
        provider.inspect_volume = mock.Mock(return_value={'Name': 'owned'})
        provider.execute = mock.Mock(side_effect=execute)
        provider.docker = mock.Mock(side_effect=docker)
        provider.verify_postmaster = mock.Mock(return_value=42)
        return provider, calls

    def test_normal_cleanup_proves_stop_remove_absence_and_closed_port(self):
        provider, calls = self.cleanup_provider()
        with mock.patch.object(FIXTURE, 'port_closed', return_value=True): provider.cleanup()
        for field in ['serverStopped', 'ownedPostmasterExited', 'containerExited', 'containerRemoved', 'volumeRemoved', 'portClosed', 'ownedProviderStopped']:
            self.assertTrue(provider.receipt['cleanup'][field], field)
        self.assertLess(calls.index('graceful-owned-postgres-stop'), calls.index('stop-owned-container'))
        self.assertLess(calls.index('remove-owned-container'), calls.index('remove-owned-volume'))

    def test_pg_stop_failure_still_cleans_owned_container_but_fails_acceptance(self):
        provider, calls = self.cleanup_provider(stop_failure=True)
        with mock.patch.object(FIXTURE, 'port_closed', return_value=True), self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            provider.cleanup()
        self.assertIn('stop-owned-container', calls)
        self.assertTrue(provider.receipt['cleanup']['containerRemoved'])
        self.assertTrue(provider.receipt['cleanup']['volumeRemoved'])
        self.assertFalse(provider.receipt['cleanup']['ownedProviderStopped'])

    def test_foreign_container_is_never_stopped_removed_or_volume_deleted(self):
        provider, calls = self.cleanup_provider(foreign=True)
        with mock.patch.object(FIXTURE, 'port_closed', return_value=True), self.assertRaises(RuntimeError): provider.cleanup()
        self.assertNotIn('stop-owned-container', calls)
        self.assertNotIn('remove-owned-container', calls)
        self.assertFalse(provider.receipt['cleanup']['ownedProviderStopped'])

    def test_daemon_failure_is_never_treated_as_successful_container_absence(self):
        provider, _ = self.cleanup_provider(list_failure=True)
        with mock.patch.object(FIXTURE, 'port_closed', return_value=True), self.assertRaises(RuntimeError): provider.cleanup()
        self.assertNotIn('containerRemoved', provider.receipt['cleanup'])

    def test_malformed_create_reply_recovers_exact_owned_name_before_cleanup(self):
        provider = object.__new__(FIXTURE.Provider)
        provider.container = provider.volume = provider.port = provider.postmaster = None
        provider.volume_name, provider.token, provider.image = 'owned', 'token', 'sha256:' + 'b' * 64
        provider.receipt = {'cleanup': {}}
        calls, present = [], {'container': True, 'volume': True}
        def docker(label, *args, **kwargs):
            calls.append(label)
            if label == 'create-owned-container': return completed(b'partial-daemon-reply')
            if label == 'find-owned-container': return completed(('a' * 64 + '\n').encode())
            if label == 'inspect-owned-container':
                return completed(json.dumps([{'Id': 'a' * 64, 'Image': provider.image,
                    'Config': {'Labels': {FIXTURE.OWNER: 'token'}},
                    'Mounts': [{'Type': 'volume', 'Name': 'owned', 'Destination': FIXTURE.DATA}],
                    'State': {'Running': False, 'ExitCode': 0}}]).encode())
            if label == 'inspect-owned-volume':
                return completed(json.dumps([{'Name': 'owned', 'Labels': {FIXTURE.OWNER: 'token'}}]).encode())
            if label == 'remove-owned-container': present['container'] = False
            if label == 'confirm-owned-container-absent': return completed(b'')
            if label in ['find-owned-volume', 'confirm-owned-volume-absent']:
                return completed(b'owned\n' if present['volume'] else b'')
            if label == 'remove-owned-volume':
                self.assertFalse(present['container'])
                present['volume'] = False
            return completed()
        provider.docker = mock.Mock(side_effect=docker)
        with self.assertRaisesRegex(ValueError, 'exact owned container ID'): provider.start()
        self.assertIsNone(provider.container)
        provider.cleanup()
        self.assertEqual('a' * 64, provider.container)
        self.assertIn('find-owned-container', calls)
        self.assertTrue(provider.receipt['cleanup']['containerRemoved'])
        self.assertTrue(provider.receipt['cleanup']['volumeRemoved'])

    def test_provider_binary_hash_must_match_actual_pinned_base_bytes(self):
        provider = object.__new__(FIXTURE.Provider)
        binary = bytearray(64)
        binary[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', binary, 18, 183)
        binary = bytes(binary)
        provider.expected_packages = 'package\t1\tarm64\n'
        provider.receipt = {'provider': {}}
        provider.lock = {'providerBinaryPaths': {'postgres': 'usr/lib/postgresql/17/bin/postgres'},
                         'providerBinaryInputs': {'postgres': {'bytes': 64, 'sha256': hashlib.sha256(binary).hexdigest()}}}
        provider.execute = mock.Mock(side_effect=[completed(provider.expected_packages.encode()), completed(binary)])
        provider.check_provider_bytes()
        self.assertEqual(183, provider.receipt['provider']['binaries']['postgres']['eMachine'])
        corrupted = binary[:-1] + b'\x01'
        provider.execute = mock.Mock(side_effect=[completed(provider.expected_packages.encode()), completed(corrupted)])
        with self.assertRaisesRegex(ValueError, 'actual pinned base bytes'): provider.check_provider_bytes()

    def test_start_queries_database_collation_and_keeps_password_out_of_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = object.__new__(FIXTURE.Provider)
            provider.work, provider.output = Path(temporary), Path(temporary) / 'public'
            provider.volume_name, provider.token, provider.image = 'owned', 'token', 'sha256:' + 'b' * 64
            provider.user, provider.password = 'g4k_test', 'private-password'
            provider.receipt = {'providerLockSha256': 'a' * 64}
            provider.lock = {'serverVersionNum': 170011, 'postgisVersion': '3.6.4'}
            provider.inspect_volume = mock.Mock()
            provider.inspect_container = mock.Mock(return_value={'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '54321'}]}}})
            provider.docker = mock.Mock(side_effect=lambda label, *a, **k: completed(('a' * 64).encode() if label == 'create-owned-container' else b''))
            provider.execute = mock.Mock(return_value=completed())
            provider.verify_postmaster = mock.Mock(return_value=42)
            provider.check_provider_bytes = mock.Mock()
            def sql(label, statement):
                if label == 'actual-server-identity':
                    self.assertNotIn("current_setting('lc_collate')", statement)
                    self.assertIn('SELECT datcollate FROM pg_database WHERE datname=current_database()', statement)
                    return json.dumps({'serverVersionNum': 170011, 'postgisVersion': '3.6.4', 'dataDirectory': FIXTURE.DATA,
                                       'encoding': 'UTF8', 'collation': 'C', 'listenAddresses': '*'})
                if label in ['owned-table-absent', 'owned-table-absent-after']: return 't'
                if label == 'point-new-connection-read': return json.dumps({'value': 42, 'x': 3, 'y': 4, 'srid': 4326, 'count': 1})
                return ''
            provider.sql = mock.Mock(side_effect=sql)
            descriptor = provider.start()
            self.assertNotIn('private-password', json.dumps(descriptor))
            self.assertEqual('PG:host=127.0.0.1 port=54321 dbname=g4k_test user=g4k_test', descriptor['dsn'])
            passfile = Path(descriptor['env']['PGPASSFILE'])
            self.assertEqual(0o600, passfile.stat().st_mode & 0o777)
            self.assertIn('private-password', passfile.read_text())
            self.assertTrue(provider.receipt['roundTrip']['tableDeletedAndAbsentNewConnection'])

    def test_startup_failure_and_caller_cancellation_cleanup_preserve_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            for original in [RuntimeError('startup failed'), KeyboardInterrupt()]:
                provider = mock.Mock()
                provider.receipt = {'commands': [], 'cleanup': {}, 'sourceSha256': {}}
                provider.output = Path(temporary)
                provider.start.side_effect = original if isinstance(original, RuntimeError) else None
                provider.cleanup.side_effect = ValueError('cleanup failed')
                with mock.patch.object(FIXTURE.platform, 'system', return_value='Linux'), \
                     mock.patch.object(FIXTURE.platform, 'machine', return_value='aarch64'), \
                     mock.patch.object(FIXTURE, 'Provider', return_value=provider), \
                     self.assertRaises(type(original)) as caught:
                    with FIXTURE.fixture(Path(temporary)):
                        raise original
                self.assertIs(original, caught.exception)
                provider.cleanup.assert_called_once()
                provider.remove_private_secrets.assert_called_once()
                self.assertEqual('failed', provider.receipt['result'])
                self.assertIn('cleanup also failed', original.__notes__[0])


if __name__ == '__main__':
    unittest.main()
