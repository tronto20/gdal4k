"""Exercise profile isolation against a real temporary Git checkout, without installing dependencies."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('driver_profile', Path(__file__).resolve().parents[1] / 'gdal4k-engine/tools/driver-profile.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report_spec = importlib.util.spec_from_file_location('driver_profile_report', Path(__file__).resolve().parent / 'driver-profile/report.py')
report = importlib.util.module_from_spec(report_spec)
report_spec.loader.exec_module(report)


def checkout(root):
    files = {'gradle.properties': 'gdal4kVersion=0.1.0-SNAPSHOT\n',
             'build.gradle.kts': '// default build\n',
             'gdal4k-engine/engine/vcpkg.json': json.dumps({'dependencies': [{'name': 'gdal', 'default-features': False, 'features': ['geos']}]}),
             'verification/consumer.py': "for test_name in ['DatasetApiTest']: pass\n",
             'verification/driver-profile/DriverProfileApiTest.kt': '@PROFILE@ @HDF5_PATH@ @HTTP_BASE@ @HTTPS_BASE@ @POSTGRESQL_DSN@',
             'verification/driver-profile/NativeHttpProfileTest.kt': '@HTTP_BASE@ @HTTPS_BASE@ @UNTRUSTED_BASE@ @WRONG_NAME_BASE@',
             'verification/driver-profile/CombinedDriverMemoryProbe.kt': '// combined probe template\n',
             'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/Existing.kt': '// API test\n',
             'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/ExistingProbe.kt': '// existing JVM test\n',
             '.gitignore': 'build/\n'}
    for name, text in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    subprocess.run(['git', 'init', '-q', root], check=True)
    subprocess.run(['git', 'add', '--', *files], cwd=root, check=True)
    return {name: (root / name).read_bytes() for name in files}


HTTP_FIXTURE = {'urls': {'http': 'http://127.0.0.1:10001', 'https': 'https://127.0.0.1:10002',
                         'untrusted': 'https://127.0.0.1:10003', 'wrong-name': 'https://127.0.0.1:10004'}}
POSTGIS_FIXTURE = {'dsn': 'PG:host=127.0.0.1 port=16432 dbname=gdal4k user=gdal4k',
                   'identity': {'target': 'macos-arm64', 'archiveSha256': 'a' * 64,
                                'safeDSN': 'PG:host=127.0.0.1 port=16432 dbname=gdal4k user=gdal4k'},
                   'env': {'PGPASSFILE': '/private/owned-fixture/passfile'}}

class ProfileIsolationTest(unittest.TestCase):
    def test_isolated_sources_version_and_tamper_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = checkout(root)
            scientific, data = module.prepare(root, 'scientific', 'linux-amd64')
            parquet, other = module.prepare(root, 'parquet', 'linux-amd64')
            fixture = HTTP_FIXTURE
            http, http_identity = module.prepare(root, 'http', 'linux-amd64', fixture)
            http_manifest = json.loads((http / 'gdal4k-engine/engine/vcpkg.json').read_text())
            self.assertEqual(['curl', 'geos'], http_manifest['dependencies'][0]['features'])
            self.assertIn({'name': 'curl', 'default-features': False, 'features': ['ssl', 'c-ares']}, http_manifest['dependencies'])
            self.assertEqual(fixture['urls'], http_identity['httpFixtureUrls'])
            self.assertNotIn('@HTTP_BASE@', (http / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt').read_text())
            self.assertIn("'NativeHttpProfileTest'", (http / 'verification/consumer.py').read_text())
            self.assertNotEqual(scientific, parquet)
            self.assertEqual('0.1.0-scientific-SNAPSHOT', data['version'])
            self.assertNotEqual(data['version'], other['version'])
            features = json.loads((scientific / 'gdal4k-engine/engine/vcpkg.json').read_text())['dependencies'][0]['features']
            self.assertEqual(['geos', 'hdf5', 'netcdf'], features)
            dependencies = json.loads((scientific / 'gdal4k-engine/engine/vcpkg.json').read_text())['dependencies']
            self.assertIn({'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']}, dependencies)
            self.assertTrue((scientific / 'verification/consumer.py').read_text().startswith("for test_name in ['DriverProfileApiTest'"))
            combined, combined_identity = module.prepare(root, 'combined', 'linux-amd64', fixture)
            union = json.loads((combined / 'gdal4k-engine/engine/vcpkg.json').read_text())
            self.assertEqual(['curl', 'geos', 'hdf5', 'netcdf', 'parquet', 'postgresql'], union['dependencies'][0]['features'])
            self.assertIn({'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']}, union['dependencies'])
            self.assertIn({'name': 'curl', 'default-features': False, 'features': ['ssl', 'c-ares']}, union['dependencies'])
            self.assertEqual('0.1.0-combined-SNAPSHOT', combined_identity['version'])
            self.assertEqual(fixture['urls'], combined_identity['httpFixtureUrls'])
            combined_test = (combined / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt').read_text()
            self.assertIn('combined', combined_test)
            self.assertIn(fixture['urls']['http'], combined_test)
            self.assertNotIn('@HDF5_PATH@', combined_test)
            self.assertIn("'NativeHttpProfileTest'", (combined / 'verification/consumer.py').read_text())
            self.assertTrue((combined / 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/CombinedDriverMemoryProbe.kt').is_file())
            self.assertNotEqual(http, combined)
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
            self.assertEqual(combined, module.prepare(root, 'combined', 'linux-amd64', fixture)[0])
            (combined / 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/CombinedDriverMemoryProbe.kt').write_text('// changed probe')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'combined', 'linux-amd64', fixture)
            self.assertEqual(scientific, module.prepare(root, 'scientific', 'linux-amd64')[0])
            (scientific / 'build.gradle.kts').write_text('// unexpected edit')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'scientific', 'linux-amd64')

    def test_unsupported_target_fails_before_creating_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'no verified build matrix'):
                module.prepare(root, 'postgresql', 'macos-arm64')
            self.assertFalse((root / 'build').exists())
            for target in ['linux-arm64', 'windows-amd64']:
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'no verified build matrix'):
                    module.prepare(root, 'combined', target, HTTP_FIXTURE)
                self.assertFalse((root / 'build').exists())

    def test_mac_combined_owned_postgis_identity_and_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = checkout(root)
            with self.assertRaisesRegex(ValueError, 'owned running PostGIS fixture'):
                module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE)
            self.assertFalse((root / 'build').exists())
            snapshot, identity = module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, POSTGIS_FIXTURE)
            expected = {key: POSTGIS_FIXTURE['identity'][key] for key in ['target', 'archiveSha256']}
            expected['dsn'] = POSTGIS_FIXTURE['dsn']
            self.assertEqual(expected, identity['postgresqlFixture'])
            self.assertNotIn('/private/', json.dumps(identity))
            generated = (snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt').read_text()
            self.assertIn(POSTGIS_FIXTURE['dsn'], generated)
            self.assertNotIn('@POSTGRESQL_DSN@', generated)
            self.assertNotIn('password=', generated)
            self.assertNotIn('/private/', generated)
            changed = dict(POSTGIS_FIXTURE, dsn=POSTGIS_FIXTURE['dsn'].replace('16432', '16433'))
            changed['identity'] = dict(changed['identity'], safeDSN=changed['dsn'])
            self.assertNotEqual(snapshot, module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, changed)[0])
            random_dsn = 'PG:host=127.0.0.1 port=16434 dbname=g4k_0123456789abcdef user=g4k_0123456789abcdef'
            randomized = dict(POSTGIS_FIXTURE, dsn=random_dsn,
                              identity=dict(POSTGIS_FIXTURE['identity'], safeDSN=random_dsn))
            self.assertEqual(random_dsn, module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, randomized)[1]['postgresqlFixture']['dsn'])
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
            self.assertEqual(snapshot, module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, POSTGIS_FIXTURE)[0])
            (snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt').write_text('// changed fixture')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, POSTGIS_FIXTURE)

    def test_unsafe_postgis_identity_rejected_before_output(self):
        invalid = [dict(POSTGIS_FIXTURE, dsn=dsn) for dsn in [
            POSTGIS_FIXTURE['dsn'] + ' password=secret',
            POSTGIS_FIXTURE['dsn'] + '\noptions=-csearch_path=public',
            POSTGIS_FIXTURE['dsn'].replace('127.0.0.1', 'localhost'),
            POSTGIS_FIXTURE['dsn'].replace('16432', '0'),
            POSTGIS_FIXTURE['dsn'].replace('16432', '65536'),
            POSTGIS_FIXTURE['dsn'].replace('user=gdal4k', 'user=other'), None]]
        invalid += [dict(POSTGIS_FIXTURE, identity=identity) for identity in [
            None, dict(POSTGIS_FIXTURE['identity'], target='linux-amd64'),
            dict(POSTGIS_FIXTURE['identity'], archiveSha256='bad'),
            dict(POSTGIS_FIXTURE['identity'], archiveSha256=None),
            dict(POSTGIS_FIXTURE['identity'], safeDSN=POSTGIS_FIXTURE['dsn'].replace('16432', '16433'))]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout(root)
            for fixture in invalid:
                with self.subTest(fixture=fixture), self.assertRaisesRegex(ValueError, 'Unsafe or mismatched'):
                    module.prepare(root, 'combined', 'macos-arm64', HTTP_FIXTURE, fixture)
                self.assertFalse((root / 'build').exists())

    def test_owned_postgis_child_environment_keeps_private_credentials_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / 'pgpass'
            password.write_text('private fixture credentials\n')
            password.chmod(0o600)
            inherited = {'PATH': '/tools/bin', 'CURL_CA_BUNDLE': '/owned/ca.crt',
                         'PGPASSWORD': 'personal', 'PGHOST': 'personal', 'PGOPTIONS': 'personal', 'PGSERVICE': 'personal'}
            private = {'PGPASSFILE': str(password), 'PGCONNECT_TIMEOUT': '10'}
            env = module.postgresql_child_environment(inherited, {'env': private})
            self.assertEqual({'PATH': '/tools/bin', 'CURL_CA_BUNDLE': '/owned/ca.crt', **private}, env)
            self.assertEqual('personal', inherited['PGPASSWORD'])
            password.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private regular file'):
                module.postgresql_child_environment(inherited, {'env': private})
            password.chmod(0o600)
            link = password.parent / 'link'; link.symlink_to(password)
            with self.assertRaisesRegex(ValueError, 'private regular file'):
                module.postgresql_child_environment(inherited, {'env': dict(private, PGPASSFILE=str(link))})
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                module.postgresql_child_environment(inherited, {'env': dict(private, DYLD_LIBRARY_PATH='/server')})

    def test_combined_requires_owned_http_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = checkout(root)
            with self.assertRaisesRegex(ValueError, 'owned running HTTP/TLS fixture'):
                module.prepare(root, 'combined', 'linux-amd64')
            self.assertFalse((root / 'build').exists())
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})

    def test_release_versions_remain_separate(self):
        self.assertEqual('1.2.3-parquet', module.profile_version('1.2.3', 'parquet'))
        self.assertEqual('1.2.3-combined', module.profile_version('1.2.3', 'combined'))


class ProfileReportBindingsTest(unittest.TestCase):
    def test_combined_request_keys_require_all_fixture_and_fresh_process_rounds(self):
        policy = {'source-jvm', 'source-native', 'consumer-jvm', 'consumer-native'}
        diagnostics = {'settings': {'warmupRounds': 1, 'rounds': 3},
                       'records': [{'contextKey': 'probe-' + str(i)} for i in range(3)]}
        keys = [p + '-combined-case' for p in policy]
        keys += [f'probe-{i}-combined-{r}' for i in range(3) for r in range(4)]
        requests = [{'case': 'combined', 'name': 'fixture.tif', 'key': key, 'status': 206,
                     'requestedBytes': 4096, 'bodyBytes': 4096} for key in keys]
        result = report.combined_request_bindings(requests, requests, policy, diagnostics)
        self.assertEqual(16, len(result['contextKeys']))
        with self.assertRaises(AssertionError):
            report.combined_request_bindings(requests, requests[:-1], policy, diagnostics)
        unrelated = requests + [dict(requests[0], key='unowned-combined-case')]
        with self.assertRaisesRegex(AssertionError, 'Unrecognized'):
            report.combined_request_bindings(unrelated, unrelated, policy, diagnostics)

    def test_diagnostics_reject_stale_package_probe_and_raw_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contents = {'source.kt': b'current source', 'probe.class': b'unit test probe identity',
                        'stdout.log': b'current raw stdout', 'rss.txt': b'current raw OS evidence',
                        'gdal4k-engine/build/engine/linux-amd64/manifest.json': b'unit test manifest identity'}
            for name, value in contents.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            digest = lambda name: hashlib.sha256(contents[name]).hexdigest()
            publication = {'jniSha256': 'native-identity', 'compiledProductionSha256': {'current.class': 'class-identity'}}
            data = {'engineFingerprint': 'fingerprint', 'engineLibrarySha256': 'native-identity',
                    'target': 'linux-amd64', 'engineManifestSha256': digest('gdal4k-engine/build/engine/linux-amd64/manifest.json'),
                    'compiledProductionSha256': dict(publication['compiledProductionSha256']),
                    'compiledProbeSha256': {'probe.class': digest('probe.class')},
                    'sourceSha256': {'source.kt': digest('source.kt')},
                    'records': [{'log': 'stdout.log', 'logSha256': digest('stdout.log'),
                                 'rssLog': 'rss.txt', 'rssLogSha256': digest('rss.txt')}]}
            measured = {'fingerprint': 'fingerprint'}
            report.diagnostic_bindings(root, data, measured, publication)
            with self.assertRaisesRegex(AssertionError, 'package class mismatch'):
                report.diagnostic_bindings(root, dict(data, compiledProductionSha256={'old.class': 'old'}), measured, publication)
            (root / 'probe.class').write_bytes(b'changed')
            with self.assertRaisesRegex(AssertionError, 'source/probe changed'):
                report.diagnostic_bindings(root, data, measured, publication)
            (root / 'probe.class').write_bytes(contents['probe.class'])
            (root / 'stdout.log').write_bytes(b'changed')
            with self.assertRaisesRegex(AssertionError, 'raw log changed'):
                report.diagnostic_bindings(root, data, measured, publication)

if __name__ == '__main__': unittest.main()
