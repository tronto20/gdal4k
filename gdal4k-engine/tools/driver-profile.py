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
                 'targets': ['linux-amd64', 'macos-arm64']},
}
TARGETS = {'linux-amd64': ('Linux', 'x86_64', 'LinuxX64', 'linux-x64-gnu'),
           'macos-arm64': ('Darwin', 'arm64', 'MacosArm64', 'macos-arm64')}


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
        if target == 'macos-arm64':
            raise ValueError('Mac PostgreSQL requires its owned running PostGIS fixture')
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


def postgresql_child_environment(environment, fixture):
    """Pass owned libpq authentication to tests without sharing the server environment."""
    result = {key: value for key, value in environment.items() if not key.startswith('PG')}
    private = fixture['env']
    if not private.get('PGPASSFILE') or not set(private) <= {'PGPASSFILE', 'PGCONNECT_TIMEOUT'}:
        raise ValueError('Unexpected owned PostGIS child environment')
    password_file = Path(private['PGPASSFILE'])
    if (not password_file.is_absolute() or not password_file.is_file()
            or password_file.is_symlink() or password_file.stat().st_mode & 0o077):
        raise ValueError('Owned PGPASSFILE must be a private regular file')
    result.update(private)
    return result


def prepare(root, profile, target, http_fixture=None, postgresql_fixture=None):
    spec = PROFILES[profile]
    if target not in spec['targets']:
        raise ValueError('Profile has no verified build matrix for this target: ' + target)
    pg_identity = postgresql_identity(profile, target, postgresql_fixture)
    properties = (root / 'gradle.properties').read_text()
    base = next(line.split('=', 1)[1] for line in properties.splitlines() if line.startswith('gdal4kVersion='))
    version = profile_version(base, profile)
    inventory = source_files(root)
    identity = {'profile': profile, 'target': target, 'version': version, 'sources': inventory}
    if pg_identity is not None:
        identity['postgresqlFixture'] = pg_identity
    if has_capability(profile, 'http'):
        if not http_fixture:
            raise ValueError('HTTP profile requires its owned running HTTP/TLS fixture')
        identity['httpFixtureUrls'] = http_fixture['urls']
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    output = root / 'build/driver-profiles' / profile / target / digest
    snapshot = output / 'source'
    if output.exists():
        if output.is_symlink() or not (output / 'inputs.json').is_file() or json.loads((output / 'inputs.json').read_text()) != identity:
            raise ValueError('Refusing to overwrite an unrecognized profile directory: ' + str(output))
        for name, expected in json.loads((output / 'prepared.json').read_text()).items():
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
    value = json.loads(manifest.read_text())
    dependency = next(d for d in value['dependencies'] if isinstance(d, dict) and d['name'] == 'gdal')
    dependency['features'] = sorted(set(dependency['features'] + spec['features']))
    value['dependencies'].extend(spec.get('dependencies', []))
    manifest.write_text(json.dumps(value, indent=2) + '\n')
    updated = '\n'.join('gdal4kVersion=' + version if line.startswith('gdal4kVersion=') else line for line in properties.splitlines()) + '\n'
    (snapshot / 'gradle.properties').write_text(updated)
    # Profile snapshots are local/CI artifacts. Keep release credentials out of their use.
    with (snapshot / 'build.gradle.kts').open('a') as file:
        file.write('''\n// Isolated optional driver profile: remote publication is deliberately disabled.
gradle.taskGraph.whenReady {
    require(allTasks.none { it.name.startsWith("publish") && !it.name.endsWith("ToMavenLocal") }) {
        "Optional driver snapshots support local Maven publication only"
    }
}
''')
    fixture = root / 'verification/driver-profile/DriverProfileApiTest.kt'
    test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
    replacements = {'@PROFILE@': profile, '@HDF5_PATH@': (snapshot / 'verification/build/driver-profile.h5').as_posix(),
                    '@POSTGRESQL_DSN@': pg_identity['dsn'] if pg_identity else
                    'PG:host=127.0.0.1 port=5432 dbname=gdal4k user=gdal4k password=profile-fixture'}
    if has_capability(profile, 'http'):
        replacements.update({'@HTTP_BASE@': http_fixture['urls']['http'], '@HTTPS_BASE@': http_fixture['urls']['https'],
                             '@UNTRUSTED_BASE@': http_fixture['urls']['untrusted'], '@WRONG_NAME_BASE@': http_fixture['urls']['wrong-name']})
    def replace(text):
        for key, value in replacements.items(): text = text.replace(key, value)
        return text
    test.write_text(replace(fixture.read_text()))
    http_test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt'
    if has_capability(profile, 'http'):
        http_test.write_text(replace((root / 'verification/driver-profile/NativeHttpProfileTest.kt').read_text()))
    combined_probe = snapshot / 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/CombinedDriverMemoryProbe.kt'
    if profile == 'combined':
        combined_probe.write_text((root / 'verification/driver-profile/CombinedDriverMemoryProbe.kt').read_text())
    consumer = snapshot / 'verification/consumer.py'
    consumer.write_text(consumer.read_text().replace("for test_name in ['DatasetApiTest'", "for test_name in ['DriverProfileApiTest', 'DatasetApiTest'"))
    if has_capability(profile, 'http'):
        consumer.write_text(consumer.read_text().replace("'DriverProfileApiTest', 'DatasetApiTest'", "'DriverProfileApiTest', 'NativeHttpProfileTest', 'DatasetApiTest'"))
    prepared = {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in inventory}
    prepared[test.relative_to(snapshot).as_posix()] = hashlib.sha256(test.read_bytes()).hexdigest()
    if has_capability(profile, 'http'): prepared[http_test.relative_to(snapshot).as_posix()] = hashlib.sha256(http_test.read_bytes()).hexdigest()
    if profile == 'combined': prepared[combined_probe.relative_to(snapshot).as_posix()] = hashlib.sha256(combined_probe.read_bytes()).hexdigest()
    (output / 'prepared.json').write_text(json.dumps(prepared, indent=2) + '\n')
    (output / 'inputs.json').write_text(json.dumps(identity, indent=2) + '\n')
    return snapshot, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PROFILES)
    parser.add_argument('target', choices=TARGETS)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--postgis-archive', type=Path,
                        help='Existing pinned Mac fixture archive; read without modifying it')
    args = parser.parse_args()
    with ExitStack() as stack:
        http_fixture = None
        postgresql_fixture = None
        if has_capability(args.profile, 'postgresql') and args.target == 'macos-arm64':
            if args.prepare_only:
                parser.error('Mac PostGIS prepare-only requires injected metadata through prepare(); no server is started')
            pg_spec = importlib.util.spec_from_file_location('postgis_fixture', ROOT / 'verification/driver-profile/postgis-fixture.py')
            pg_module = importlib.util.module_from_spec(pg_spec); pg_spec.loader.exec_module(pg_module)
            pg_output = ROOT / 'verification/results/postgis-fixtures' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
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
        for file in [*fixture_root.glob('*-requests.json'), *fixture_root.glob('*.crt')]:
            shutil.copy2(file, evidence / file.name)
        checksum = 1125899906842597
        for byte in (fixture_root / 'fixture.tif').read_bytes(): checksum = (checksum * 31 + byte) & ((1 << 64) - 1)
        (evidence / 'fixture.json').write_text(json.dumps({
            'urls': http_fixture['urls'], 'imageSha256': hashlib.sha256((fixture_root / 'fixture.tif').read_bytes()).hexdigest(),
            'imageChecksum': str(checksum),
            'imageBytes': (fixture_root / 'fixture.tif').stat().st_size,
            'serverSourceSha256': hashlib.sha256((ROOT / 'verification/driver-profile/http-fixture.py').read_bytes()).hexdigest(),
            'testSourceSha256': hashlib.sha256((snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt').read_bytes()).hexdigest()
        }, indent=2) + '\n')
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
        env = postgresql_child_environment(env, postgresql_fixture)
    if http_fixture:
        env['CURL_CA_BUNDLE'] = http_fixture['ca']
        env['NO_PROXY'] = ','.join(filter(None, [env.get('NO_PROXY', ''), '127.0.0.1', 'localhost']))
    for key in ['GDAL4K_BINARY_VERSION', 'VCPKG_ROOT', 'VCPKG_INSTALLED_DIR']:
        env.pop(key, None)
    # Cache downloads/binaries may be explicitly shared; source, install and CMake stay local.
    env['VCPKG_ROOT'] = str(snapshot / 'verification/tools/vcpkg')
    gradle = snapshot / 'gradlew'
    def run(*command):
        subprocess.run([str(x) for x in command], cwd=snapshot, env=env, check=True)
    if has_capability(args.profile, 'scientific'):
        run(sys.executable, '-c', "import h5py,numpy as np; from pathlib import Path; p=Path('verification/build/driver-profile.h5'); p.parent.mkdir(parents=True,exist_ok=True); f=h5py.File(p,'w'); f.create_dataset('values',data=np.array([[1,2],[3,4]],dtype='int16')); f.close()")
    run(gradle, '--console=plain', ':gdal4k:compileKotlin' + native,
        ':gdal4k-runtime:jvmTest', ':gdal4k-runtime:' + native[0].lower() + native[1:] + 'Test')
    run(sys.executable, snapshot / 'verification/audit-engine.py', args.target)
    if has_capability(args.profile, 'http'):
        probe = snapshot / 'gdal4k-engine/build/engine' / args.target / 'cmake/gdal4k_http_probe'
        result = subprocess.run([str(probe), http_fixture['urls']['http'] + '/cache/internal/fixture.tif'],
                                cwd=snapshot, env=env, text=True, capture_output=True)
        (snapshot / 'verification/results/native-http-cache-check.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Actual optional producer cache probe failed; inspect verification/results/native-http-cache-check.log')
        evidence = json.loads(result.stdout)
        assert evidence['result'] == 'passed' and evidence['peakCachedBytes'] <= 8 * 1024 * 1024
        evidence.update(probeSha256=hashlib.sha256(probe.read_bytes()).hexdigest(),
                        sourceSha256=hashlib.sha256((snapshot / 'gdal4k/native/http-cache-probe.cpp').read_bytes()).hexdigest())
        (snapshot / 'verification/results/native-http-cache-check.json').write_text(json.dumps(evidence, indent=2) + '\n')
    if has_capability(args.profile, 'parquet'):
        run(sys.executable, snapshot / 'verification/driver-profile/arrow-ipc-check.py', snapshot, args.target)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', 'jvm', '--jvm-binary', classifier)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', native[0].lower() + native[1:])
    if has_capability(args.profile, 'http'):
        library = snapshot / 'gdal4k-engine/build/jvm-resources/gdal4k' / args.target / ('libgdal4k.dylib' if system == 'Darwin' else 'libgdal4k.so')
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
