"""Exercise profile isolation against a real temporary Git checkout, without installing dependencies."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import shutil
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


def private_vcpkg(root):
    """Two owned real Git repositories; no vcpkg binary or dependency build."""
    snapshot, shared = root / 'snapshot', root / 'shared'
    snapshot.mkdir(); shared.mkdir()
    for repository in [snapshot, shared]:
        subprocess.run(['git', 'init', '-q', repository], check=True)
        subprocess.run(['git', '-C', repository, 'config', '--local', 'core.longpaths', 'false'], check=True)
    (shared / 'unit.txt').write_text('unit-only Git repository\n')
    subprocess.run(['git', '-C', shared, 'add', 'unit.txt'], check=True)
    subprocess.run(['git', '-C', shared, '-c', 'user.name=Unit', '-c', 'user.email=unit@example.invalid',
                    'commit', '-qm', 'unit source'], check=True)
    revision = subprocess.check_output(['git', '-C', shared, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', 'clone', '--no-local', '--quiet', shared, snapshot / 'v'], check=True)
    subprocess.run(['git', '-C', snapshot / 'v', 'config', '--local', 'core.longpaths', 'false'], check=True)
    files = {
        'build/build-tools/native.json': json.dumps({'VCPKG_ROOT':str(snapshot / 'v')}),
        'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties': 'vcpkg.revision='+revision+'\n',
        'gdal4k-engine/tools/driver-profile.py': '// unit manager source binding\n',
    }
    for relative, text in files.items():
        path = snapshot / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    return snapshot, shared, revision


class ProfileIsolationTest(unittest.TestCase):
    def test_private_windows_git_config_survives_vcpkg_minimal_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, shared, revision = private_vcpkg(Path(temporary))
            original = {path:path.read_bytes() for path in [shared / '.git/config', snapshot / '.git/config']}
            identity = module.configure_windows_vcpkg_git(snapshot)
            self.assertEqual(revision, identity['revision'])
            self.assertEqual('v', identity['repositoryRelativePath'])
            self.assertTrue(identity['localLongpaths'])
            # Pinned vcpkg-tool uses precisely this environment for indexed Git.
            git = shutil.which('git')
            effective = subprocess.check_output([git, '--git-dir='+str(snapshot / 'v/.git'),
                                                'config', '--type=bool', '--get', 'core.longpaths'],
                                               env={'GIT_INDEX_FILE':str(snapshot / 'unit.index')}, text=True).strip()
            self.assertEqual('true', effective)
            self.assertEqual(identity, module.windows_vcpkg_git_identity(snapshot))
            self.assertEqual(identity, json.loads((snapshot / 'verification/results/windows-vcpkg-git.json').read_text()))
            for path, before in original.items():
                self.assertEqual(before, path.read_bytes())

    def test_private_windows_git_rejects_wrong_revision_or_external_selection_before_mutation(self):
        for failure in ['revision', 'outside', 'shared-metadata', 'hardlink']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                snapshot, shared, _ = private_vcpkg(Path(temporary))
                config = snapshot / 'v/.git/config'
                if failure == 'revision':
                    (snapshot / 'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties').write_text(
                        'vcpkg.revision='+'0'*40+'\n')
                elif failure == 'outside':
                    (snapshot / 'build/build-tools/native.json').write_text(json.dumps({'VCPKG_ROOT':str(shared)}))
                elif failure == 'shared-metadata':
                    (snapshot / 'v/.git/commondir').write_text(str(shared / '.git'))
                else:
                    config.unlink()
                    os.link(shared / '.git/config', config)
                before = config.read_bytes()
                with self.assertRaises(ValueError):
                    module.configure_windows_vcpkg_git(snapshot)
                self.assertEqual(before, config.read_bytes())
                self.assertFalse((snapshot / 'verification/results/windows-vcpkg-git.json').exists())

    def test_report_binds_private_windows_git_receipt_to_current_physical_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, _, _ = private_vcpkg(Path(temporary))
            identity = module.configure_windows_vcpkg_git(snapshot)
            binding = report.windows_vcpkg_git_bindings(snapshot)
            self.assertEqual(identity, binding['identity'])
            receipt = snapshot / binding['receiptPath']
            self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), binding['receiptSha256'])
            receipt.write_text(json.dumps(dict(identity, revision='0' * 40)))
            with self.assertRaisesRegex(AssertionError, 'differs from its receipt'):
                report.windows_vcpkg_git_bindings(snapshot)
            receipt.write_text(json.dumps(identity))
            subprocess.run(['git', '-C', snapshot / 'v', 'config', '--local', 'core.longpaths', 'false'], check=True)
            with self.assertRaisesRegex(ValueError, 'long paths are not enabled'):
                report.windows_vcpkg_git_bindings(snapshot)

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks needs Windows privileges')
    def test_private_windows_git_rejects_symlinked_clone_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, shared, _ = private_vcpkg(Path(temporary))
            before = (shared / '.git/config').read_bytes()
            shutil.rmtree(snapshot / 'v')
            (snapshot / 'v').symlink_to(shared, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink or reparse'):
                module.configure_windows_vcpkg_git(snapshot)
            self.assertEqual(before, (shared / '.git/config').read_bytes())

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
            generated_build = snapshot / 'build.gradle.kts'
            self.assertIn('vcpkgTool.installationDirectory.set(layout.projectDirectory.dir("v"))',
                          generated_build.read_text(encoding='utf-8'))
            prepared = json.loads((snapshot.parent / 'prepared.json').read_text(encoding='utf-8'))
            self.assertEqual(hashlib.sha256(generated_build.read_bytes()).hexdigest(), prepared['build.gradle.kts'])
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

    def windows_tls_evidence(self, root):
        """Real production crypto; simulated ACL/tool/kernel inputs are never Windows proof."""
        names = ['verification/driver-profile/http-fixture.py', 'verification/windows_process.py',
                 'verification/fixtures/windows-tls-generator-lock.json', 'verification/driver-profile/windows-tls-requirements.txt']
        for name in names:
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((report.ROOT / name).read_bytes())
        evidence = root / 'verification/results/native-http-profile'; evidence.mkdir(parents=True)
        private = root / 'private-fixture'; private.mkdir()
        lock = json.loads((root / names[2]).read_text(encoding='utf-8'))
        identity = dict(versions={name: value['version'] for name, value in lock['packages'].items()},
                        python=dict(version='synthetic tool identity; real crypto below', executable='python.exe', sha256='a' * 64),
                        actualInstalledModules={name: dict(filename=filename, bytes=10, sha256='b' * 64)
                        for name, filename in [('cryptography.hazmat.bindings._rust', '_rust.pyd'),
                                               ('_cffi_backend', '_cffi_backend.pyd'), ('pycparser', '__init__.py')]})
        tool = dict(schemaVersion=1, tool='pyca/cryptography', lockSha256=report.sha(root / names[2]), **identity)
        (evidence / 'certificate-tool.json').write_text(json.dumps(tool), encoding='utf-8')
        acl = dict(currentUserSid='S-1-5-21-unit', ownerSid='S-1-5-21-unit', daclProtected=True,
                   entries=[dict(sid=sid, type=0, mask=0x1f01ff, flags=0) for sid in ['S-1-5-21-unit', 'S-1-5-18']])
        fixture_spec = importlib.util.spec_from_file_location('report_test_http_fixture', report.ROOT / names[0])
        fixture_api = importlib.util.module_from_spec(fixture_spec); fixture_spec.loader.exec_module(fixture_api)
        process = Mock(); process.private_file.return_value = acl
        url = 'http://127.0.0.1:10005/crl/' + 'a' * 32 + '/fixture-ca.crl'
        with patch.object(fixture_api, 'windows_process_api', return_value=process), patch.object(fixture_api, 'windows_certificate_tool'):
            ca = fixture_api.windows_certificate(private, 'ca', None, 'gdal4k-fixture-ca', url)
            fixture_api.windows_certificate(private, 'trusted', ca, 'localhost', url)
            fixture_api.windows_certificate(private, 'untrusted', None, 'localhost')
            fixture_api.windows_certificate(private, 'wrong-name', ca, 'wrong.invalid', url)
            revoked = fixture_api.windows_certificate(private, 'revoked', ca, 'localhost', url)
            crl = fixture_api.windows_crl(private, ca, revoked, url)
        for path in private.iterdir():
            if path.suffix != '.key': shutil.copyfile(path, evidence / path.name)
        manifest = dict(schemaVersion=1, crl=crl,
                        certificates={name: json.loads((evidence / (name + '-generator.json')).read_text())
                                      for name in ['ca', 'trusted', 'untrusted', 'wrong-name', 'revoked']},
                        server=dict(host='127.0.0.1', port=10005, owner=dict(pid=777, creationTime100ns=7777, imagePath=r'C:\Python\python.exe'),
                                    maxBodyBytes=16384, maxConnections=4, connectionDeadlineSeconds=2, maxRecordedRequests=128),
                        serverSourceSha256=report.sha(root / names[0]))
        (evidence / 'revocation-fixture.json').write_text(json.dumps(manifest))
        (evidence / 'revocation-cleanup.json').write_text(json.dumps(dict(crlListenerClosed=True, crlServerThreadStopped=True,
                                                                       tlsListenersClosed=True, tlsServerThreadsStopped=True)))
        (evidence / 'crl-requests.json').write_text(json.dumps([dict(method='GET', pathKind='public-crl', status=200, bodyBytes=crl['bytes'])]))
        (evidence / 'revoked-requests.json').write_text('[]')
        fixture = dict(urls=copy.deepcopy(HTTP_FIXTURE['urls']), imageBytes=4200000,
                       windowsRevocation=dict(crlUrl=url, revokedBase='https://127.0.0.1:10006'))
        (evidence / 'https-requests.json').write_text(json.dumps([dict(case='cache', key='native-tls-trusted', name='fixture.tif',
                       status=206, range='bytes=0-16383', requestedBytes=16384, bodyBytes=16384, ifRange=None)]))
        return evidence, identity, fixture

    def test_windows_tls_public_evidence_requires_tool_certificates_and_private_acl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); evidence, identity, _ = self.windows_tls_evidence(root)
            with patch.object(report, 'windows_tls_tool_identity', return_value=identity):
                bound = report.windows_tls_fixture_bindings(root, evidence)
                self.assertEqual({'ca', 'trusted', 'untrusted', 'wrong-name', 'revoked'}, set(bound['certificates']))
                tool_file = evidence / 'certificate-tool.json'; raw = tool_file.read_bytes(); tool_file.unlink()
                with self.assertRaisesRegex(AssertionError, 'Missing current evidence'):
                    report.windows_tls_fixture_bindings(root, evidence)
                tool_file.write_bytes(raw)
                cert = evidence / 'trusted.crt'; original = cert.read_bytes(); cert.write_bytes(b'changed public certificate')
                with self.assertRaisesRegex(AssertionError, 'public certificate changed'):
                    report.windows_tls_fixture_bindings(root, evidence)
                cert.write_bytes(original)
                record_file = evidence / 'trusted-generator.json'
                record = json.loads(record_file.read_text()); record['privateKeyAcl']['daclProtected'] = False
                record_file.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, 'Fixture DACL'):
                    report.windows_tls_fixture_bindings(root, evidence)

    def test_windows_revocation_rejects_actual_der_or_certificate_changes_after_hash_repair(self):
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        from cryptography.exceptions import InvalidSignature
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); evidence, identity, _ = self.windows_tls_evidence(root)
            originals = {p.name: p.read_bytes() for p in evidence.iterdir()}
            ca_key = serialization.load_pem_private_key((root / 'private-fixture/ca.key').read_bytes(), None)
            cert = x509.load_pem_x509_certificate(originals['trusted.crt'])
            crl = x509.load_der_x509_crl(originals['fixture-ca.crl'])
            def save_manifest(manifest):
                (evidence / 'revocation-fixture.json').write_text(json.dumps(manifest))
            def changed_certificate(mode):
                now = datetime.datetime.now(datetime.timezone.utc)
                public_key = ec.generate_private_key(ec.SECP256R1()).public_key() if mode == 'actual-leaf-key' else cert.public_key()
                builder = (x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
                           .public_key(public_key).serial_number(cert.serial_number)
                           .not_valid_before(now - datetime.timedelta(days=3) if mode == 'expired-certificate' else cert.not_valid_before_utc)
                           .not_valid_after(now - datetime.timedelta(days=1) if mode == 'expired-certificate' else cert.not_valid_after_utc))
                for ext in cert.extensions:
                    value = x509.SubjectAlternativeName([x509.DNSName('wrong.invalid')]) if mode == 'actual-san' and isinstance(ext.value, x509.SubjectAlternativeName) else ext.value
                    if mode == 'actual-leaf-key' and isinstance(ext.value, x509.SubjectKeyIdentifier):
                        value = x509.SubjectKeyIdentifier.from_public_key(public_key)
                    builder = builder.add_extension(value, ext.critical)
                data = builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
                if mode == 'certificate-signature':
                    parsed = x509.load_pem_x509_certificate(data)
                    der = bytearray(parsed.public_bytes(serialization.Encoding.DER)); der[-1] ^= 1
                    data = x509.load_der_x509_certificate(bytes(der)).public_bytes(serialization.Encoding.PEM)
                (evidence / 'trusted.crt').write_bytes(data)
                record = json.loads(originals['trusted-generator.json']); record['certificateSha256'] = report.sha(evidence / 'trusted.crt')
                (evidence / 'trusted-generator.json').write_text(json.dumps(record))
                manifest = json.loads(originals['revocation-fixture.json']); manifest['certificates']['trusted'] = record; save_manifest(manifest)
            def changed_crl(mode):
                if mode == 'crl-signature':
                    raw = bytearray(originals['fixture-ca.crl']); raw[-1] ^= 1; changed = x509.load_der_x509_crl(bytes(raw))
                else:
                    now = datetime.datetime.now(datetime.timezone.utc)
                    builder = (x509.CertificateRevocationListBuilder()
                               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'other')]) if mode == 'crl-issuer' else crl.issuer)
                               .last_update(now - datetime.timedelta(days=3) if mode == 'expired-crl' else crl.last_update_utc)
                               .next_update(now - datetime.timedelta(days=1) if mode == 'expired-crl' else crl.next_update_utc))
                    entry = crl[0]
                    if mode == 'revoked-serial':
                        entry_builder = x509.RevokedCertificateBuilder().serial_number(entry.serial_number + 1).revocation_date(entry.revocation_date_utc)
                        for ext in entry.extensions: entry_builder = entry_builder.add_extension(ext.value, ext.critical)
                        entry = entry_builder.build()
                    for ext in crl.extensions: builder = builder.add_extension(ext.value, ext.critical)
                    changed = builder.add_revoked_certificate(entry).sign(ca_key, hashes.SHA256())
                (evidence / 'fixture-ca.crl').write_bytes(changed.public_bytes(serialization.Encoding.DER))
                manifest = json.loads(originals['revocation-fixture.json']); info = manifest['crl']
                info.update(bytes=(evidence / 'fixture-ca.crl').stat().st_size, sha256=report.sha(evidence / 'fixture-ca.crl'),
                            issuer=changed.issuer.rfc4514_string(), thisUpdateUtc=changed.last_update_utc.isoformat(),
                            nextUpdateUtc=changed.next_update_utc.isoformat(), revokedSerialHex=[format(item.serial_number, 'x') for item in changed])
                save_manifest(manifest)
            with patch.object(report, 'windows_tls_tool_identity', return_value=identity):
                report.windows_tls_fixture_bindings(root, evidence)
                for mode in ['certificate-signature', 'actual-san', 'actual-leaf-key', 'expired-certificate', 'crl-signature', 'crl-issuer', 'revoked-serial', 'expired-crl']:
                    for name, raw in originals.items(): (evidence / name).write_bytes(raw)
                    changed_certificate(mode) if mode in ['certificate-signature', 'actual-san', 'actual-leaf-key', 'expired-certificate'] else changed_crl(mode)
                    with self.subTest(mode=mode), self.assertRaises((AssertionError, InvalidSignature)):
                        report.windows_tls_fixture_bindings(root, evidence)

    def test_windows_revocation_requires_final_cleanup_and_real_complete_crl_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); evidence, identity, _ = self.windows_tls_evidence(root)
            changes = [('revocation-cleanup.json', dict(crlListenerClosed=True, crlServerThreadStopped=False, tlsListenersClosed=True, tlsServerThreadsStopped=True)),
                       ('crl-requests.json', []), ('crl-requests.json', [dict(method='HEAD', pathKind='public-crl', status=200, bodyBytes=0)]),
                       ('crl-requests.json', [dict(method='GET', pathKind='public-crl', status=200, bodyBytes=1)]),
                       ('revoked-requests.json', [dict(status=206)])]
            complete = json.loads((evidence / 'crl-requests.json').read_text())[0]
            changes += [('crl-requests.json', [complete, dict(method='GET', pathKind='other', status=200, bodyBytes=0)]),
                        ('crl-requests.json', [complete, dict(method='POST', pathKind='public-crl', status=200, bodyBytes=0)])]
            with patch.object(report, 'windows_tls_tool_identity', return_value=identity):
                for name, changed in changes:
                    path = evidence / name; original = path.read_bytes(); path.write_text(json.dumps(changed))
                    with self.subTest(file=name, value=changed), self.assertRaises(AssertionError):
                        report.windows_tls_fixture_bindings(root, evidence)
                    path.write_bytes(original)

    def test_windows_early_tls_controls_bind_pe_source_owned_commands_and_actual_logs(self):
        """Real PE parsing/crypto, modeled commands only; never executes Windows TLS."""
        import datetime
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); evidence, identity, fixture = self.windows_tls_evidence(root)
            sources = ['gdal4k-engine/tools/driver-profile.py', 'gdal4k/native/http-cache-probe.cpp',
                       'gdal4k/native/http-inputs.hpp', 'verification/command_evidence.py', 'verification/windows_process.py']
            for name in sources + ['verification/windows_pe.py']:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes((report.ROOT / name).read_bytes())
            build = root / 'gdal4k-engine/build'
            archive = build / 'engine/windows-amd64/libgdal4k.a'; archive.parent.mkdir(parents=True); archive.write_bytes(b'modeled engine archive')
            manifest_path = archive.parent / 'manifest.json'
            manifest_path.write_text(json.dumps(dict(target='windows-amd64', source_fingerprint='current-optional-engine',
                                     artifacts={'engine/windows-amd64/libgdal4k.a': report.sha(archive)})))
            probe = archive.parent / 'cmake/gdal4k_http_probe.exe'; probe.parent.mkdir(); probe.write_bytes(pe_image())
            pe_path = root / 'verification/results/native-http-cache-probe-pe.json'
            module.windows_pe_api(root).audit(probe, root=root, output=pe_path)
            reference = lambda path: dict(path=path.relative_to(root).as_posix(), sha256=report.sha(path))
            directory = root / 'verification/results/native-http-tls-controls'; directory.mkdir()
            value = dict(schemaVersion=1, target='windows-amd64', result='passed', sourceSha256={name: report.sha(root / name) for name in sources},
                         engineManifest=reference(manifest_path), engineFingerprint='current-optional-engine', probe=reference(probe),
                         windowsDependencyAudit=reference(pe_path), fixtureManifestSha256=report.sha(evidence / 'revocation-fixture.json'), controls={})
            start = datetime.datetime.now(datetime.timezone.utc)
            for index, (name, code) in enumerate([('trusted', 0), ('revoked', 1)]):
                url = (fixture['urls']['https'] if name == 'trusted' else fixture['windowsRevocation']['revokedBase']) + '/cache/native-tls-' + name + '/fixture.tif'
                native = dict(result='passed', mode='tls-control', fileBytes=fixture['imageBytes'], headerBytes=16,
                              tiffHeaderVerified=True, vsiDispatchVerified=True, registryReleaseVerified=True)
                log = json.dumps(native).encode() if code == 0 else b'HTTP range request failed [curl=60, schannelReason=revoked]\n'
                log_path = directory / (name + '.log'); log_path.write_bytes(log)
                command_path = directory / (name + '.json')
                record = dict(command=[str(probe), url, '--tls-only'], cwd=str(root), timeoutSeconds=30,
                              status='passed' if code == 0 else 'failed', returncode=code, outputComplete=True,
                              log=name + '.log', logSha256=report.sha(log_path), pidRole='owned child process', pid=1000 + index,
                              windowsProcess=dict(pid=1000 + index, creationTime100ns=12345678 + index, imagePath=str(probe)),
                              ownership=dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True),
                              cleanup=dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False), elapsedSeconds=1,
                              startedAtUtc=(start + datetime.timedelta(seconds=index * 2)).isoformat(),
                              completedAtUtc=(start + datetime.timedelta(seconds=index * 2 + 1)).isoformat())
                command_path.write_text(json.dumps(record))
                value['controls'][name] = dict(url=url, requestKey='native-tls-' + name, commandRecord=reference(command_path),
                                               log=reference(log_path), expectedReturncode=code,
                                               **({'nativeResult': native} if code == 0 else {'schannelReason': 'revoked'}))
            result_path = directory / 'result.json'; result_path.write_text(json.dumps(value))
            validate = lambda: report.windows_native_tls_control_bindings(root, evidence, fixture, 'current-optional-engine')
            with patch.object(report, 'windows_tls_tool_identity', return_value=identity):
                report.windows_tls_fixture_bindings(root, evidence)
                self.assertEqual('native-tls-trusted', validate()['trustedRequestKey'])
                original_files = {path: path.read_bytes() for path in [*directory.iterdir(), archive, probe, root / sources[0], evidence / 'https-requests.json']}
                for mode in ['source', 'engine', 'probe', 'different-command', 'different-image', 'unowned', 'forced-cleanup', 'incomplete-output',
                             'wrong-status', 'wrong-curl', 'wrong-reason', 'endpoint-leak', 'forged-native-json', 'different-log',
                             'wall-time', 'missing-trusted-request', 'wrong-trusted-range', 'unknown-control']:
                    for path, raw in original_files.items(): path.write_bytes(raw)
                    changed = copy.deepcopy(value)
                    if mode in ['source', 'engine', 'probe']:
                        path = {'source': root / sources[0], 'engine': archive, 'probe': probe}[mode]; path.write_bytes(b'changed retained bytes')
                    elif mode in ['different-command', 'different-image', 'unowned', 'forced-cleanup', 'incomplete-output', 'wrong-status', 'different-log', 'wall-time']:
                        path = directory / 'revoked.json'; record = json.loads(path.read_text())
                        if mode == 'different-command': record['command'][-1] = '--not-the-control'
                        if mode == 'different-image': record['windowsProcess']['imagePath'] = r'C:\other\gdal4k_http_probe.exe'
                        if mode == 'unowned': record['ownership']['assignedBeforeResume'] = False
                        if mode == 'forced-cleanup': record['cleanup']['forcedTreeTermination'] = True
                        if mode == 'incomplete-output': record['outputComplete'] = False
                        if mode == 'wrong-status': record['status'] = 'passed'
                        if mode == 'different-log': record['log'] = 'other.log'
                        if mode == 'wall-time': record['completedAtUtc'] = (start + datetime.timedelta(seconds=25)).isoformat()
                        path.write_text(json.dumps(record)); changed['controls']['revoked']['commandRecord'] = reference(path)
                    elif mode in ['wrong-curl', 'wrong-reason', 'endpoint-leak', 'forged-native-json']:
                        name = 'trusted' if mode == 'forged-native-json' else 'revoked'
                        path = directory / (name + '.log'); raw = path.read_bytes()
                        if mode == 'wrong-curl': raw = raw.replace(b'curl=60', b'curl=35')
                        if mode == 'wrong-reason': raw = raw.replace(b'revoked', b'unknown-ca')
                        if mode == 'endpoint-leak': raw += b'https://127.0.0.1:10006/private-token'
                        if mode == 'forged-native-json':
                            native = json.loads(raw); native['registryReleaseVerified'] = False; raw = json.dumps(native).encode()
                            changed['controls'][name]['nativeResult'] = native
                        path.write_bytes(raw); changed['controls'][name]['log'] = reference(path)
                        command_path = directory / (name + '.json'); record = json.loads(command_path.read_text()); record['logSha256'] = report.sha(path)
                        command_path.write_text(json.dumps(record)); changed['controls'][name]['commandRecord'] = reference(command_path)
                    elif mode in ['missing-trusted-request', 'wrong-trusted-range']:
                        path = evidence / 'https-requests.json'; requests = json.loads(path.read_text())
                        if mode == 'missing-trusted-request': requests = []
                        else: requests[0]['range'] = 'bytes=0-99999999'
                        path.write_text(json.dumps(requests))
                    else: changed['controls']['unexpected'] = copy.deepcopy(changed['controls']['trusted'])
                    result_path.write_text(json.dumps(changed))
                    with self.subTest(mode=mode), self.assertRaises((AssertionError, ValueError)):
                        validate()

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

class WindowsNativeTlsControlRecordTest(unittest.TestCase):
    """Synthetic receipt mutations test evidence rejection, not Windows TLS."""
    def test_expected_revocation_failure_keeps_failed_command_status_and_owned_identity(self):
        command = [r'C:\owned\gdal4k_http_probe.exe', 'https://127.0.0.1:12345/cache/native-tls-revoked/fixture.tif', '--tls-only']
        log = b'HTTP range request failed [curl=60, schannelReason=revoked]\n'
        record = dict(command=command, cwd=r'C:\owned', timeoutSeconds=30, status='failed', returncode=1,
                      outputComplete=True, logSha256=hashlib.sha256(log).hexdigest(), pidRole='owned child process', pid=1234,
                      windowsProcess=dict(pid=1234, creationTime100ns=12345678, imagePath=command[0]),
                      ownership=dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True),
                      cleanup=dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False),
                      startedAtUtc='2026-10-24T00:00:00+00:00', completedAtUtc='2026-10-24T00:00:01+00:00', elapsedSeconds=1)
        def validate(value, output=log, expected=1):
            module.validate_windows_tls_control_record(value, command=command, cwd=r'C:\owned',
                                                       log_bytes=output, expected_code=expected)
        validate(record)
        positive = dict(record, status='passed', returncode=0)
        validate(positive, expected=0)
        mutations = [dict(status='passed'), dict(returncode=0), dict(returncode=True), dict(elapsedSeconds=True), dict(outputComplete=False),
                     dict(logSha256='0' * 64), dict(timeoutSeconds=600), dict(pid=5678),
                     dict(windowsProcess=dict(record['windowsProcess'], creationTime100ns=0)),
                     dict(windowsProcess=dict(record['windowsProcess'], imagePath=r'C:\elsewhere\gdal4k_http_probe.exe')),
                     dict(ownership=dict(record['ownership'], assignedBeforeResume=False)),
                     dict(cleanup=dict(record['cleanup'], activeProcessesAfter=1)),
                     dict(cleanup=dict(record['cleanup'], forcedTreeTermination=True)),
                     dict(cleanup=dict(record['cleanup'], jobClosed=False)),
                     dict(completedAtUtc=record['startedAtUtc']), dict(elapsedSeconds=0)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(dict(record, **mutation))
        with self.assertRaises(ValueError):
            validate(record, output=log + b'unretained bytes')


if __name__ == '__main__': unittest.main()
