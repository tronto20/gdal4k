#!/usr/bin/env python3
"""Cross-produce combined drivers on Linux x64; execute unchanged bytes on Linux ARM64.

The transfer contains portable outputs and exact prepared source, never an installed
prefix, toolchain or build cache. Compilation receipts remain compilation receipts.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
TARGET = 'linux-arm64'


def load(name, relative, root=ROOT):
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def host(machine):
    if (platform.system(), platform.machine().lower()) != ('Linux', machine):
        raise ValueError('This phase requires actual Linux ' + machine)


def git_identity(root):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    return dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'))


def owned(root, name):
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or '..' in relative.parts or '.' in relative.parts
            or relative.as_posix() != name or '\\' in name):
        raise ValueError('Unsafe staged relative path')
    path = root.joinpath(*relative.parts)
    if any(root.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise ValueError('Staged path contains a symlink')
    return path


def elf_arm64(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01'
            or int.from_bytes(header[16:18], 'little') not in [2, 3]
            or int.from_bytes(header[18:20], 'little') != 183):
        raise ValueError('Expected an ELF64 little-endian AArch64 executable')


def prepared_snapshot(root, output):
    source = output / 'source'
    inputs = json.loads((output / 'inputs.json').read_text())
    if (inputs['profile'], inputs['target'], inputs.get('runtimeFixtures')) != (
            'combined', TARGET, 'required-on-execution-host'):
        raise ValueError('Wrong prepared ARM runtime fixture contract')
    api = load('arm_profile_source', 'gdal4k-engine/tools/driver-profile.py', root)
    if api.source_files(root) != inputs['sources']:
        raise ValueError('Current checkout source differs from the cross producer')
    prepared = json.loads((output / 'prepared.json').read_text())
    for name, expected in prepared.items():
        path = owned(source, name)
        if not path.is_file() or sha(path) != expected:
            raise ValueError('Prepared ARM source changed: ' + name)
    return source, inputs, prepared


def engine_files(root):
    build = root / 'gdal4k-engine/build'
    manifest = build / f'engine/{TARGET}/manifest.json'
    data = json.loads(manifest.read_text())
    if data.get('target') != TARGET or data.get('gdal') != '3.12.4' or data.get('jxl') is not True:
        raise ValueError('Wrong current ARM engine manifest')
    files = {manifest}
    for name in ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json'):
        metadata = owned(build, f'engine/{TARGET}/{name}')
        if not metadata.is_file():
            raise ValueError('Missing current ARM SDK input metadata')
        files.add(metadata)
    for name, digest in data['artifacts'].items():
        path = owned(build, name)
        if not path.is_file() or sha(path) != digest:
            raise ValueError('Current ARM engine payload hash mismatch')
        files.add(path)
    return files


def create_transfer(root, snapshot, destination):
    output = snapshot.parent
    _, _, prepared = prepared_snapshot(root, output)
    relative_output = output.relative_to(root).as_posix()
    files = {snapshot / name for name in prepared}
    files |= {output / 'inputs.json', output / 'prepared.json'}
    files |= engine_files(root) | engine_files(snapshot)
    source_binary = snapshot / 'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'
    consumers = list((snapshot / 'verification/results/published-consumer-linuxArm64/bin').glob('**/test.kexe'))
    if len(consumers) != 1:
        raise ValueError('Exactly one independent cross-linked Native consumer is required')
    for path in [source_binary, consumers[0]]:
        elf_arm64(path)
        files.add(path)
    native_folder = snapshot / 'verification/results/published-consumer-linuxArm64'
    files |= {p for p in native_folder.rglob('*') if p.is_file()}
    files |= {p for p in (snapshot / 'verification/results/arm64-commands').rglob('*') if p.is_file()}
    result = json.loads((native_folder / 'result.json').read_text())
    if not (result['result'] == 'passed' and result['compiled_only'] is True
            and result['producer_paths_hidden_count'] == 3):
        raise ValueError('Missing independently linked compile-only consumer evidence')
    repo = snapshot / 'verification/build/consumer-maven'
    files |= {p for p in repo.rglob('*') if p.is_file()}
    if not any(p.suffix == '.pom' for p in files if p.is_relative_to(repo)):
        raise ValueError('Missing isolated profile Maven outputs')
    for name in ['verification/build/arrow-ipc-regression/arrow-ipc-check',
                 'verification/results/arrow-ipc-build-linux-arm64.json',
                 f'gdal4k-engine/build/engine/{TARGET}/cmake/gdal4k_http_probe']:
        path = snapshot / name
        if not path.is_file():
            raise ValueError('Missing actual cross-produced probe: ' + name)
        files.add(path)
    metadata = {}
    for path in sorted(files):
        name = path.relative_to(root).as_posix()
        if owned(root, name) != path or not path.is_file():
            raise ValueError('Transfer supports regular owned files only')
        metadata[name] = dict(sha256=sha(path), bytes=path.stat().st_size, mode=path.stat().st_mode & 0o777)
    receipt = dict(schemaVersion=1, target=TARGET, profile='combined', producerHost=dict(system=platform.system(), machine=platform.machine()),
                   git=git_identity(root), output=relative_output, sourceBinary=source_binary.relative_to(root).as_posix(),
                   consumerBinary=consumers[0].relative_to(root).as_posix(), files=metadata,
                   scope='Cross compilation only; no producer-host ARM Native/JVM execution')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Refusing to replace an existing ARM transfer')
    payload = json.dumps(receipt, sort_keys=True, indent=2).encode()
    with tarfile.open(destination, 'w:gz') as archive:
        member = tarfile.TarInfo('arm64-transfer.json'); member.size = len(payload); member.mode = 0o644
        archive.addfile(member, io.BytesIO(payload))
        for name in metadata:
            archive.add(root / name, arcname=name, recursive=False)
    save(destination.with_suffix(destination.suffix + '.json'), dict(sha256=sha(destination), bytes=destination.stat().st_size,
                                                                  transfer=receipt))
    return receipt


def restore_transfer(root, archive_path):
    """Audit every member before writing; no links or tracked-checkout replacement."""
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if not members or members[0].name != 'arm64-transfer.json' or not members[0].isfile() or members[0].size > 2**24:
            raise ValueError('Missing bounded ARM transfer manifest')
        receipt = json.load(archive.extractfile(members[0]))
        if (receipt.get('schemaVersion'), receipt.get('target'), receipt.get('profile')) != (1, TARGET, 'combined'):
            raise ValueError('Wrong ARM transfer schema/target')
        if receipt['producerHost'] != dict(system='Linux', machine='x86_64') or receipt['git'] != git_identity(root):
            raise ValueError('ARM transfer producer or current Git identity differs')
        output_name = receipt['output']
        if not re.fullmatch(r'build/driver-profiles/combined/linux-arm64/[0-9a-f]{64}', output_name):
            raise ValueError('Unsafe isolated profile output')
        expected = receipt['files']
        if len(members) != len(expected) + 1 or len({m.name for m in members}) != len(members):
            raise ValueError('Duplicate or incomplete ARM transfer inventory')
        for member in members[1:]:
            name = member.name
            path = owned(root, name)
            allowed = name.startswith(output_name + '/') or name.startswith('gdal4k-engine/build/')
            if not allowed or not member.isfile() or name not in expected or path.exists():
                raise ValueError('ARM transfer refuses a link, unexpected file or existing destination')
            item = expected[name]
            if member.size != item['bytes'] or member.mode & 0o777 != item['mode'] or member.mode & 0o7000:
                raise ValueError('ARM transfer size or mode mismatch')
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                    raise ValueError('ARM transfer member hash mismatch')
        for member in members[1:]:
            path = owned(root, member.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            path.chmod(expected[member.name]['mode'])
    snapshot, _, _ = prepared_snapshot(root, root / receipt['output'])
    engine_files(root); engine_files(snapshot)
    for name in ['sourceBinary', 'consumerBinary']:
        elf_arm64(owned(root, receipt[name]))
    save(snapshot / 'verification/results/arm64-transfer-restored.json',
         dict(archiveSha256=sha(archive_path), transfer=receipt, actualHost=dict(system=platform.system(), machine=platform.machine())))
    return snapshot, receipt


def native_cases(text):
    starts = re.findall(r'^\[==========\] Running (\d+) tests from (\d+) test cases\.$', text, re.M)
    ends = re.findall(r'^\[==========\] (\d+) tests from (\d+) test cases ran\. .*$', text, re.M)
    passed = re.findall(r'^\[  PASSED  \] (\d+) tests\.$', text, re.M)
    runs = re.findall(r'^\[ RUN      \] (\S+)$', text, re.M)
    oks = re.findall(r'^\[       OK \] (\S+) \(.*\)$', text, re.M)
    if (len(starts) != 1 or ends != starts or len(passed) != 1 or int(passed[0]) != int(starts[0][0])
            or not runs or runs != oks or len(runs) != len(set(runs)) or len(runs) != int(passed[0])
            or re.search(r'\[(?:\s*FAILED|\s*SKIPPED)\s*\]|DISABLED_|Uncaught|uncaught exception', text)):
        raise ValueError('Incomplete or failed actual Native runtime stdout')
    fixtures = [name for name in runs if '.DriverProfileApiTest.' in name]
    http = [name for name in runs if '.NativeHttpProfileTest.' in name]
    if len(fixtures) != 1 or not fixtures[0].endswith('.requiredDriversAndRealDataRoundTrip') or len(http) != 10:
        raise ValueError('Actual Native profile and ten HTTP cases are required')
    return dict(tests=len(runs), failures=0, errors=0, skipped=0, cases=runs,
                fixture=fixtures[0].rsplit('.', 1)[-1], httpCases=[name.rsplit('.', 1)[-1] for name in http])


def command(root, args, env=None, timeout=10800):
    api = load('arm_command_evidence', 'verification/command_evidence.py', root)
    return api.run_with_evidence(args, cwd=root, directory=root / 'verification/results/arm64-commands', timeout=timeout, env=env)


def gradle(root, *tasks, env=None):
    return command(root, [root / 'gradlew', '--no-daemon', '--console=plain', '--init-script',
                          root / 'verification/ci-diagnostics.init.gradle', *tasks], env)


def cross_build(root, destination):
    host('x86_64')
    api = load('arm_cross_profile', 'gdal4k-engine/tools/driver-profile.py', root)
    snapshot, identity = api.prepare(root, 'combined', TARGET, runtime_fixtures=True)
    env = dict(os.environ)
    for name in ['VCPKG_ROOT', 'VCPKG_INSTALLED_DIR', 'GDAL4K_BINARY_VERSION', 'ORG_GRADLE_PROJECT_gdal4kVerifiedStagedEngine']:
        env.pop(name, None)
    gradle(snapshot, ':gdal4k:compileKotlinLinuxArm64', ':gdal4k-runtime:linkDebugTestLinuxArm64', env=env)
    consumer = snapshot / 'verification/consumer.py'
    command(snapshot, [sys.executable, consumer, '--target', 'linuxArm64', '--compile-only', '--command-timeout-seconds', '7200'], env)
    repo = snapshot / 'verification/build/consumer-maven'
    tasks = [f':{module}:publish{publication}PublicationToMavenLocal' for module in ['gdal4k', 'gdal4k-runtime']
             for publication in ['KotlinMultiplatform', 'Jvm']]
    tasks += [':gdal4k-engine:publishKotlinMultiplatformPublicationToMavenLocal']
    gradle(snapshot, f'-Dmaven.repo.local={repo}', '-Pgdal4kJvmClassifiers=linux-arm64-gnu', *tasks, env=env)
    ipc = load('arm_cross_ipc', 'verification/driver-profile/arrow-ipc-check.py', snapshot)
    ipc.compile_staged(snapshot)
    create_transfer(root, snapshot, destination)


def fixture_markers(text, expected):
    api = load('arm_runtime_contract', 'verification/driver-profile/runtime-fixture.py')
    markers = re.findall(r'^' + re.escape(api.MARKER) + r'(.+)$', text, re.M)
    if len(markers) != 1:
        raise ValueError('Exactly one actual runtime fixture marker is required per process')
    marker = json.loads(markers[0])
    if set(marker) != {'configuration', 'contextKey'} or api.validate(marker['configuration']) != expected:
        raise ValueError('Actual runtime fixture configuration differs')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', marker['contextKey']):
        raise ValueError('Invalid actual fixture context key')
    return marker


def native_execute(snapshot, executable, label, configuration, env, compile_receipt=None):
    elf_arm64(executable)
    digest = sha(executable)
    receipt = command(snapshot, [executable], env, timeout=1800)
    metadata = json.loads(receipt.read_text())
    raw_path = receipt.with_name(metadata['log'])
    raw = raw_path.read_text(encoding='utf-8')
    counts = native_cases(raw)
    marker = fixture_markers(raw, configuration)
    if sha(executable) != digest:
        raise ValueError('Actual Native executable changed during execution')
    value = dict(result='passed', actual_execution=True, compiled_only=False, target=TARGET,
                 host=dict(system=platform.system(), machine=platform.machine()),
                 executable=executable.relative_to(snapshot).as_posix(), executableSha256=digest,
                 command=receipt.relative_to(snapshot).as_posix(), commandSha256=sha(receipt),
                 log=raw_path.relative_to(snapshot).as_posix(), logSha256=sha(raw_path), counts=counts,
                 runtimeFixture=marker, compilationReceipt=compile_receipt)
    save(snapshot / f'verification/results/arm64-native-{label}.json', value)


def preserve_http(snapshot, fixture):
    source = Path(fixture['output'])
    folder = snapshot / 'verification/results/native-http-profile'
    folder.mkdir(parents=True, exist_ok=False)
    for path in [*source.glob('*-requests.json'), *source.glob('*.crt')]:
        shutil.copy2(path, folder / path.name)
    checksum = 1125899906842597
    with (source / 'fixture.tif').open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            for byte in chunk:
                checksum = (checksum * 31 + byte) & ((1 << 64) - 1)
    save(folder / 'fixture.json', dict(urls=fixture['urls'], imageSha256=sha(source / 'fixture.tif'),
         imageChecksum=str(checksum), imageBytes=(source / 'fixture.tif').stat().st_size,
         serverSourceSha256=sha(ROOT / 'verification/driver-profile/http-fixture.py'),
         testSourceSha256=sha(snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt')))


def execute(root, archive):
    host('aarch64')
    snapshot, transfer = restore_transfer(root, archive)
    command(root, [sys.executable, root / 'gdal4k-engine/tools/build-engine.py', TARGET, '--verify-staged-only'], timeout=300)
    command(snapshot, [sys.executable, snapshot / 'gdal4k-engine/tools/build-engine.py', TARGET, '--verify-staged-only'], timeout=300)
    api = load('arm_runtime_profile', 'gdal4k-engine/tools/driver-profile.py', root)
    contract = load('arm_fixture_contract', 'verification/driver-profile/runtime-fixture.py', root)
    provider = load('arm_postgis_fixture', 'verification/driver-profile/postgis-arm64-fixture.py', root)
    http_api = load('arm_http_fixture', 'verification/driver-profile/http-fixture.py', root)
    run_id = 'arm64_' + uuid.uuid4().hex
    output = root / 'verification/results/arm64-fixtures' / run_id
    with ExitStack() as stack:
        pg = stack.enter_context(provider.fixture(output / 'postgis'))
        http = stack.enter_context(http_api.fixture(output / 'http'))
        fixture = snapshot / 'verification/build/driver-profile.h5'
        fixture.parent.mkdir(parents=True, exist_ok=True)
        import h5py
        import numpy as np
        with h5py.File(fixture, 'w') as file:
            file.create_dataset('values', data=np.array([[1, 2], [3, 4]], dtype='int16'))
        configuration = contract.validate(dict(schemaVersion=1, profile='combined', target=TARGET, runId=run_id,
                                              postgresqlDsn=pg['dsn'], httpUrls=http['urls'], hdf5Path=str(fixture.resolve())))
        save(snapshot / 'verification/results/arm64-runtime-configuration.json', configuration)
        env = api.postgresql_child_environment(os.environ, pg)
        env.update({contract.ENVIRONMENT_VARIABLE: contract.canonical_json(configuration), 'CURL_CA_BUNDLE': http['ca'],
                    'NO_PROXY': '127.0.0.1,localhost', 'ORG_GRADLE_PROJECT_gdal4kVerifiedStagedEngine': TARGET})
        for name in ['VCPKG_ROOT', 'VCPKG_INSTALLED_DIR', 'GDAL4K_BINARY_VERSION']:
            env.pop(name, None)
        # The ordinary task verifies every staged engine input before any JVM test.
        gradle(snapshot, ':gdal4k:jvmTest', ':gdal4k-runtime:jvmTest', env=env)
        consumer = snapshot / 'verification/consumer.py'
        command(snapshot, [sys.executable, consumer, '--target', 'jvm', '--jvm-binary', 'linux-arm64-gnu',
                           '--no-publish', '--command-timeout-seconds', '7200'], env)
        original_compile = snapshot / 'verification/results/published-consumer-linuxArm64/result.json'
        compile_info = dict(path=original_compile.relative_to(snapshot).as_posix(), sha256=sha(original_compile))
        native_execute(snapshot, root / transfer['sourceBinary'], 'source', configuration, env)
        native_execute(snapshot, root / transfer['consumerBinary'], 'consumer', configuration, env, compile_info)
        command(snapshot, [sys.executable, snapshot / 'verification/audit-engine.py', TARGET], env)
        probe = snapshot / f'gdal4k-engine/build/engine/{TARGET}/cmake/gdal4k_http_probe'
        elf_arm64(probe)
        cache_command = command(snapshot, [probe, http['urls']['http'] + '/cache/internal/fixture.tif'], env, timeout=120)
        cache_meta = json.loads(cache_command.read_text())
        cache_log = cache_command.with_name(cache_meta['log'])
        cache = json.loads(cache_log.read_text())
        if cache['result'] != 'passed' or not 0 < cache['peakCachedBytes'] <= 8388608:
            raise ValueError('Actual ARM HTTP cache probe failed')
        cache.update(probeSha256=sha(probe), sourceSha256=sha(snapshot / 'gdal4k/native/http-cache-probe.cpp'),
                     actual_execution=True, command=cache_command.relative_to(snapshot).as_posix(), commandSha256=sha(cache_command))
        shutil.copy2(cache_log, snapshot / 'verification/results/native-http-cache-check.log')
        save(snapshot / 'verification/results/native-http-cache-check.json', cache)
        ipc = load('arm_runtime_ipc', 'verification/driver-profile/arrow-ipc-check.py', snapshot)
        # run_staged inherits only this owned fixture environment; IPC has no network input.
        ipc.run_staged(snapshot, snapshot / 'verification/build/arrow-ipc-regression/arrow-ipc-check',
                       snapshot / 'verification/results/arrow-ipc-build-linux-arm64.json')
        library = snapshot / f'gdal4k-engine/build/jvm-resources/gdal4k/{TARGET}/libgdal4k.so'
        manifest = snapshot / f'gdal4k-engine/build/engine/{TARGET}/manifest.json'
        command(snapshot, [sys.executable, snapshot / 'verification/combined-driver-memory-report.py',
                           '--library', library, '--manifest', manifest], env, timeout=1800)
        command(snapshot, [sys.executable, snapshot / 'verification/http-memory-report.py', '--library', library,
                           '--manifest', manifest, '--label', 'profile-controlled', '--modes', 'download-local', 'range'], env, timeout=1800)
    # A report is permitted only after both owned providers have closed.
    shutil.copytree(pg['output'], snapshot / 'verification/results/native-postgis-profile')
    preserve_http(snapshot, http)
    prepared_snapshot(root, snapshot.parent)
    command(snapshot, [sys.executable, root / 'verification/driver-profile/report.py', snapshot, 'combined', TARGET], timeout=1800)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['cross-build', 'run'])
    parser.add_argument('--transfer', type=Path, required=True)
    args = parser.parse_args()
    if args.phase == 'cross-build':
        cross_build(ROOT, args.transfer.resolve())
    else:
        execute(ROOT, args.transfer.resolve())


if __name__ == '__main__':
    main()
