"""Pure fixture ownership/validation tests; never execute provider binaries or a database."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest import mock

SPEC = importlib.util.spec_from_file_location('postgis_fixture', Path(__file__).parent / 'driver-profile/postgis-fixture.py')
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class PostgisFixtureTest(unittest.TestCase):
    def test_wrong_archive_identity_rejected_before_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'provider.dmg'
            archive.write_bytes(b'wrong provider')
            with self.assertRaisesRegex(ValueError, 'size/SHA256'):
                FIXTURE._verify_archive(archive, {'bytes': archive.stat().st_size, 'sha256': '0' * 64})

    def test_private_dsn_does_not_accept_injected_parameters(self):
        self.assertEqual('PG:host=127.0.0.1 port=55432 dbname=g4k_db user=g4k_user',
                         FIXTURE._safe_dsn('g4k_user', 'g4k_db', 55432))
        for user, database, port in [('g4k_user password=oops', 'g4k_db', 55432),
                                     ('g4k_user', "db' options=oops", 55432),
                                     ('g4k_user', 'g4k_db', 0), ('g4k_user', 'g4k_db', True)]:
            with self.subTest(user=user, database=database, port=port), self.assertRaises(ValueError):
                FIXTURE._safe_dsn(user, database, port)

    def test_existing_output_and_escaping_provider_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'existing').mkdir()
            (root / 'existing/evidence').write_text('preserve')
            with self.assertRaisesRegex(ValueError, 'nonempty'):
                FIXTURE._claim_output(root / 'existing')
            (root / 'app').mkdir()
            (root / 'app/escape').symlink_to(root / 'existing')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                FIXTURE._inventory(root / 'app')
            self.assertEqual('preserve', (root / 'existing/evidence').read_text())

    def test_foreign_postmaster_data_directory_is_never_owned(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            pgdata = work / 'pgdata'
            pgdata.mkdir()
            (work / 'ownership.json').write_text(json.dumps({'token': 'owned-token', 'pgdata': str(pgdata)}))
            (pgdata / 'postmaster.pid').write_text('12345\n/foreign/cluster\n0\n5432\n')
            with self.assertRaisesRegex(ValueError, 'owned cluster'):
                FIXTURE._owned_pid(work, pgdata, 'owned-token')
            with self.assertRaisesRegex(ValueError, 'unowned'):
                FIXTURE._owned_pid(work, pgdata, 'foreign-token')

    def test_server_environment_is_private_and_does_not_change_parent(self):
        inherited = {'PGPASSWORD': 'unrelated-parent-secret', 'PGHOST': 'shared-host',
                     'DYLD_LIBRARY_PATH': '/unrelated/server-libraries', 'GDAL_DATA': '/unrelated/data'}
        with mock.patch.dict(FIXTURE.os.environ, inherited):
            env = FIXTURE._server_environment(Path('/owned/provider/bin'), Path('/owned/work'))
            self.assertFalse(any(key.startswith(('PG', 'DYLD_')) for key in env))
            self.assertNotIn('GDAL_DATA', env)
            self.assertEqual('/owned/work/proj-user', env['PROJ_USER_WRITABLE_DIRECTORY'])
            self.assertEqual('OFF', env['PROJ_NETWORK'])
            self.assertEqual('shared-host', FIXTURE.os.environ['PGHOST'])
            self.assertEqual('/unrelated/server-libraries', FIXTURE.os.environ['DYLD_LIBRARY_PATH'])

    def test_unexpected_child_secret_echo_stays_out_of_public_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'private-work'
            output = Path(temporary) / 'public'
            work.mkdir(); output.mkdir()
            receipt = {'commands': []}
            commands = FIXTURE._Commands(output, work, receipt)
            commands.private_values.append('fixture-private-password')
            child = subprocess.CompletedProcess(['private-child'], 0,
                stdout=b'child echoed fixture-private-password', stderr=b'fixture-private-password')
            with mock.patch.object(FIXTURE.subprocess, 'run', return_value=child):
                commands.run('unexpected-echo', ['private-child'])
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'fixture-private-password', path.read_bytes())
            self.assertTrue(receipt['commands'][0]['fixtureSecretRedacted'])
            retained = work / 'private-command-output/00-unexpected-echo.stdout'
            self.assertIn(b'fixture-private-password', retained.read_bytes())
            self.assertEqual(0o600, retained.stat().st_mode & 0o777)

    def test_start_failure_and_caller_cancellation_both_attempt_owned_stop(self):
        class OwnedServer:
            def __init__(self, fail=False):
                self.fail, self.stopped = fail, False

            def start(self):
                if self.fail:
                    raise RuntimeError('owned startup failed')

            def stop(self):
                self.stopped = True

        startup = OwnedServer(fail=True)
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            with FIXTURE._server_scope(startup):
                self.fail('startup failure must not yield')
        self.assertTrue(startup.stopped)
        active = OwnedServer()
        with self.assertRaises(KeyboardInterrupt):
            with FIXTURE._server_scope(active):
                raise KeyboardInterrupt()
        self.assertTrue(active.stopped)

    def test_cleanup_failure_preserves_original_error(self):
        class OwnedServer:
            def start(self):
                raise RuntimeError('original startup failure')

            def stop(self):
                raise ValueError('owned stop failed')

        with self.assertRaisesRegex(RuntimeError, 'original startup failure') as caught:
            with FIXTURE._server_scope(OwnedServer()):
                self.fail('unexpected yield')
        self.assertIn('owned stop failed', caught.exception.__notes__[0])


if __name__ == '__main__':
    unittest.main()
