#!/usr/bin/env python3
"""Build optional dependencies in a separate source tree, prefix and Maven version.

Never edits the producer checkout's default manifest/build or publishes remotely.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import datetime
import importlib.util
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    'scientific': {'features': ['hdf5', 'netcdf'], 'drivers': ['HDF5', 'netCDF'],
                   'dependencies': [{'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']}],
                   'targets': ['linux-amd64', 'macos-arm64']},
    'parquet': {'features': ['parquet'], 'drivers': ['Parquet'],
                'targets': ['linux-amd64', 'macos-arm64']},
    'postgresql': {'features': ['postgresql'], 'drivers': ['PostgreSQL'],
                   'targets': ['linux-amd64']},
    'http': {'features': ['curl'], 'drivers': [],
             'dependencies': [{'name': 'curl', 'default-features': False, 'features': ['ssl', 'c-ares']}],
             'targets': ['linux-amd64', 'macos-arm64']},
    'combined': {'features': ['curl', 'hdf5', 'netcdf', 'parquet', 'postgresql'],
                 'drivers': ['HDF5', 'netCDF', 'Parquet', 'PostgreSQL'],
                 'dependencies': [
                     {'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']},
                     {'name': 'curl', 'default-features': False, 'features': ['ssl', 'c-ares']}],
                 'capabilities': ['scientific', 'parquet', 'http', 'postgresql'],
                 'targets': ['linux-amd64', 'macos-arm64', 'linux-arm64', 'windows-amd64']},
}
TARGETS = {'linux-amd64': ('Linux', 'x86_64', 'LinuxX64', 'linux-x64-gnu'),
           'macos-arm64': ('Darwin', 'arm64', 'MacosArm64', 'macos-arm64'),
           'linux-arm64': ('Linux', 'aarch64', 'LinuxArm64', 'linux-arm64-gnu'),
           'windows-amd64': ('Windows', 'amd64', 'MingwX64', 'windows-x64')}


def jni_library(target):
    return 'gdal4k.dll' if target.startswith('windows') else 'libgdal4k.dylib' if target.startswith('macos') else 'libgdal4k.so'


def cache_probe(target):
    return 'gdal4k_http_probe.exe' if target.startswith('windows') else 'gdal4k_http_probe'


def has_capability(profile, capability):
    return capability in PROFILES[profile].get('capabilities', [profile])


def profile_version(base, profile):
    # The default payload must never be replaced at its Maven coordinates.
    return (base[:-9] + '-' + profile + '-SNAPSHOT') if base.endswith('-SNAPSHOT') else base + '-' + profile


def source_files(root):
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached'], cwd=root)
    result = {}
    for name in sorted(set(names.decode().split('\0')) - {''}):
        path = root / name
        if path.is_symlink():
            raise ValueError('Source symlinks are not copied into an isolated profile: ' + name)
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def postgresql_identity(profile, target, fixture):
    """Only public endpoint/locked-input identity may enter generated sources."""
    if not has_capability(profile, 'postgresql'):
        if fixture is not None:
            raise ValueError('PostGIS fixture supplied to a profile without PostgreSQL')
        return None
    if fixture is None:
        if target in ['macos-arm64', 'windows-amd64']:
            raise ValueError('Mac/Windows PostgreSQL requires its owned running PostGIS fixture')
        return None  # Existing Linux CI service keeps its public test credentials.
    if not isinstance(fixture, dict) or not isinstance(fixture.get('identity'), dict):
        raise ValueError('Unsafe or mismatched owned PostGIS fixture identity')
    dsn = fixture.get('dsn', '')
    if not isinstance(dsn, str):
        raise ValueError('Unsafe or mismatched owned PostGIS fixture identity')
    match = re.fullmatch(r'PG:host=127\.0\.0\.1 port=([1-9][0-9]{0,4}) '
                         r'dbname=([a-z][a-z0-9_]{0,62}) user=\2', dsn)
    identity = fixture.get('identity', {})
    digest = identity.get('archiveSha256', '')
    if (not match or not 0 < int(match.group(1)) < 65536
            or identity.get('target') != target
            or identity.get('safeDSN') != dsn
            or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('Unsafe or mismatched owned PostGIS fixture identity')
    return dict(target=target, dsn=dsn, archiveSha256=digest)


def windows_process_api():
    spec = importlib.util.spec_from_file_location('profile_windows_process', ROOT / 'verification/windows_process.py')
    windows = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(windows)
    return windows


def windows_pe_api(root):
    spec = importlib.util.spec_from_file_location('profile_windows_pe', root / 'verification/windows_pe.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def postgresql_child_environment(environment, fixture, target=None):
    """Pass owned libpq authentication to tests without sharing the server environment."""
    result = {key: value for key, value in environment.items()
              if not (key.upper() if target == 'windows-amd64' else key).startswith('PG')}
    private = fixture['env']
    if not private.get('PGPASSFILE') or not set(private) <= {'PGPASSFILE', 'PGCONNECT_TIMEOUT'}:
        raise ValueError('Unexpected owned PostGIS child environment')
    password_file = Path(private['PGPASSFILE'])
    if (not password_file.is_absolute() or not password_file.is_file()
            or password_file.is_symlink()):
        raise ValueError('Owned PGPASSFILE must be a private regular file')
    if target == 'windows-amd64':
        windows_process_api().verify_private_path(password_file)
    elif password_file.stat().st_mode & 0o077:
        raise ValueError('Owned PGPASSFILE must be a private regular file')
    result.update(private)
    return result


def windows_profile_path(snapshot, relative, kind=None):
    snapshot = Path(snapshot).absolute()
    path = snapshot / relative
    if '..' in path.parts or not path.is_relative_to(snapshot):
        raise ValueError('Windows vcpkg path is outside the private snapshot')
    parts = path.relative_to(snapshot).parts
    for current in [snapshot, *(snapshot.joinpath(*parts[:i]) for i in range(1, len(parts)+1))]:
        if (current.is_symlink() or getattr(current, 'is_junction', lambda: False)()
                or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & 0x400)):
            raise ValueError('Windows vcpkg path traverses a symlink or reparse point')
    if (kind == 'file' and not path.is_file()) or (kind == 'directory' and not path.is_dir()):
        raise ValueError('Windows vcpkg requires an ordinary private '+kind)
    return path


def windows_vcpkg_git_inputs(snapshot):
    snapshot = Path(snapshot).absolute()
    tools = windows_profile_path(snapshot, 'build/build-tools/native.json', 'file')
    configured = json.loads(tools.read_text(encoding='utf-8')).get('VCPKG_ROOT')
    repository = windows_profile_path(snapshot, 'v', 'directory')
    if not isinstance(configured, str) or not Path(configured).is_absolute() or Path(configured) != repository:
        raise ValueError('Prepared vcpkg does not select the exact private snapshot clone')
    git_dir = windows_profile_path(snapshot, 'v/.git', 'directory')
    for path in git_dir.rglob('*'):
        windows_profile_path(snapshot, path.relative_to(snapshot))
        if path.is_file() and path.stat().st_nlink > 1:
            raise ValueError('Windows vcpkg Git metadata shares hard-linked files')
    if (git_dir/'commondir').exists() or (git_dir/'objects/info/alternates').exists():
        raise ValueError('Windows vcpkg must not share Git metadata or object storage')
    windows_profile_path(snapshot, 'v/.git/config', 'file')
    pins_path = 'build-logic/src/main/resources/dev/tronto/buildtools/versions.properties'
    pins_file = windows_profile_path(snapshot, pins_path, 'file')
    pins = dict(line.split('=', 1) for line in pins_file.read_text(encoding='utf-8').splitlines()
                if line and not line.startswith('#') and '=' in line)
    expected = pins.get('vcpkg.revision', '')
    if not re.fullmatch('[0-9a-f]{40}', expected):
        raise ValueError('Private vcpkg requires the exact pinned source revision')
    git = ['git', '-C', str(repository), '--git-dir='+str(git_dir), '--work-tree='+str(repository)]
    common = Path(subprocess.check_output([*git, 'rev-parse', '--git-common-dir'], text=True).strip())
    if not common.is_absolute():
        common = repository/common
    if common != git_dir:
        raise ValueError('Windows vcpkg Git metadata is shared outside its private clone')
    revision = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
    if revision != expected:
        raise ValueError('Private vcpkg source revision differs from the pin')
    manager_path = 'gdal4k-engine/tools/driver-profile.py'
    manager = windows_profile_path(snapshot, manager_path, 'file')
    identity = dict(schemaVersion=1, target='windows-amd64', repositoryRelativePath='v',
                    revision=revision, buildTools=dict(path=tools.relative_to(snapshot).as_posix(),
                    sha256=hashlib.sha256(tools.read_bytes()).hexdigest()),
                    sourceSha256={pins_path:hashlib.sha256(pins_file.read_bytes()).hexdigest(),
                                  manager_path:hashlib.sha256(manager.read_bytes()).hexdigest()})
    return git, identity


def windows_vcpkg_git_identity(snapshot):
    """Read physical private-clone bindings without changing any Git configuration."""
    git, identity = windows_vcpkg_git_inputs(snapshot)
    value = subprocess.check_output([*git, 'config', '--local', '--type=bool', '--get', 'core.longpaths'], text=True).strip()
    if value != 'true':
        raise ValueError('Private Windows vcpkg Git long paths are not enabled')
    return dict(identity, localLongpaths=True)


def configure_windows_vcpkg_git(snapshot):
    # Pinned vcpkg-tool supplies only GIT_INDEX_FILE to Windows read-tree and
    # checkout-index. Its explicit --git-dir still reads this private local config.
    git, _ = windows_vcpkg_git_inputs(snapshot)
    subprocess.run([*git, 'config', '--local', 'core.longpaths', 'true'], check=True)
    identity = windows_vcpkg_git_identity(snapshot)
    receipt = windows_profile_path(snapshot, 'verification/results/windows-vcpkg-git.json')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(identity, indent=2)+'\n', encoding='utf-8', newline='\n')
    return identity


def prepare(root, profile, target, http_fixture=None, postgresql_fixture=None, *, runtime_fixtures=False):
    spec = PROFILES[profile]
    if target not in spec['targets']:
        raise ValueError('Profile has no verified build matrix for this target: ' + target)
    if runtime_fixtures != (target == 'linux-arm64') or (runtime_fixtures and profile != 'combined'):
        raise ValueError('Linux ARM64 combined requires explicit cross-build/runtime fixture phases')
    if runtime_fixtures and (http_fixture is not None or postgresql_fixture is not None):
        raise ValueError('Cross-build sources cannot capture producer-host runtime fixtures')
    pg_identity = None if runtime_fixtures else postgresql_identity(profile, target, postgresql_fixture)
    properties = (root / 'gradle.properties').read_text(encoding='utf-8')
    base = next(line.split('=', 1)[1] for line in properties.splitlines() if line.startswith('gdal4kVersion='))
    version = profile_version(base, profile)
    inventory = source_files(root)
    identity = {'profile': profile, 'target': target, 'version': version, 'sources': inventory}
    if runtime_fixtures:
        identity['runtimeFixtures'] = 'required-on-execution-host'
    if pg_identity is not None:
        identity['postgresqlFixture'] = pg_identity
    if has_capability(profile, 'http') and not runtime_fixtures:
        if not http_fixture:
            raise ValueError('HTTP profile requires its owned running HTTP/TLS fixture')
        identity['httpFixtureUrls'] = http_fixture['urls']
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    # Git's vcpkg versioning index path exceeds Windows MAX_PATH beneath the
    # full source-digest layout. The complete identity remains checked below;
    # the shorter directory key must never authorize reuse on its own.
    output = (root / 'build/driver-profiles/w' / digest[:24] if target == 'windows-amd64'
              else root / 'build/driver-profiles' / profile / target / digest)
    snapshot = output / 'source'
    if output.exists():
        if output.is_symlink() or not (output / 'inputs.json').is_file() or json.loads((output / 'inputs.json').read_text(encoding='utf-8')) != identity:
            raise ValueError('Refusing to overwrite an unrecognized profile directory: ' + str(output))
        for name, expected in json.loads((output / 'prepared.json').read_text(encoding='utf-8')).items():
            path = snapshot / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Prepared profile source changed; do not reuse it: ' + name)
        return snapshot, identity
    snapshot.mkdir(parents=True)
    for name in inventory:
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    # An independent manifest ensures vcpkg never adds dependencies to the default prefix.
    manifest = snapshot / 'gdal4k-engine/engine/vcpkg.json'
    value = json.loads(manifest.read_text(encoding='utf-8'))
    dependency = next(d for d in value['dependencies'] if isinstance(d, dict) and d['name'] == 'gdal')
    dependency['features'] = sorted(set(dependency['features'] + spec['features']))
    value['dependencies'].extend(spec.get('dependencies', []))
    manifest.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')
    updated = '\n'.join('gdal4kVersion=' + version if line.startswith('gdal4kVersion=') else line for line in properties.splitlines()) + '\n'
    (snapshot / 'gradle.properties').write_text(updated, encoding='utf-8', newline='\n')
    # Profile snapshots are local/CI artifacts. Keep release credentials out of their use.
    with (snapshot / 'build.gradle.kts').open('a', encoding='utf-8', newline='\n') as file:
        file.write('''\n// Isolated optional driver profile: remote publication is deliberately disabled.
gradle.taskGraph.whenReady {
    require(allTasks.none { it.name.startsWith("publish") && !it.name.endsWith("ToMavenLocal") }) {
        "Optional driver snapshots support local Maven publication only"
    }
}
''')
        if target == 'windows-amd64':
            file.write('\n// Keep Windows Git configuration inside this isolated producer.\n'
                       'vcpkgTool.installationDirectory.set(layout.projectDirectory.dir("v"))\n')
    fixture = root / 'verification/driver-profile/DriverProfileApiTest.kt'
    test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
    replacements = {'@PROFILE@': profile, '@RUNTIME_FIXTURES_REQUIRED@': str(runtime_fixtures).lower(),
                    '@HDF5_PATH@': '' if runtime_fixtures else (snapshot / 'verification/build/driver-profile.h5').as_posix(),
                    '@POSTGRESQL_DSN@': pg_identity['dsn'] if pg_identity else
                    ('' if runtime_fixtures else 'PG:host=127.0.0.1 port=5432 dbname=gdal4k user=gdal4k password=profile-fixture')}
    replacements.update({key: '' for key in ['@HTTP_BASE@', '@HTTPS_BASE@', '@UNTRUSTED_BASE@', '@WRONG_NAME_BASE@']})
    if has_capability(profile, 'http') and not runtime_fixtures:
        replacements.update({'@HTTP_BASE@': http_fixture['urls']['http'], '@HTTPS_BASE@': http_fixture['urls']['https'],
                             '@UNTRUSTED_BASE@': http_fixture['urls']['untrusted'], '@WRONG_NAME_BASE@': http_fixture['urls']['wrong-name']})
    def replace(text):
        for key, value in replacements.items(): text = text.replace(key, value)
        return text
    test.write_text(replace(fixture.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
    generated_helpers = []
    for suffix, source_set in [('', 'commonTest'), ('Jvm', 'jvmTest'), ('Native', 'nativeTest')]:
        name = 'DriverProfileRuntimeFixture' + suffix + '.kt'
        destination = snapshot / 'gdal4k-runtime/src' / source_set / 'kotlin/dev/gdal4k' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(replace((root / 'verification/driver-profile' / name).read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
        if re.search(r'@[A-Z_]+@', destination.read_text(encoding='utf-8')):
            raise ValueError('Unrendered test runtime fixture placeholder')
        generated_helpers.append(destination)
    http_test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt'
    if has_capability(profile, 'http'):
        http_test.write_text(replace((root / 'verification/driver-profile/NativeHttpProfileTest.kt').read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
    combined_probe = snapshot / 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/CombinedDriverMemoryProbe.kt'
    if profile == 'combined':
        combined_probe.write_text((root / 'verification/driver-profile/CombinedDriverMemoryProbe.kt').read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
    consumer = snapshot / 'verification/consumer.py'
    consumer.write_text(consumer.read_text(encoding='utf-8').replace("for test_name in ['DatasetApiTest'", "for test_name in ['DriverProfileApiTest', 'DatasetApiTest'"), encoding='utf-8', newline='\n')
    if has_capability(profile, 'http'):
        consumer.write_text(consumer.read_text(encoding='utf-8').replace("'DriverProfileApiTest', 'DatasetApiTest'", "'DriverProfileApiTest', 'NativeHttpProfileTest', 'DatasetApiTest'"), encoding='utf-8', newline='\n')
    prepared = {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in inventory}
    prepared[test.relative_to(snapshot).as_posix()] = hashlib.sha256(test.read_bytes()).hexdigest()
    for helper in generated_helpers:
        prepared[helper.relative_to(snapshot).as_posix()] = hashlib.sha256(helper.read_bytes()).hexdigest()
    if has_capability(profile, 'http'): prepared[http_test.relative_to(snapshot).as_posix()] = hashlib.sha256(http_test.read_bytes()).hexdigest()
    if profile == 'combined': prepared[combined_probe.relative_to(snapshot).as_posix()] = hashlib.sha256(combined_probe.read_bytes()).hexdigest()
    (output / 'prepared.json').write_text(json.dumps(prepared, indent=2) + '\n', encoding='utf-8', newline='\n')
    (output / 'inputs.json').write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8', newline='\n')
    return snapshot, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PROFILES)
    parser.add_argument('target', choices=TARGETS)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--postgis-archive', type=Path,
                        help='Existing pinned Mac fixture archive; read without modifying it')
    parser.add_argument('--postgis-postgres-archive', type=Path,
                        help='Existing pinned Windows PostgreSQL ZIP; read without modifying it')
    parser.add_argument('--postgis-extension-archive', type=Path,
                        help='Existing pinned Windows PostGIS archive (official EXE archive); read without executing or modifying it')
    parser.add_argument('--postgis-runtime-archive', type=Path,
                        help='Existing pinned Microsoft Desktop VCLibs appx archive; read without installing it')
    args = parser.parse_args()
    if args.target == 'linux-arm64':
        parser.error('Use verification/driver-profile/arm64-profile.py cross-build and run phases')
    with ExitStack() as stack:
        http_fixture = None
        postgresql_fixture = None
        if has_capability(args.profile, 'postgresql') and args.target in ['macos-arm64', 'windows-amd64']:
            if args.prepare_only:
                parser.error('Owned PostGIS prepare-only requires injected metadata through prepare(); no server is started')
            windows = args.target == 'windows-amd64'
            if windows and args.postgis_archive:
                parser.error('Windows PostGIS uses PostgreSQL, PostGIS and Microsoft runtime archive options')
            if not windows and (args.postgis_postgres_archive or args.postgis_extension_archive or args.postgis_runtime_archive):
                parser.error('Windows PostgreSQL/PostGIS archive options require windows-amd64')
            manager = 'postgis-windows-fixture.py' if windows else 'postgis-fixture.py'
            pg_spec = importlib.util.spec_from_file_location('postgis_fixture', ROOT / 'verification/driver-profile' / manager)
            pg_module = importlib.util.module_from_spec(pg_spec); pg_spec.loader.exec_module(pg_module)
            public_folder = 'postgis-windows-fixtures' if windows else 'postgis-fixtures'
            pg_output = ROOT / 'verification/results' / public_folder / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            if windows:
                archives = {key: value for key, value in [('postgres', args.postgis_postgres_archive),
                            ('postgis', args.postgis_extension_archive), ('microsoft', args.postgis_runtime_archive)] if value is not None}
                if archives and set(archives) != {'postgres', 'postgis', 'microsoft'}:
                    parser.error('Supply all three locked Windows fixture archives together; partial overrides never trigger acquisition')
                postgresql_fixture = stack.enter_context(pg_module.fixture(pg_output, args.target, archives or None))
            else:
                postgresql_fixture = stack.enter_context(pg_module.fixture(pg_output, args.target, args.postgis_archive))
        if has_capability(args.profile, 'http'):
            spec = importlib.util.spec_from_file_location('native_http_fixture', ROOT / 'verification/driver-profile/http-fixture.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            output = ROOT / 'verification/results' / ('native-http-fixture-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            http_fixture = stack.enter_context(module.fixture(output))
        snapshot = run_profile(args, http_fixture, postgresql_fixture)
    if postgresql_fixture is not None:
        # Only sanitized evidence is copied. Provider, PGDATA and private credentials
        # stay outside uploaded results; the final receipt includes terminal shutdown.
        evidence = snapshot / 'verification/results/native-postgis-profile'
        shutil.copytree(postgresql_fixture['output'], evidence)
    if has_capability(args.profile, 'http') and not args.prepare_only:
        evidence = snapshot / 'verification/results/native-http-profile'
        evidence.mkdir(parents=True, exist_ok=False)
        fixture_root = Path(http_fixture['output'])
        public_files = [*fixture_root.glob('*-requests.json'), *fixture_root.glob('*.crt')]
        if args.target == 'windows-amd64':
            public_files += [fixture_root / 'certificate-tool.json',
                             *(fixture_root / (name + '-generator.json') for name in ['ca', 'trusted', 'untrusted', 'wrong-name'])]
        for file in public_files:
            shutil.copy2(file, evidence / file.name)
        checksum = 1125899906842597
        for byte in (fixture_root / 'fixture.tif').read_bytes(): checksum = (checksum * 31 + byte) & ((1 << 64) - 1)
        (evidence / 'fixture.json').write_text(json.dumps({
            'urls': http_fixture['urls'], 'imageSha256': hashlib.sha256((fixture_root / 'fixture.tif').read_bytes()).hexdigest(),
            'imageChecksum': str(checksum),
            'imageBytes': (fixture_root / 'fixture.tif').stat().st_size,
            'serverSourceSha256': hashlib.sha256((ROOT / 'verification/driver-profile/http-fixture.py').read_bytes()).hexdigest(),
            'testSourceSha256': hashlib.sha256((snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt').read_bytes()).hexdigest()
        }, indent=2) + '\n', encoding='utf-8', newline='\n')
        subprocess.run([sys.executable, str(ROOT / 'verification/driver-profile/report.py'), str(snapshot), args.profile, args.target], check=True)


def run_profile(args, http_fixture, postgresql_fixture=None):
    snapshot, identity = prepare(ROOT, args.profile, args.target, http_fixture, postgresql_fixture)
    print(json.dumps({'source': str(snapshot), 'version': identity['version']}), flush=True)
    if args.prepare_only:
        return
    system, arch, native, classifier = TARGETS[args.target]
    if (platform.system(), platform.machine().lower()) != (system, arch):
        raise ValueError('Run the profile on its matching native host; cross builds are not yet verified')
    env = os.environ.copy()
    if postgresql_fixture is not None:
        env = postgresql_child_environment(env, postgresql_fixture, args.target)
    if http_fixture:
        env['CURL_CA_BUNDLE'] = http_fixture['ca']
        env['NO_PROXY'] = ','.join(filter(None, [env.get('NO_PROXY', ''), '127.0.0.1', 'localhost']))
    for key in ['GDAL4K_BINARY_VERSION', 'VCPKG_ROOT', 'VCPKG_INSTALLED_DIR']:
        env.pop(key, None)
    # Cache downloads/binaries may be explicitly shared; source, install and CMake stay local.
    env['VCPKG_ROOT'] = str(snapshot / 'verification/tools/vcpkg')
    gradle = snapshot / ('gradlew.bat' if system == 'Windows' else 'gradlew')
    def run(*command):
        subprocess.run([str(x) for x in command], cwd=snapshot, env=env, check=True)
    if args.target == 'windows-amd64':
        run(gradle, '--console=plain', ':prepareNativeBuildTools')
        configure_windows_vcpkg_git(snapshot)
    if has_capability(args.profile, 'scientific'):
        run(sys.executable, '-c', "import h5py,numpy as np; from pathlib import Path; p=Path('verification/build/driver-profile.h5'); p.parent.mkdir(parents=True,exist_ok=True); f=h5py.File(p,'w'); f.create_dataset('values',data=np.array([[1,2],[3,4]],dtype='int16')); f.close()")
    run(gradle, '--console=plain', ':gdal4k:compileKotlin' + native,
        ':gdal4k-runtime:jvmTest', ':gdal4k-runtime:' + native[0].lower() + native[1:] + 'Test')
    run(sys.executable, snapshot / 'verification/audit-engine.py', args.target)
    if has_capability(args.profile, 'http'):
        probe = snapshot / 'gdal4k-engine/build/engine' / args.target / 'cmake' / cache_probe(args.target)
        dependency_audit = None
        if args.target == 'windows-amd64':
            path = snapshot / 'verification/results/native-http-cache-probe-pe.json'
            windows_pe_api(snapshot).audit(probe, root=snapshot, output=path)
            dependency_audit = dict(path=path.relative_to(snapshot).as_posix(), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result = subprocess.run([str(probe), http_fixture['urls']['http'] + '/cache/internal/fixture.tif'],
                                cwd=snapshot, env=env, text=True, capture_output=True)
        (snapshot / 'verification/results/native-http-cache-check.log').write_text(result.stdout + result.stderr, encoding='utf-8', newline='\n')
        if result.returncode:
            raise RuntimeError('Actual optional producer cache probe failed; inspect verification/results/native-http-cache-check.log')
        evidence = json.loads(result.stdout)
        assert evidence['result'] == 'passed' and evidence['peakCachedBytes'] <= 8 * 1024 * 1024
        evidence.update(probeSha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
                        sourceSha256=hashlib.sha256((snapshot / 'gdal4k/native/http-cache-probe.cpp').read_bytes()).hexdigest())
        if dependency_audit is not None:
            evidence['windowsDependencyAudit'] = dependency_audit
        (snapshot / 'verification/results/native-http-cache-check.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8', newline='\n')
    if has_capability(args.profile, 'parquet'):
        run(sys.executable, snapshot / 'verification/driver-profile/arrow-ipc-check.py', snapshot, args.target)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', 'jvm', '--jvm-binary', classifier)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', native[0].lower() + native[1:])
    if has_capability(args.profile, 'http'):
        library = snapshot / 'gdal4k-engine/build/jvm-resources/gdal4k' / args.target / jni_library(args.target)
        if args.profile == 'combined':
            run(sys.executable, snapshot / 'verification/combined-driver-memory-report.py', '--library', library,
                '--manifest', snapshot / 'gdal4k-engine/build/engine' / args.target / 'manifest.json')
        run(sys.executable, snapshot / 'verification/http-memory-report.py', '--library', library,
            '--manifest', snapshot / 'gdal4k-engine/build/engine' / args.target / 'manifest.json',
            '--label', 'profile-controlled', '--modes', 'download-local', 'range')
        # Final request logs are written when the owned servers stop. Reporting is deferred
        # to main after their context exits, so partial/in-flight evidence cannot pass.
        return snapshot
    run(sys.executable, ROOT / 'verification/driver-profile/report.py', snapshot, args.profile, args.target)


if __name__ == '__main__':
    main()
