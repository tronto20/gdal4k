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
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    'scientific': {'features': ['hdf5', 'netcdf'], 'drivers': ['HDF5', 'netCDF'],
                   'dependencies': [{'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']}],
                   'targets': ['linux-amd64', 'macos-arm64']},
    'parquet': {'features': ['parquet'], 'drivers': ['Parquet'],
                'targets': ['linux-amd64', 'macos-arm64']},
    'postgresql': {'features': ['postgresql'], 'drivers': ['PostgreSQL'],
                   'targets': ['linux-amd64']},
}
TARGETS = {'linux-amd64': ('Linux', 'x86_64', 'LinuxX64', 'linux-x64-gnu'),
           'macos-arm64': ('Darwin', 'arm64', 'MacosArm64', 'macos-arm64')}


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


def prepare(root, profile, target):
    spec = PROFILES[profile]
    if target not in spec['targets']:
        raise ValueError('Profile has no verified build matrix for this target: ' + target)
    properties = (root / 'gradle.properties').read_text()
    base = next(line.split('=', 1)[1] for line in properties.splitlines() if line.startswith('gdal4kVersion='))
    version = profile_version(base, profile)
    inventory = source_files(root)
    identity = {'profile': profile, 'target': target, 'version': version, 'sources': inventory}
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
    test.write_text(fixture.read_text().replace('@PROFILE@', profile).replace('@HDF5_PATH@', (snapshot / 'verification/build/driver-profile.h5').as_posix()))
    consumer = snapshot / 'verification/consumer.py'
    consumer.write_text(consumer.read_text().replace("for test_name in ['DatasetApiTest'", "for test_name in ['DriverProfileApiTest', 'DatasetApiTest'"))
    prepared = {name: hashlib.sha256((snapshot / name).read_bytes()).hexdigest() for name in inventory}
    prepared[test.relative_to(snapshot).as_posix()] = hashlib.sha256(test.read_bytes()).hexdigest()
    (output / 'prepared.json').write_text(json.dumps(prepared, indent=2) + '\n')
    (output / 'inputs.json').write_text(json.dumps(identity, indent=2) + '\n')
    return snapshot, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PROFILES)
    parser.add_argument('target', choices=TARGETS)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    snapshot, identity = prepare(ROOT, args.profile, args.target)
    print(json.dumps({'source': str(snapshot), 'version': identity['version']}), flush=True)
    if args.prepare_only:
        return
    system, arch, native, classifier = TARGETS[args.target]
    if (platform.system(), platform.machine().lower()) != (system, arch):
        raise ValueError('Run the profile on its matching native host; cross builds are not yet verified')
    env = os.environ.copy()
    for key in ['GDAL4K_BINARY_VERSION', 'VCPKG_ROOT', 'VCPKG_INSTALLED_DIR']:
        env.pop(key, None)
    # Cache downloads/binaries may be explicitly shared; source, install and CMake stay local.
    env['VCPKG_ROOT'] = str(snapshot / 'verification/tools/vcpkg')
    gradle = snapshot / 'gradlew'
    def run(*command):
        subprocess.run([str(x) for x in command], cwd=snapshot, env=env, check=True)
    if args.profile == 'scientific':
        run(sys.executable, '-c', "import h5py,numpy as np; from pathlib import Path; p=Path('verification/build/driver-profile.h5'); p.parent.mkdir(parents=True,exist_ok=True); f=h5py.File(p,'w'); f.create_dataset('values',data=np.array([[1,2],[3,4]],dtype='int16')); f.close()")
    run(gradle, '--console=plain', ':gdal4k:compileKotlin' + native,
        ':gdal4k-runtime:jvmTest', ':gdal4k-runtime:' + native[0].lower() + native[1:] + 'Test')
    run(sys.executable, snapshot / 'verification/audit-engine.py', args.target)
    if args.profile == 'parquet':
        run(sys.executable, snapshot / 'verification/driver-profile/arrow-ipc-check.py', snapshot, args.target)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', 'jvm', '--jvm-binary', classifier)
    run(sys.executable, snapshot / 'verification/consumer.py', '--target', native[0].lower() + native[1:])
    run(sys.executable, ROOT / 'verification/driver-profile/report.py', snapshot, args.profile, args.target)


if __name__ == '__main__':
    main()
