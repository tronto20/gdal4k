"""Exercise profile isolation against a real temporary Git checkout, without installing dependencies."""
import importlib.util
import hashlib
import json
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import copy
import io
import urllib.request
from verification.test_windows_pe import pe_image

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
             'verification/driver-profile/DriverProfileRuntimeFixture.kt': '@RUNTIME_FIXTURES_REQUIRED@ @HDF5_PATH@ @POSTGRESQL_DSN@ @HTTP_BASE@ @HTTPS_BASE@ @UNTRUSTED_BASE@ @WRONG_NAME_BASE@',
             'verification/driver-profile/DriverProfileRuntimeFixtureJvm.kt': '// JVM getenv actual\n',
             'verification/driver-profile/DriverProfileRuntimeFixtureNative.kt': '// Native getenv actual\n',
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
    def test_windows_partial_archive_overrides_fail_before_acquisition(self):
        options = ['--postgis-postgres-archive', '--postgis-extension-archive', '--postgis-runtime-archive']
        for option in options:
            with self.subTest(option=option), \
                 patch.object(module.sys, 'argv', ['driver-profile.py', 'combined', 'windows-amd64', option, 'owned-input']), \
                 patch.object(module.sys, 'stderr', io.StringIO()), \
                 patch.object(urllib.request, 'urlopen') as download, \
                 self.assertRaises(SystemExit) as failure:
                module.main()
            self.assertEqual(2, failure.exception.code)
            download.assert_not_called()

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
            for profile in ['scientific', 'parquet', 'http', 'postgresql']:
                with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, 'no verified build matrix'):
                    module.prepare(root, profile, 'windows-amd64', HTTP_FIXTURE)
                self.assertFalse((root / 'build').exists())

    def test_windows_combined_prepares_current_utf8_sources_with_owned_public_identity(self):
        with tempfile.TemporaryDirectory(prefix='Windows profile ') as temporary:
            root = Path(temporary)
            before = checkout(root)
            fixture = dict(POSTGIS_FIXTURE, identity=dict(POSTGIS_FIXTURE['identity'], target='windows-amd64'))
            with self.assertRaisesRegex(ValueError, 'owned running PostGIS fixture'):
                module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE)
            self.assertFalse((root / 'build').exists())
            source = root / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/Existing.kt'
            source.write_text('// UTF-8 source: 日本語 한글 ©\n', encoding='utf-8', newline='\n')
            snapshot, identity = module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE, fixture)
            self.assertEqual(source.read_bytes(), (snapshot / source.relative_to(root)).read_bytes())
            self.assertEqual(('Windows', 'amd64', 'MingwX64', 'windows-x64'), module.TARGETS['windows-amd64'])
            self.assertEqual('gdal4k.dll', module.jni_library('windows-amd64'))
            self.assertEqual('gdal4k_http_probe.exe', module.cache_probe('windows-amd64'))
            generated = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
            self.assertIn(fixture['dsn'], generated.read_text(encoding='utf-8'))
            self.assertNotIn('/private/', generated.read_text(encoding='utf-8'))
            self.assertNotIn('password=', generated.read_text(encoding='utf-8'))
            self.assertEqual('windows-amd64', identity['postgresqlFixture']['target'])
            self.assertNotIn('PGPASSFILE', json.dumps(identity))
            manifest = json.loads((snapshot / 'gdal4k-engine/engine/vcpkg.json').read_text(encoding='utf-8'))
            self.assertEqual(['curl', 'geos', 'hdf5', 'netcdf', 'parquet', 'postgresql'], manifest['dependencies'][0]['features'])
            self.assertEqual(before['gdal4k-engine/engine/vcpkg.json'], (root / 'gdal4k-engine/engine/vcpkg.json').read_bytes())
            self.assertNotIn(b'\r', generated.read_bytes())
            generated.write_text('// changed generated fixture\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE, fixture)

    def test_windows_snapshot_bounds_vcpkg_versioning_path_and_checks_full_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout(root)
            fixture = dict(POSTGIS_FIXTURE, identity=dict(POSTGIS_FIXTURE['identity'], target='windows-amd64'))
            snapshot, identity = module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE, fixture)
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            self.assertEqual(Path('build/driver-profiles/w') / digest[:24] / 'source', snapshot.relative_to(root))
            # This is the actual Git read-tree index filename that failed in CI.
            index = (PureWindowsPath('D:/a/gdal4k/gdal4k') / snapshot.relative_to(root).as_posix()
                     / 'gdal4k-engine/build/engine/windows-amd64/vcpkg-buildtrees/versioning_/versions/expat'
                     / ('a' * 40 + '_6256.index.lock'))
            self.assertLess(len(str(index)), 240)
            self.assertEqual(identity, json.loads((snapshot.parent / 'inputs.json').read_text(encoding='utf-8')))
            self.assertEqual(snapshot, module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE, fixture)[0])
            other_fixture = copy.deepcopy(HTTP_FIXTURE)
            other_fixture['urls']['http'] = 'http://127.0.0.1:10005'
            other, other_identity = module.prepare(root, 'combined', 'windows-amd64', other_fixture, fixture)
            self.assertNotEqual(snapshot, other)
            self.assertNotEqual(identity, other_identity)
            # Model a truncated-key collision/unrecognized directory: complete
            # identity equality is mandatory even when prepared bytes still match.
            (snapshot.parent / 'inputs.json').write_text(json.dumps(other_identity), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unrecognized profile directory'):
                module.prepare(root, 'combined', 'windows-amd64', HTTP_FIXTURE, fixture)

    def test_arm_cross_sources_require_runtime_configuration_without_producer_endpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = checkout(root)
            with self.assertRaisesRegex(ValueError, 'explicit cross-build/runtime'):
                module.prepare(root, 'combined', 'linux-arm64', HTTP_FIXTURE)
            self.assertFalse((root / 'build').exists())
            with self.assertRaisesRegex(ValueError, 'cannot capture'):
                module.prepare(root, 'combined', 'linux-arm64', HTTP_FIXTURE, runtime_fixtures=True)
            snapshot, identity = module.prepare(root, 'combined', 'linux-arm64', runtime_fixtures=True)
            self.assertEqual('required-on-execution-host', identity['runtimeFixtures'])
            self.assertNotIn('httpFixtureUrls', identity)
            self.assertNotIn('postgresqlFixture', identity)
            generated = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixture.kt'
            self.assertEqual('true      ', generated.read_text())
            prepared = json.loads((snapshot.parent / 'prepared.json').read_text())
            helpers = [name for name in prepared if '/src/' in name and 'DriverProfileRuntimeFixture' in name]
            self.assertEqual(3, len(helpers))
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
            generated.write_text('changed endpoint fallback')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'combined', 'linux-arm64', runtime_fixtures=True)

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

    def test_windows_child_environment_requires_actual_acl_without_server_dll_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            password = Path(temporary) / 'pgpass'; password.write_text('unit private file\n')
            private = {'PGPASSFILE': str(password), 'PGCONNECT_TIMEOUT': '10'}
            inherited = {'Path': r'C:\owned compiler', 'pgpassword': 'personal', 'PgHost': 'personal', 'PGOPTIONS': 'personal'}
            windows = Mock()
            with patch.object(module, 'windows_process_api', return_value=windows):
                value = module.postgresql_child_environment(inherited, {'env': private}, 'windows-amd64')
                windows.verify_private_path.assert_called_once_with(password)
                self.assertEqual({'Path': r'C:\owned compiler', **private}, value)
                windows.verify_private_path.side_effect = ValueError('ACL is not private')
                with self.assertRaisesRegex(ValueError, 'ACL'):
                    module.postgresql_child_environment(inherited, {'env': private}, 'windows-amd64')
                with self.assertRaisesRegex(ValueError, 'Unexpected'):
                    module.postgresql_child_environment(inherited, {'env': dict(private, PATH=r'C:\server')}, 'windows-amd64')

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
    def test_windows_probe_receipt_rechecks_actual_exe_and_shared_inspector(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'verification/windows_pe.py'; source.parent.mkdir()
            source.write_bytes((report.ROOT / 'verification/windows_pe.py').read_bytes())
            program = root / 'verification/build/arrow-ipc-regression/arrow-ipc-check.exe'
            program.parent.mkdir(parents=True); program.write_bytes(pe_image(delay=['ws2_32.dll']))
            path = root / 'verification/results/arrow-ipc-probe-pe.json'
            inspector = module.windows_pe_api(root)
            inspector.audit(program, root=root, output=path)
            reference = dict(path=path.relative_to(root).as_posix(), sha256=report.sha(path))
            result = report.windows_probe_bindings(root, reference, program)
            self.assertEqual(report.sha(program), result['executableSha256'])
            original = program.read_bytes(); program.write_bytes(pe_image(delay=['libwinpthread-1.dll']))
            with self.assertRaises(AssertionError): report.windows_probe_bindings(root, reference, program)
            program.write_bytes(original)
            source.write_bytes(source.read_bytes()+b'\n# changed inspector\n')
            with self.assertRaises(AssertionError): report.windows_probe_bindings(root, reference, program)

    def windows_receipt(self, root, combined=True):
        """Synthetic verifier inputs only; no Windows process is launched."""
        windows = module.windows_process_api()
        family = 'combined-driver' if combined else 'http'
        cp = {'java': r'C:\Program Files\JDK\bin\java.exe', 'classpath': r'C:\unit classes;C:\unit library.jar'}
        cp_path = root / f'gdal4k-runtime/build/{family}-memory-classpath.json'
        cp_path.parent.mkdir(parents=True)
        cp_path.write_text(json.dumps(cp), encoding='utf-8')
        folder = f'verification/results/{family}-memory/unit'
        mode = 'range'
        stem = '0' if combined else '0-' + mode
        log = folder + '/' + stem + '.log'; rss = folder + '/' + stem + '.time.txt'
        execution_log = folder + '/' + stem + '.command.json'
        (root / log).parent.mkdir(parents=True)
        (root / log).write_bytes(b'synthetic verifier stdout\n')
        identity = dict(pid=1234, creationTime100ns=123456789, imagePath=cp['java'])
        peak = dict(collector=windows.OS_RSS['collector'], field=windows.OS_RSS['field'], reportedUnit='bytes',
                    peakWorkingSetSizeBytes=80000000, windowsProcess=identity,
                    queryPhase='after-process-exit-before-handle-close', returncode=0)
        (root / rss).write_text(json.dumps(peak), encoding='utf-8')
        settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
        library = str((root / 'gdal4k-engine/build/jvm-resources/gdal4k/windows-amd64/gdal4k.dll').resolve())
        command = [cp['java'], *settings, '-cp', cp['classpath']]
        command += (['dev.gdal4k.CombinedDriverMemoryProbe', library] if combined else
                    ['dev.gdal4k.HttpMemoryProbe', mode, 'http://127.0.0.1:12345/fixture.tif',
                     str(root / 'fixture.tif'), '8192', library])
        execution = dict(command=command, cwd=str(root), status='passed', returncode=0, pid=1234,
                         windowsProcess=identity, pidRole='owned JVM process', outputComplete=True,
                         logSha256=report.sha(root / log),
                         ownership=dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True),
                         cleanup=dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False), peakMemory=peak,
                         startedAtUtc='2026-10-23T00:00:00+00:00', completedAtUtc='2026-10-23T00:00:01+00:00',
                         elapsedSeconds=1.0, environmentOverrides={'LC_ALL': 'C'}, timeoutSeconds=600 if combined else 300)
        (root / execution_log).write_text(json.dumps(execution), encoding='utf-8')
        record = dict(trial=0, mode=mode, executionLog=execution_log,
                      executionLogSha256=report.sha(root / execution_log), execution=execution,
                      log=log, logSha256=report.sha(root / log), rssLog=rss, rssLogSha256=report.sha(root / rss),
                      peakRssBytes=80000000)
        data = dict(target='windows-amd64', gradleTarget='MingwX64', classifier='windows-x64',
                    system='Windows', machine='AMD64', osRss=windows.OS_RSS.copy(),
                    settings={'jvm': settings, 'side': 8192}, fixture={'path': 'fixture.tif'}, records=[record])
        if combined: data['profile'] = 'combined'
        return data

    def test_windows_combined_and_http_bind_exact_owned_kernel_receipts(self):
        for combined in [True, False]:
            with self.subTest(combined=combined), tempfile.TemporaryDirectory(prefix='Windows evidence ') as temporary:
                root = Path(temporary)
                data = self.windows_receipt(root, combined)
                self.assertEqual({data['records'][0]['executionLog']: data['records'][0]['executionLogSha256']},
                                 report.windows_execution_bindings(root, data))

    def test_windows_bindings_reject_wrong_units_raw_peak_and_embedded_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = self.windows_receipt(root)
            changed = copy.deepcopy(data); changed['osRss']['reportedUnit'] = 'KiB'
            with self.assertRaisesRegex(AssertionError, 'RSS collector'):
                report.windows_execution_bindings(root, changed)
            changed = copy.deepcopy(data); changed['records'][0]['peakRssBytes'] += 1
            with self.assertRaisesRegex(AssertionError, 'reported peak'):
                report.windows_execution_bindings(root, changed)
            changed = copy.deepcopy(data); changed['records'][0]['execution']['command'][-1] = 'unrelated.dll'
            with self.assertRaisesRegex(AssertionError, 'embedded command'):
                report.windows_execution_bindings(root, changed)
            changed = copy.deepcopy(data); changed['records'] *= 2
            with self.assertRaises(AssertionError): report.windows_execution_bindings(root, changed)
            peak_path = root / data['records'][0]['rssLog']
            peak = json.loads(peak_path.read_text(encoding='utf-8')); peak['windowsProcess']['creationTime100ns'] += 1
            peak_path.write_text(json.dumps(peak), encoding='utf-8')
            data['records'][0]['rssLogSha256'] = report.sha(peak_path)
            with self.assertRaisesRegex(ValueError, 'final kernel peak'):
                report.windows_execution_bindings(root, data)

    def test_windows_diagnostics_require_both_actual_kernel_collector_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = self.windows_receipt(root, combined=False)
            files = {'verification/windows_process.py': b'synthetic current kernel collector source',
                     'verification/command_evidence.py': b'synthetic current command owner source',
                     'source.kt': b'synthetic current probe source', 'probe.class': b'synthetic current probe class',
                     'gdal4k-engine/build/engine/windows-amd64/manifest.json': b'synthetic current manifest'}
            for name, raw in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
            digest = lambda name: hashlib.sha256(files[name]).hexdigest()
            publication = dict(jniSha256='unit-current-jni', compiledProductionSha256={'current.class': 'unit-current-class'})
            sources = {name: digest(name) for name in ['source.kt', 'verification/windows_process.py', 'verification/command_evidence.py']}
            data.update(engineFingerprint='unit-current-fingerprint', engineLibrarySha256='unit-current-jni',
                        engineManifestSha256=digest('gdal4k-engine/build/engine/windows-amd64/manifest.json'),
                        compiledProductionSha256=publication['compiledProductionSha256'],
                        compiledProbeSha256={'probe.class': digest('probe.class')}, sourceSha256=sources)
            measured = {'fingerprint': 'unit-current-fingerprint'}
            report.diagnostic_bindings(root, data, measured, publication)
            for helper in ['verification/windows_process.py', 'verification/command_evidence.py']:
                changed = copy.deepcopy(data); del changed['sourceSha256'][helper]
                with self.subTest(helper=helper), self.assertRaisesRegex(AssertionError, 'kernel collector source'):
                    report.diagnostic_bindings(root, changed, measured, publication)
            (root / 'verification/windows_process.py').write_bytes(b'changed collector source')
            with self.assertRaisesRegex(AssertionError, 'source/probe changed'):
                report.diagnostic_bindings(root, data, measured, publication)

    def test_windows_tls_public_evidence_requires_tool_certificates_and_private_acl(self):
        """Synthetic public receipts test binding only, never actual TLS acceptance."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); evidence = root / 'verification/results/native-http-profile'
            evidence.mkdir(parents=True)
            names = ['verification/driver-profile/http-fixture.py', 'verification/windows_process.py',
                     'verification/fixtures/windows-tls-generator-lock.json',
                     'verification/driver-profile/windows-tls-requirements.txt']
            for name in names:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((report.ROOT / name).read_bytes())
            lock = json.loads((root / names[2]).read_text(encoding='utf-8'))
            identity = dict(versions={name: value['version'] for name, value in lock['packages'].items()},
                            python=dict(version='3.12 synthetic verifier input', executable='python.exe', sha256='a' * 64),
                            actualInstalledModules={name: dict(filename=filename, bytes=10, sha256='b' * 64)
                            for name, filename in [('cryptography.hazmat.bindings._rust', '_rust.pyd'),
                                                   ('_cffi_backend', '_cffi_backend.pyd'), ('pycparser', '__init__.py')]})
            tool = dict(schemaVersion=1, tool='pyca/cryptography', lockSha256=report.sha(root / names[2]), **identity)
            tool_file = evidence / 'certificate-tool.json'; tool_file.write_text(json.dumps(tool), encoding='utf-8')
            acl = dict(currentUserSid='S-1-5-21-unit', ownerSid='S-1-5-21-unit', daclProtected=True,
                       entries=[dict(sid=sid, type=0, mask=0x1f01ff, flags=0) for sid in ['S-1-5-21-unit', 'S-1-5-18']])
            for name, hostname, ca, trusted in [('ca', 'gdal4k-fixture-ca', True, False), ('trusted', 'localhost', False, True),
                                               ('untrusted', 'localhost', False, False), ('wrong-name', 'wrong.invalid', False, True)]:
                cert = evidence / (name + '.crt'); cert.write_bytes(('synthetic certificate ' + name).encode())
                record = dict(name=name, subject='CN=' + hostname, issuer='CN=' + ('gdal4k-fixture-ca' if trusted else hostname),
                              certificateSha256=report.sha(cert), privateKeyAcl=copy.deepcopy(acl), ca=ca,
                              trustedIssuerProvided=trusted, san=[hostname] + (['127.0.0.1'] if hostname == 'localhost' else []))
                (evidence / (name + '-generator.json')).write_text(json.dumps(record), encoding='utf-8')
            with patch.object(report, 'windows_tls_tool_identity', return_value=identity):
                bound = report.windows_tls_fixture_bindings(root, evidence)
                self.assertEqual({'ca', 'trusted', 'untrusted', 'wrong-name'}, set(bound['certificates']))
                tool_raw = tool_file.read_bytes(); tool_file.unlink()
                with self.assertRaisesRegex(AssertionError, 'Missing current evidence'):
                    report.windows_tls_fixture_bindings(root, evidence)
                tool_file.write_bytes(tool_raw)
                cert = evidence / 'trusted.crt'; original = cert.read_bytes(); cert.write_bytes(b'changed public certificate')
                with self.assertRaisesRegex(AssertionError, 'public certificate changed'):
                    report.windows_tls_fixture_bindings(root, evidence)
                cert.write_bytes(original)
                record_file = evidence / 'trusted-generator.json'
                record = json.loads(record_file.read_text(encoding='utf-8')); record['privateKeyAcl']['daclProtected'] = False
                record_file.write_text(json.dumps(record), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Fixture DACL'):
                    report.windows_tls_fixture_bindings(root, evidence)

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
