"""Archive only isolated local publications and measured evidence, never build dependencies."""
import gzip
import hashlib
import importlib.util
import json
import ntpath
from pathlib import Path
import re
import shlex
import statistics
import struct
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[2]
profile_spec = importlib.util.spec_from_file_location('driver_profile', ROOT / 'gdal4k-engine/tools/driver-profile.py')
profile_api = importlib.util.module_from_spec(profile_spec)
profile_spec.loader.exec_module(profile_api)
has_capability = profile_api.has_capability

def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()

class CountOutput:
    def __init__(self): self.count = 0
    def write(self, data): self.count += len(data); return len(data)

def measure(path):
    destination = CountOutput()
    with gzip.GzipFile(fileobj=destination, mode='wb', mtime=0) as compressed, path.open('rb') as source:
        while chunk := source.read(1024 * 1024): compressed.write(chunk)
    return {'bytes': path.stat().st_size, 'gzipBytes': destination.count}


def publication_bindings(snapshot, repo, version, target, artifacts):
    """Bind the published files to this producer, rather than filenames alone."""
    files = {p.relative_to(repo).as_posix(): {'bytes': p.stat().st_size, 'sha256': sha(p)}
             for p in sorted(repo.rglob('*')) if p.is_file()}
    assert files, 'Missing isolated publications'
    coordinates = set()
    for pom in repo.rglob('*.pom'):
        xml = ET.parse(pom).getroot()
        ns = '{http://maven.apache.org/POM/4.0.0}'
        assert xml.findtext(ns + 'groupId') == 'dev.tronto.gdal4k'
        coordinate = xml.findtext(ns + 'artifactId')
        assert xml.findtext(ns + 'version') == version and pom.parent.name == version
        coordinates.add(coordinate)
        for dependency in xml.findall('.//' + ns + 'dependency'):
            if dependency.findtext(ns + 'groupId') == 'dev.tronto.gdal4k':
                assert dependency.findtext(ns + 'version') == version, 'Mixed profile dependency version'
    native = profile_api.TARGETS[target][2].lower()
    engine = profile_api.TARGETS[target][3]
    expected = {m + suffix for m in ['gdal4k', 'gdal4k-runtime'] for suffix in ['', '-jvm', '-' + native]}
    assert coordinates == expected | {'gdal4k-engine', 'gdal4k-engine-native-' + engine}, coordinates
    base = repo / 'dev/tronto/gdal4k'
    production = {}
    for module in ['gdal4k', 'gdal4k-runtime']:
        current = {p.relative_to(snapshot).as_posix(): sha(p)
                   for p in (snapshot / module / 'build/classes/kotlin/jvm/main').rglob('*.class')}
        assert current, 'Missing compiled production classes: ' + module
        jar = base / (module + '-jvm') / version / f'{module}-jvm-{version}.jar'
        with zipfile.ZipFile(jar) as archive:
            packaged = {f'{module}/build/classes/kotlin/jvm/main/{name}': hashlib.sha256(archive.read(name)).hexdigest()
                        for name in archive.namelist() if name.endswith('.class')}
        assert packaged == current, 'Published classes differ from the current producer: ' + module
        production.update(packaged)
    classifier = profile_api.TARGETS[target][3]
    library = profile_api.jni_library(target)
    resource = f'gdal4k/{target}/{library}'
    jar = base / 'gdal4k-engine' / version / f'gdal4k-engine-{version}-{classifier}.jar'
    with zipfile.ZipFile(jar) as archive:
        assert {n for n in archive.namelist() if not n.endswith('/') and n != 'META-INF/MANIFEST.MF'} == {resource}
        with archive.open(resource) as file:
            jni_hash = hashlib.file_digest(file, 'sha256').hexdigest()
    assert jni_hash == artifacts['jvm-resources/' + resource]
    module = 'gdal4k-engine-native-' + engine
    klib = base / module / version / f'{module}-{version}-cinterop-engine.klib'
    with zipfile.ZipFile(klib) as archive:
        members = [n for n in archive.namelist() if n.endswith('/included/libgdal4k.a')]
        assert len(members) == 1
        with archive.open(members[0]) as file:
            native_hash = hashlib.file_digest(file, 'sha256').hexdigest()
        assert 'libraryPaths=' not in archive.read('default/manifest').decode()
    assert native_hash == artifacts[f'engine/{target}/libgdal4k.a']
    return dict(coordinates=sorted(coordinates), artifactSha256=files,
                compiledProductionSha256=production, jniSha256=jni_hash, nativeSha256=native_hash)


def combined_execution_bindings(snapshot, data):
    """Bind command/PID/exit receipts omitted by Darwin's raw time output."""
    if data['target'] == 'windows-amd64':
        return windows_execution_bindings(snapshot, data)
    snapshot = snapshot.resolve()
    hosts = {'macos-arm64': ('Darwin', 'arm64', '-l', 'Darwin time -l', 'bytes', 1, 'libgdal4k.dylib'),
             'linux-amd64': ('Linux', 'x86_64', '-v', 'GNU time -v', 'KiB', 1024, 'libgdal4k.so'),
             'linux-arm64': ('Linux', 'aarch64', '-v', 'GNU time -v', 'KiB', 1024, 'libgdal4k.so')}
    assert data['target'] in hosts, 'Unsupported combined command host'
    system, machine, flag, time_format, unit, multiplier, library = hosts[data['target']]
    assert (data['system'], data['machine'].lower()) == (system, machine), 'Combined command host metadata mismatch'
    assert isinstance(data['platform'], str) and data['platform']
    assert data['osRss'] == dict(timeExecutable='/usr/bin/time', timeArgument=flag, timeFormat=time_format,
                                 reportedUnit=unit, multiplierToBytes=multiplier), 'Combined time format/unit mismatch'
    cp = json.loads((snapshot / 'gdal4k-runtime/build/combined-driver-memory-classpath.json').read_text(encoding='utf-8'))
    assert Path(cp['java']).is_absolute() and Path(cp['java']).name == 'java', 'Invalid combined Java executable'
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    assert data['settings']['jvm'] == settings, 'Combined JVM settings changed'
    logs = {}
    for record in data['records']:
        relative = Path(record['executionLog'])
        assert (not relative.is_absolute() and '..' not in relative.parts
                and relative.parts[:3] == ('verification', 'results', 'combined-driver-memory'))
        assert relative.parent == Path(record['log']).parent == Path(record['rssLog']).parent
        assert type(record['trial']) is int and relative.name == f"{record['trial']}.command.json"
        assert relative.as_posix() not in logs, 'Duplicate combined command receipt'
        path = snapshot / relative
        assert not any(snapshot.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1))
        assert sha(path) == record['executionLogSha256'], 'Combined command receipt changed'
        execution = json.loads(path.read_text(encoding='utf-8'))
        assert execution == record['execution'], 'Combined embedded command receipt differs'
        assert (execution['status'] == 'passed' and type(execution['returncode']) is int
                and execution['returncode'] == 0), 'Missing successful owned combined command'
        assert (type(execution['pid']) is int and execution['pid'] > 0
                and type(execution['observedProcessGroupId']) is int
                and execution['observedProcessGroupId'] == execution['pid']), 'Combined owned PID/PGID mismatch'
        assert type(execution['parentPid']) is int and execution['parentPid'] > 0
        assert execution['pidRole'] == '/usr/bin/time wrapper; JVM PID is not observed'
        assert execution['requestedNewSession'] is True and execution['timeoutSeconds'] == 600
        assert execution['environmentOverrides'] == {'LC_ALL': 'C'} and execution['cwd'] == str(snapshot)
        assert execution['startedAtUtc'] and execution['completedAtUtc'] and execution['elapsedSeconds'] > 0
        library_path = snapshot / f"gdal4k-engine/build/jvm-resources/gdal4k/{data['target']}/{library}"
        expected = ['/usr/bin/time', flag, '-o', str(snapshot / record['rssLog']), cp['java'], *settings,
                    '-cp', cp['classpath'], 'dev.gdal4k.CombinedDriverMemoryProbe', str(library_path.resolve())]
        assert execution['command'] == expected, 'Combined time/Java/probe/library command mismatch'
        logs[relative.as_posix()] = record['executionLogSha256']
    return logs


def windows_execution_bindings(snapshot, data):
    """Bind final kernel memory evidence to each exact, directly owned JVM."""
    windows = profile_api.windows_process_api()
    assert data['target'] == 'windows-amd64'
    assert (data['system'], data['machine'].lower()) == ('Windows', 'amd64')
    assert data['gradleTarget'] == 'MingwX64' and data['classifier'] == 'windows-x64'
    assert data['osRss'] == windows.OS_RSS, 'Windows kernel RSS collector/unit mismatch'
    combined = data.get('profile') == 'combined'
    family = 'combined-driver' if combined else 'http'
    cp = json.loads((snapshot / f'gdal4k-runtime/build/{family}-memory-classpath.json').read_text(encoding='utf-8'))
    assert ntpath.isabs(cp['java']) and ntpath.basename(cp['java']).lower() == 'java.exe'
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    assert data['settings']['jvm'] == settings
    library = snapshot / 'gdal4k-engine/build/jvm-resources/gdal4k/windows-amd64/gdal4k.dll'
    logs, processes = {}, set()
    for record in data['records']:
        relative = record['executionLog']
        command_file = owned_file(snapshot, relative)
        path = Path(relative)
        assert path.parts[:3] == ('verification', 'results', family + '-memory')
        assert path.parent == Path(record['log']).parent == Path(record['rssLog']).parent
        assert type(record['trial']) is int and relative not in logs
        stem = str(record['trial']) if combined else f"{record['trial']}-{record['mode']}"
        assert path.name == stem + '.command.json'
        assert sha(command_file) == record['executionLogSha256'], 'Windows command receipt changed'
        execution = json.loads(command_file.read_text(encoding='utf-8'))
        assert execution == record['execution'], 'Windows embedded command receipt differs'
        expected = [cp['java'], *settings, '-cp', cp['classpath']]
        if combined:
            expected += ['dev.gdal4k.CombinedDriverMemoryProbe', str(library.resolve())]
        else:
            assert record['mode'] in ['download-local', 'range']
            arguments = execution['command'][len(expected):]
            assert len(arguments) == 6 and re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/fixture\.tif', arguments[2])
            assert 0 < int(arguments[2].split(':')[2].split('/')[0]) < 65536
            expected += ['dev.gdal4k.HttpMemoryProbe', record['mode'], arguments[2],
                         str(snapshot / data['fixture']['path']), str(data['settings']['side']), str(library.resolve())]
        stdout = owned_file(snapshot, record['log'])
        raw_peak = owned_file(snapshot, record['rssLog'])
        assert sha(stdout) == record['logSha256'] and sha(raw_peak) == record['rssLogSha256']
        normalized = windows.validate_execution_record(execution, command=expected, cwd=snapshot,
            log_sha256=record['logSha256'], peak_record=json.loads(raw_peak.read_text(encoding='utf-8')))
        assert execution['environmentOverrides'] == {'LC_ALL': 'C'}
        assert execution['timeoutSeconds'] == (600 if combined else 300)
        assert normalized['peakWorkingSetSizeBytes'] == record['peakRssBytes'], 'Windows reported peak differs from kernel receipt'
        identity = normalized['windowsProcess']
        process = (identity['pid'], identity['creationTime100ns'])
        assert process not in processes, 'Diagnostic reused a JVM process identity'
        processes.add(process)
        logs[relative] = record['executionLogSha256']
    return logs


def windows_vcpkg_git_bindings(snapshot):
    """Match the configuration receipt to the actual owned, pinned Git clone."""
    path = owned_file(snapshot, 'verification/results/windows-vcpkg-git.json')
    recorded = json.loads(path.read_text(encoding='utf-8'))
    actual = profile_api.windows_vcpkg_git_identity(snapshot)
    assert recorded == actual, 'Windows private vcpkg Git configuration differs from its receipt'
    return dict(receiptPath=path.relative_to(snapshot).as_posix(), receiptSha256=sha(path), identity=actual)


def windows_tls_tool_identity():
    """Read the current CI generator identity, without running certificate generation."""
    import importlib.metadata
    modules = {}
    for name in ['cryptography.hazmat.bindings._rust', '_cffi_backend', 'pycparser']:
        path = Path(importlib.util.find_spec(name).origin)
        modules[name] = dict(filename=path.name, bytes=path.stat().st_size, sha256=sha(path))
    return dict(versions={name: importlib.metadata.version(name) for name in ['cryptography', 'cffi', 'pycparser']},
                python=dict(version=sys.version, executable=Path(sys.executable).name, sha256=sha(Path(sys.executable))),
                actualInstalledModules=modules)


def windows_tls_fixture_bindings(snapshot, evidence):
    """Require the actual pinned generator and protected-key public receipts."""
    source_names = ['verification/driver-profile/http-fixture.py', 'verification/windows_process.py',
                    'verification/fixtures/windows-tls-generator-lock.json',
                    'verification/driver-profile/windows-tls-requirements.txt']
    source_hashes = {name: sha(owned_file(snapshot, name)) for name in source_names}
    assert all(source_hashes[name] == sha(ROOT / name) for name in source_names), 'Windows TLS source/lock changed'
    lock_name = 'verification/fixtures/windows-tls-generator-lock.json'
    lock = json.loads((snapshot / lock_name).read_text(encoding='utf-8'))
    assert lock['schemaVersion'] == 1 and set(lock['packages']) == {'cryptography', 'cffi', 'pycparser'}
    tool_path = owned_file(evidence, 'certificate-tool.json')
    tool = json.loads(tool_path.read_text(encoding='utf-8'))
    assert tool['schemaVersion'] == 1 and tool['tool'] == 'pyca/cryptography'
    assert tool['lockSha256'] == source_hashes[lock_name], 'Windows TLS generator lock differs'
    assert tool['versions'] == {name: value['version'] for name, value in lock['packages'].items()}
    current = windows_tls_tool_identity()
    assert all(tool[name] == current[name] for name in ['versions', 'python', 'actualInstalledModules']), \
        'Windows TLS generator differs from actual current CI tools'
    assert set(tool['actualInstalledModules']) == {'cryptography.hazmat.bindings._rust', '_cffi_backend', 'pycparser'}
    for item in tool['actualInstalledModules'].values():
        assert Path(item['filename']).name == item['filename'] and type(item['bytes']) is int and item['bytes'] > 0
        assert re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
    names = {'ca': ('gdal4k-fixture-ca', True, False), 'trusted': ('localhost', False, True),
             'untrusted': ('localhost', False, False), 'wrong-name': ('wrong.invalid', False, True)}
    assert {p.name for p in evidence.glob('*-generator.json')} == {name + '-generator.json' for name in names}
    assert {p.name for p in evidence.glob('*.crt')} == {name + '.crt' for name in names}
    assert not list(evidence.glob('*.key')), 'Private TLS keys must stay outside public profile evidence'
    windows = profile_api.windows_process_api()
    records = {}
    for name, (hostname, ca, trusted) in names.items():
        path = owned_file(evidence, name + '-generator.json')
        record = json.loads(path.read_text(encoding='utf-8'))
        cert = owned_file(evidence, name + '.crt')
        assert record['name'] == name and record['certificateSha256'] == sha(cert), 'Windows TLS public certificate changed'
        assert record['subject'] == 'CN=' + hostname
        assert record['issuer'] == 'CN=' + ('gdal4k-fixture-ca' if trusted else hostname)
        assert record['san'] == [hostname] + (['127.0.0.1'] if hostname == 'localhost' else [])
        assert record['ca'] is ca and record['trustedIssuerProvided'] is trusted
        windows.validate_private_acl(record['privateKeyAcl'], False)
        records[name] = dict(generatorSha256=sha(path), certificateSha256=sha(cert), privateKeyAcl=record['privateKeyAcl'])
    return dict(sourceSha256=source_hashes, toolReceiptSha256=sha(tool_path), generatorIdentity=current,
                certificates=records,
                scope='Current pinned Windows CI certificate generator versions/installed-module identities and actual public certificate hashes; '
                      'protected private-key DACL receipts only, no private keys uploaded. Original installed generator module bytes are not uploaded; '
                      'TLS trust/hostname/negative behavior requires the separate actual JVM/Native fixtures.')


def windows_probe_bindings(snapshot, reference, program):
    """Recheck retained actual probe bytes and the unchanged shared DLL policy."""
    path = owned_file(snapshot, reference['path'])
    assert sha(path) == reference['sha256'], 'Windows probe dependency receipt changed'
    value = json.loads(path.read_text(encoding='utf-8'))
    assert (value['schemaVersion'], value['target'], value['result']) == (1, 'windows-amd64', 'passed')
    assert value['executablePath'] == program.relative_to(snapshot).as_posix()
    assert value['executableBytes'] == program.stat().st_size and value['executableSha256'] == sha(program)
    assert value['inspectorSourcePath'] == 'verification/windows_pe.py'
    assert value['inspectorSourceSha256'] == sha(owned_file(snapshot, value['inspectorSourcePath'])) == sha(ROOT / value['inspectorSourcePath'])
    inspector = profile_api.windows_pe_api(snapshot)
    inspector._owned(snapshot, program)
    inspector._owned(snapshot, path)
    assert value['systemDLLs'] == sorted(inspector.SYSTEM_DLLS) and value['systemPrefixes'] == list(inspector.SYSTEM_PREFIXES)
    assert value['pe'] == inspector.inspect(program.read_bytes()), 'Windows probe PE import bytes changed'
    inspector.validate_system_dependencies(value['pe']['imports']['all'])
    return dict(receiptPath=reference['path'], receiptSha256=reference['sha256'], executableSha256=value['executableSha256'],
                inspectorSourceSha256=value['inspectorSourceSha256'], pe=value['pe'],
                scope='Actual standalone PE32+ AMD64 bytes; regular and delay imports satisfy the unchanged JNI system-DLL policy')


def arm64_api():
    spec = importlib.util.spec_from_file_location('profile_arm64_runtime', ROOT / 'verification/driver-profile/arm64-profile.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def owned_file(root, name):
    """Use an existing regular evidence file inside its declared source tree."""
    relative = Path(name)
    assert name and not relative.is_absolute() and '..' not in relative.parts and relative.as_posix() == name
    path = root / relative
    assert not any(root.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1))
    assert path.is_file(), 'Missing current evidence file: ' + name
    return path


def arm64_transfer_bindings(snapshot):
    """Recheck transported immutable inputs without relabeling compilation as runtime."""
    restored_path = snapshot / 'verification/results/arm64-transfer-restored.json'
    restored = json.loads(restored_path.read_text(encoding='utf-8'))
    transfer = restored['transfer']
    assert (transfer['schemaVersion'], transfer['target'], transfer['profile']) == (1, 'linux-arm64', 'combined')
    assert transfer['producerHost'] == dict(system='Linux', machine='x86_64')
    assert restored['actualHost'] == dict(system='Linux', machine='aarch64')
    assert re.fullmatch(r'[0-9a-f]{64}', restored['archiveSha256'])
    assert re.fullmatch(r'build/driver-profiles/combined/linux-arm64/[0-9a-f]{64}', transfer['output'])
    project = snapshot
    for part in reversed((*Path(transfer['output']).parts, 'source')):
        assert project.name == part, 'Restored source location differs from transfer'
        project = project.parent
    api = arm64_api()
    assert transfer['git'] == api.git_identity(project), 'Restored transfer differs from the current Git identity'
    assert transfer['sourceBinary'] == transfer['output'] + '/source/gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'
    assert transfer['consumerBinary'].startswith(transfer['output'] + '/source/verification/results/published-consumer-linuxArm64/bin/')
    assert transfer['consumerBinary'].endswith('/test.kexe')
    for name, metadata in transfer['files'].items():
        assert name.startswith(transfer['output'] + '/') or name.startswith('gdal4k-engine/build/')
        path = owned_file(project, name)
        assert (path.stat().st_size, path.stat().st_mode & 0o777, sha(path)) == \
               (metadata['bytes'], metadata['mode'], metadata['sha256']), 'Transported input changed: ' + name
    for name in ['sourceBinary', 'consumerBinary']:
        assert transfer[name] in transfer['files']
        api.elf_arm64(owned_file(project, transfer[name]))
    prepared_path = snapshot.parent / 'prepared.json'
    inputs_path = snapshot.parent / 'inputs.json'
    for path in [prepared_path, inputs_path]:
        assert path.relative_to(project).as_posix() in transfer['files']
    prepared = json.loads(prepared_path.read_text(encoding='utf-8'))
    for name, digest in prepared.items():
        source = owned_file(snapshot, name)
        assert transfer['files'][source.relative_to(project).as_posix()]['sha256'] == digest == sha(source)
    identity = json.loads(inputs_path.read_text(encoding='utf-8'))
    assert (identity['profile'], identity['target'], identity['runtimeFixtures']) == \
           ('combined', 'linux-arm64', 'required-on-execution-host')
    return dict(restoredReceiptSha256=sha(restored_path), archiveSha256=restored['archiveSha256'],
                transfer=transfer, project=project, preparedSha256=sha(prepared_path), inputsSha256=sha(inputs_path))


def runtime_fixture_source_bindings(snapshot):
    """Bind generated source and both independently copied platform actuals."""
    prepared = json.loads((snapshot.parent / 'prepared.json').read_text(encoding='utf-8'))
    generated = {}
    for suffix, source_set in [('', 'commonTest'), ('Jvm', 'jvmTest'), ('Native', 'nativeTest')]:
        name = f'gdal4k-runtime/src/{source_set}/kotlin/dev/gdal4k/DriverProfileRuntimeFixture{suffix}.kt'
        path = owned_file(snapshot, name)
        assert sha(path) == prepared[name], 'Runtime fixture generated source changed'
        template = owned_file(snapshot, 'verification/driver-profile/DriverProfileRuntimeFixture' + suffix + '.kt')
        expected = template.read_text(encoding='utf-8').replace('@RUNTIME_FIXTURES_REQUIRED@', 'true')
        for key in ['@HDF5_PATH@', '@POSTGRESQL_DSN@', '@HTTP_BASE@', '@HTTPS_BASE@', '@UNTRUSTED_BASE@', '@WRONG_NAME_BASE@']:
            expected = expected.replace(key, '')
        assert path.read_bytes() == expected.encode('utf-8'), 'ARM fixture helper must require runtime input without producer endpoints'
        generated[name] = sha(path)
    consumers = {}
    for target, suffix in [('jvm', 'Jvm'), ('linuxArm64', 'Native')]:
        folder = snapshot / ('verification/results/published-consumer-' + target)
        result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        receipts = result['profile_runtime_fixture_sources']
        assert len(receipts) == 2
        expected_sources = {
            'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixture.kt':
                'src/commonTest/kotlin/consumer/DriverProfileRuntimeFixture.kt',
            f'gdal4k-runtime/src/{"jvmTest" if target == "jvm" else "nativeTest"}/kotlin/dev/gdal4k/DriverProfileRuntimeFixture{suffix}.kt':
                f'src/{target}Test/kotlin/consumer/DriverProfileRuntimeFixture{suffix}.kt'}
        assert {item['source']: item['consumer'] for item in receipts} == expected_sources
        for item in receipts:
            source = owned_file(snapshot, item['source'])
            assert item['sourceSha256'] == generated[item['source']] == sha(source)
            assert item['preservedSource'] == 'fixture-sources/' + item['consumer']
            preserved = owned_file(folder, item['preservedSource'])
            copied = source.read_text(encoding='utf-8').replace('package dev.gdal4k\n', 'package consumer\n').encode('utf-8')
            assert preserved.read_bytes() == copied
            assert sha(preserved) == item['consumerSha256'], 'Independent runtime fixture source changed'
        consumers[target] = dict(resultSha256=sha(folder / 'result.json'), sources=receipts)
    return dict(generatedSourceSha256=generated, independentConsumerSources=consumers)


def arm64_native_bindings(snapshot, label, configuration, restored):
    assert label in ['source', 'consumer']
    path = snapshot / f'verification/results/arm64-native-{label}.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    assert value['result'] == 'passed' and value['target'] == 'linux-arm64'
    assert value['actual_execution'] is True and value['compiled_only'] is False
    assert value['host'] == dict(system='Linux', machine='aarch64')
    transfer = restored['transfer']
    transfer_name = transfer[label + 'Binary']
    executable = owned_file(snapshot, value['executable'])
    assert executable == restored['project'] / transfer_name
    api = arm64_api()
    api.elf_arm64(executable)
    assert sha(executable) == value['executableSha256'] == transfer['files'][transfer_name]['sha256']
    command = owned_file(snapshot, value['command'])
    log = owned_file(snapshot, value['log'])
    assert sha(command) == value['commandSha256'] and sha(log) == value['logSha256']
    receipt = json.loads(command.read_text(encoding='utf-8'))
    assert command.parent == log.parent and receipt['log'] == log.name
    assert receipt['command'] == [str(executable)] and receipt['cwd'] == str(snapshot)
    assert receipt['status'] == 'passed' and receipt['outputComplete'] is True
    assert type(receipt['returncode']) is int and receipt['returncode'] == 0
    assert type(receipt['pid']) is int and receipt['pid'] > 0
    assert receipt['timeoutSeconds'] == 1800 and receipt['elapsedSeconds'] > 0
    assert receipt['startedAtUtc'] and receipt['completedAtUtc']
    raw = log.read_text(encoding='utf-8')
    assert value['counts'] == api.native_cases(raw), 'Native case receipts differ from actual stdout'
    namespace = 'dev.gdal4k.' if label == 'source' else 'consumer.'
    assert all(name.startswith(namespace) for name in value['counts']['cases']), 'Native source/consumer test namespace mismatch'
    assert value['runtimeFixture'] == api.fixture_markers(raw, configuration)
    expected_http = re.findall(r'@Test\s+fun\s+(\w+)\(',
                              (snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt').read_text(encoding='utf-8'))
    assert set(value['counts']['httpCases']) == set(expected_http) and len(expected_http) == len(set(expected_http))
    if label == 'consumer':
        compile_path = snapshot / 'verification/results/published-consumer-linuxArm64/result.json'
        assert value['compilationReceipt'] == dict(path=compile_path.relative_to(snapshot).as_posix(), sha256=sha(compile_path))
        compiled = json.loads(compile_path.read_text(encoding='utf-8'))
        assert compiled['result'] == 'passed' and compiled['target'] == 'linuxArm64'
        assert compiled['version'] == json.loads((snapshot.parent / 'inputs.json').read_text(encoding='utf-8'))['version']
        assert compiled['compiled_only'] is True and compiled['producer_paths_hidden'] is True
        assert compiled['producer_paths_hidden_count'] == 3
        assert set(compiled['producer_paths_hidden_names']) == {
            'gdal4k-engine/build/engine/linux-arm64',
            'gdal4k-engine/build/classes/kotlin/linuxArm64/main/cinterop',
            'gdal4k-runtime/build/classes/kotlin/linuxArm64/main/cinterop'}
        assert compile_path.relative_to(restored['project']).as_posix() in transfer['files']
    else:
        assert value['compilationReceipt'] is None
    return dict(receiptSha256=sha(path), executableSha256=value['executableSha256'], executable=value['executable'],
                commandSha256=value['commandSha256'], command=value['command'], log=value['log'], logSha256=value['logSha256'],
                counts=value['counts'], runtimeFixture=value['runtimeFixture'], compilationReceipt=value['compilationReceipt'],
                scope='Actual AArch64 Native execution; transported compilation receipts remain compiled_only:true')


def arm64_runtime_fixture_bindings(snapshot, diagnostics, policy_keys, native):
    configuration_path = snapshot / 'verification/results/arm64-runtime-configuration.json'
    configuration = json.loads(configuration_path.read_text(encoding='utf-8'))
    api = arm64_api()
    # This validates the whole public configuration atomically before interpreting endpoints.
    contract = api.load('report_runtime_fixture_contract', 'verification/driver-profile/runtime-fixture.py')
    assert contract.validate(configuration) == configuration
    assert configuration['hdf5Path'] == str((snapshot / 'verification/build/driver-profile.h5').resolve())
    http = json.loads((snapshot / 'verification/results/native-http-profile/fixture.json').read_text(encoding='utf-8'))
    assert configuration['httpUrls'] == http['urls']
    markers, raw_hashes = {}, {}
    for name, folder in [('sourceJvm', snapshot / 'gdal4k-runtime/build/test-results/jvmTest'),
                         ('consumerJvm', snapshot / 'verification/results/published-consumer-jvm')]:
        xml = sorted(folder.rglob('TEST-*.xml'))
        assert xml
        text = '\n'.join(output.text or '' for path in xml for output in ET.parse(path).getroot().iter('system-out'))
        markers[name] = api.fixture_markers(text, configuration)
        raw_hashes[name] = {path.relative_to(snapshot).as_posix(): sha(path) for path in xml}
    for label in ['source', 'consumer']:
        item = native[label]
        path = owned_file(snapshot, item['log'])
        assert sha(path) == item['logSha256']
        markers[label + 'Native'] = api.fixture_markers(path.read_text(encoding='utf-8'), configuration)
        raw_hashes[label + 'Native'] = {item['log']: item['logSha256']}
    assert len({value['contextKey'] for value in markers.values()}) == 4
    assert {value['contextKey'] for value in markers.values()} == set(policy_keys), 'Actual fixture markers differ from the four HTTP contexts'
    for record in diagnostics['records']:
        path = owned_file(snapshot, record['log'])
        marker = api.fixture_markers(path.read_text(encoding='utf-8'), configuration)
        assert marker['contextKey'] == record['contextKey']
        name = 'combinedJvm' + str(record['trial'])
        assert name not in markers
        markers[name] = marker
        raw_hashes[name] = {record['log']: record['logSha256']}
        assert sha(path) == record['logSha256']
    assert len(markers) == len({value['contextKey'] for value in markers.values()}) == 7
    sources = runtime_fixture_source_bindings(snapshot)
    return dict(configuration=configuration, configurationSha256=sha(configuration_path),
                canonicalConfigurationSha256=hashlib.sha256(contract.canonical_json(configuration).encode('utf-8')).hexdigest(),
                processMarkers=markers, actualOutputSha256=raw_hashes, sourceBindings=sources,
                scope='Seven actual process markers; four source/consumer test contexts and three fresh combined JVMs')


def postgis_arm64_fixture_bindings(snapshot, target):
    assert target == 'linux-arm64'
    spec = importlib.util.spec_from_file_location('arm64_postgis_report', ROOT / 'verification/driver-profile/arm64-postgis-report.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.bindings(snapshot, target)


def postgis_windows_fixture_bindings(snapshot, target):
    assert target == 'windows-amd64'
    spec = importlib.util.spec_from_file_location('windows_postgis_report', ROOT / 'verification/driver-profile/postgis-windows-report.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.bindings(snapshot, target)


def arm64_ipc_bindings(snapshot, ipc):
    """Require the actual ARM execution and the unchanged cross-build receipt."""
    build_path = snapshot / 'verification/results/arrow-ipc-build-linux-arm64.json'
    built = json.loads(build_path.read_text(encoding='utf-8'))
    assert built['result'] == 'compiled' and built['compiled_only'] is True and built['actual_execution'] is False
    assert built['target'] == 'linux-arm64' and built['compilerHost'] == dict(system='Linux', architecture='x86_64')
    assert ipc['compiled_only'] is False and ipc['actual_execution'] is True
    assert ipc['host'] == dict(system='Linux', architecture='aarch64')
    assert ipc['buildReceiptSha256'] == sha(build_path)
    for key in ['sourceSha256', 'archiveSha256', 'engineManifestSha256', 'engineSourceFingerprint', 'executableSha256']:
        assert ipc[key] == built[key], 'IPC runtime/build identity mismatch: ' + key
    program = owned_file(snapshot, built['executablePath'])
    arm64_api().elf_arm64(program)
    assert sha(program) == built['executableSha256']
    manifest_path = snapshot / 'gdal4k-engine/build/engine/linux-arm64/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert sha(manifest_path) == built['engineManifestSha256']
    assert manifest['source_fingerprint'] == built['engineSourceFingerprint']
    execution = ipc['execution']
    assert execution['argv'] == [str(program)] and execution['cwd'] == str(snapshot)
    assert execution['processStarted'] is True and execution['timedOut'] is False and execution['error'] is None
    assert type(execution['returncode']) is int and execution['returncode'] == 0
    assert execution['timeoutSeconds'] == 120 and execution['startedAtUtc'] and execution['completedAtUtc']
    for stream in ['stdout', 'stderr']:
        path = owned_file(snapshot, execution[stream + 'Log'])
        assert sha(path) == execution[stream + 'Sha256']
        assert path.read_text(encoding='utf-8') == ipc[stream]
    assert ipc['stdout'].splitlines().count('GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK') == 1
    return dict(buildReceiptSha256=sha(build_path), executableSha256=sha(program), execution=execution,
                scope='Cross-compiled IPC executable actually ran on Linux ARM64; compiler receipt remains compile-only')


def diagnostic_bindings(snapshot, data, measured, publication):
    assert data['engineFingerprint'] == measured['fingerprint']
    assert data['engineLibrarySha256'] == publication['jniSha256']
    manifest = snapshot / f"gdal4k-engine/build/engine/{data['target']}/manifest.json"
    assert data['engineManifestSha256'] == sha(manifest), 'Diagnostic manifest changed'
    assert data['compiledProductionSha256'] == publication['compiledProductionSha256'], 'Diagnostic/package class mismatch'
    assert data['compiledProbeSha256'] and data['sourceSha256']
    if data['target'] == 'windows-amd64':
        assert {'verification/windows_process.py', 'verification/command_evidence.py'} <= set(data['sourceSha256']), \
            'Windows kernel collector source bindings are incomplete'
    for name, digest in {**data['compiledProbeSha256'], **data['sourceSha256']}.items():
        assert sha(snapshot / name) == digest, 'Diagnostic source/probe changed: ' + name
    for record in data['records']:
        for path, digest in [('log', 'logSha256'), ('rssLog', 'rssLogSha256')]:
            assert sha(snapshot / record[path]) == record[digest], 'Diagnostic raw log changed'
    bindings = dict(compiledProbeSha256=data['compiledProbeSha256'], sourceSha256=data['sourceSha256'],
                    productionClassCount=len(data['compiledProductionSha256']))
    if data.get('profile') == 'combined':
        bindings['executionLogSha256'] = combined_execution_bindings(snapshot, data)
    elif data['target'] == 'windows-amd64':
        bindings['executionLogSha256'] = windows_execution_bindings(snapshot, data)
    return bindings


def mach_o_architectures(path):
    """Inspect provider executable headers without running a provider binary."""
    size = path.stat().st_size
    with path.open('rb') as file:
        def thin_cpu(offset, length):
            assert length >= 32
            file.seek(offset)
            thin = file.read(16)
            endian = {b'\xcf\xfa\xed\xfe': '<', b'\xfe\xed\xfa\xcf': '>'}.get(thin[:4])
            assert endian is not None, 'PostGIS executable slice is not Mach-O64'
            cpu, _subtype, kind = struct.unpack_from(endian + 'III', thin, 4)
            assert kind == 2, 'PostGIS provider must contain executables'
            return cpu
        header = file.read(8)
        if header[:4] in [b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf']:
            cpus = [thin_cpu(0, size)]
        else:
            formats = {b'\xca\xfe\xba\xbe': ('>', False), b'\xbe\xba\xfe\xca': ('<', False),
                       b'\xca\xfe\xba\xbf': ('>', True), b'\xbf\xba\xfe\xca': ('<', True)}
            assert header[:4] in formats, 'PostGIS provider executable is not Mach-O'
            endian, wide = formats[header[:4]]
            count = struct.unpack_from(endian + 'I', header, 4)[0]
            assert 0 < count <= 16, 'Invalid Mach-O architecture count'
            entry_size = 32 if wide else 20
            entries = file.read(count * entry_size)
            assert len(entries) == count * entry_size
            cpus = []
            for index in range(count):
                cpu, _subtype, offset, length = struct.unpack_from(
                    endian + ('IIQQ' if wide else 'IIII'), entries, index * entry_size)
                assert length > 0 and offset >= 8 + len(entries) and offset + length <= size
                assert thin_cpu(offset, length) == cpu, 'Mach-O slice disagrees with its architecture table'
                cpus.append(cpu)
    names = {0x0100000C: 'arm64', 0x01000007: 'x86_64'}
    assert len(cpus) == len(set(cpus)) and all(cpu in names for cpu in cpus)
    return sorted(names[cpu] for cpu in cpus)


def postgis_fixture_bindings(snapshot, target, manager_source=None, provider_lock=None):
    """Require terminal evidence from the owned Mac server, never a machine service."""
    assert target == 'macos-arm64'
    intended = json.loads((snapshot.parent / 'inputs.json').read_text(encoding='utf-8'))
    assert intended['target'] == target and has_capability(intended['profile'], 'postgresql')
    assert 'postgresqlFixture' in intended, 'Missing intended owned PostGIS identity'
    manager_name = 'verification/driver-profile/postgis-fixture.py'
    lock_name = 'verification/driver-profile/postgis-fixture-lock.json'
    manager_source = manager_source or ROOT / manager_name
    provider_lock = provider_lock or ROOT / lock_name
    lock = json.loads(provider_lock.read_text(encoding='utf-8'))
    assert lock['schemaVersion'] == 1
    expected = lock['providers'][target]
    folder = snapshot / 'verification/results/native-postgis-profile'
    path = folder / 'fixture.json'
    fixture = json.loads(path.read_text(encoding='utf-8'))
    assert fixture['schemaVersion'] == 1 and fixture['target'] == target and fixture['result'] == 'passed'
    sources = fixture['sourceSha256']
    assert set(sources) == {manager_name, lock_name}
    for name, current in [(manager_name, manager_source), (lock_name, provider_lock)]:
        assert sha(current) == sources[name] == sha(snapshot / name), 'PostGIS fixture source changed: ' + name
    assert fixture['archive'] == {key: expected[key] for key in ['url', 'sha256', 'bytes']}
    assert expected['url'].startswith('https://') and '@' not in expected['url']
    assert re.fullmatch(r'[0-9a-f]{64}', expected['sha256']) and expected['bytes'] > 0
    connection = fixture['connection']
    assert connection['passwordIncluded'] is False
    dsn = connection['dsn']
    assert isinstance(dsn, str) and dsn.startswith('PG:')
    pairs = [part.split('=', 1) for part in shlex.split(dsn[3:])]
    assert all(len(pair) == 2 for pair in pairs)
    values = dict(pairs)
    assert len(pairs) == len(values) and set(values) == {'host', 'port', 'dbname', 'user'}
    assert values['host'] == '127.0.0.1' and re.fullmatch(r'[0-9]+', values['port'])
    assert 0 < int(values['port']) < 65536
    assert all(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', values[key]) for key in ['dbname', 'user'])
    assert intended['postgresqlFixture'] == dict(target=target, dsn=dsn, archiveSha256=fixture['archive']['sha256']), \
        'Terminal PostGIS receipt differs from intended fixture identity'
    provider = fixture['provider']
    assert provider['pgMajor'] == expected['pgMajor'] == 17
    binaries = provider['binaries']
    assert set(binaries) == {'postgres', 'initdb', 'pg_ctl', 'psql'}
    actual_binaries = {}
    parents = set()
    for name, metadata in binaries.items():
        binary = Path(metadata['path'])
        suffix = Path(expected['appRelativePath']) / expected['binaryRelativePath'] / name
        assert binary.is_absolute() and binary.parts[-len(suffix.parts):] == suffix.parts
        assert re.fullmatch(r'[0-9a-f]{64}', metadata['sha256'])
        binary_sha = sha(binary)
        assert binary_sha == metadata['sha256'], 'PostGIS provider binary changed: ' + name
        architectures = mach_o_architectures(binary)
        assert 'arm64' in architectures and sorted(metadata['architectures']) == architectures
        assert not binary.resolve().is_relative_to((snapshot / 'verification/results').resolve())
        actual_binaries[name] = dict(path=str(binary), sha256=binary_sha, architectures=architectures)
        parents.add(binary.parent)
    assert len(parents) == 1, 'Mixed PostGIS provider binary roots'
    server = fixture['server']
    assert server['listenAddresses'] == ['127.0.0.1'] and server['port'] == int(values['port'])
    data_directory = Path(server['dataDirectory'])
    assert data_directory.is_absolute() and not data_directory.resolve().is_relative_to((snapshot / 'verification/results').resolve())
    assert type(server['postmasterPid']) is int and server['postmasterPid'] > 0
    assert server['serverVersionNum'] == expected['serverVersionNum']
    assert server['serverVersionNum'] // 10000 == provider['pgMajor']
    assert isinstance(server['serverVersion'], str) and server['serverVersion']
    assert server['postgisVersion'] == expected['postgisVersion']
    assert all(fixture['startup'][key] is True for key in
               ['completed', 'healthy', 'dataDirectoryVerified', 'postgisExtensionVerified'])
    cleanup = fixture['cleanup']
    assert all(cleanup[key] is True for key in ['serverStopped', 'portClosed', 'ownedPostmasterExited'])
    assert cleanup['pgCtlStopReturncode'] == 0
    commands = fixture['commands']
    assert commands, 'Missing PostGIS lifecycle commands'
    logs = {}
    for command in commands:
        assert isinstance(command['label'], str) and command['label']
        assert isinstance(command['argv'], list) and command['argv'] and all(isinstance(v, str) for v in command['argv'])
        assert Path(command['cwd']).is_absolute() and isinstance(command['returncode'], int)
        assert command['startedAt'] and command['completedAt']
        for stream in ['stdout', 'stderr']:
            relative = Path(command[stream])
            assert not relative.is_absolute() and relative.parts and relative.parts[0] == 'commands' and '..' not in relative.parts
            file = folder / relative
            assert not folder.is_symlink() and not any((folder.joinpath(*relative.parts[:index])).is_symlink()
                                                       for index in range(1, len(relative.parts) + 1))
            assert file.resolve().is_relative_to(folder.resolve())
            digest = command[stream + 'Sha256']
            assert re.fullmatch(r'[0-9a-f]{64}', digest) and sha(file) == digest, 'PostGIS command log changed'
            text = file.read_text(encoding='utf-8')
            assert not re.search(r'(?i)(?:password|PGPASSWORD)\s*=', text), 'Unsanitized PostGIS command output'
            logs[relative.as_posix()] = digest
        assert not any(re.search(r'(?i)(?:password|PGPASSWORD)\s*=', value) for value in command['argv'])
    for executable, operation in [('initdb', None), ('pg_ctl', 'start'), ('psql', None), ('pg_ctl', 'stop')]:
        assert any(Path(command['argv'][0]).name == executable and
                   (operation is None or operation in command['argv']) and command['returncode'] == 0
                   for command in commands), 'Missing successful owned PostGIS command: ' + str((executable, operation))
    return dict(fixtureSha256=sha(path), sourceSha256=sources, archive=fixture['archive'],
                connection=connection, provider=dict(pgMajor=provider['pgMajor'], binaries=actual_binaries),
                server=server, startup=fixture['startup'], cleanup=cleanup,
                commandLogSha256=logs, commandCount=len(commands),
                scope='Owned loopback Mac PostGIS lifecycle; provider executable bytes/architectures and sanitized command logs verified. '
                      'Private credentials, PGDATA and the provider bundle are outside uploaded evidence.')


def combined_request_bindings(records, tls, policy_keys, diagnostics):
    """Only the owned same-process fixture may add keys beyond HTTP policy cases."""
    http = [r for r in records if r['case'] == 'combined' and r['name'] == 'fixture.tif']
    https = [r for r in tls if r['case'] == 'combined' and r['name'] == 'fixture.tif']
    keys = {r['key'] for r in http}
    assert keys == {r['key'] for r in https}
    expected_counts = {key: 1 for key in policy_keys}
    settings = diagnostics['settings']
    for record in diagnostics['records']:
        assert record['contextKey'] not in expected_counts
        expected_counts[record['contextKey']] = settings['warmupRounds'] + settings['rounds']
    assigned = set()
    for prefix, count in expected_counts.items():
        matches = {key for key in keys if key.startswith(prefix + '-combined-')}
        assert len(matches) == count, (prefix, count, matches)
        assert not assigned & matches
        assigned.update(matches)
    assert assigned == keys, 'Unrecognized combined HTTP/TLS fixture context'
    assert all(r['status'] in [206, 404] and r['requestedBytes'] <= 65536 for r in http + https)
    for key in keys:
        for requests in [http, https]:
            assert any(r['key'] == key and r['status'] == 206 and r['bodyBytes'] > 0 for r in requests)
    return dict(contextKeys=sorted(keys), expectedInvocations=expected_counts,
                httpRequests=len(http), trustedTlsRequests=len(https))

def main():
    snapshot, profile, target = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    output = snapshot.parent
    identity = json.loads((output / 'inputs.json').read_text(encoding='utf-8'))
    assert identity['profile'] == profile and identity['target'] == target
    arm_runtime = target == 'linux-arm64'
    restored = None
    native_runtime = {}
    if arm_runtime:
        assert profile == 'combined' and identity['runtimeFixtures'] == 'required-on-execution-host'
        restored = arm64_transfer_bindings(snapshot)
        configuration = json.loads((snapshot / 'verification/results/arm64-runtime-configuration.json').read_text(encoding='utf-8'))
        for label in ['source', 'consumer']:
            native_runtime[label] = arm64_native_bindings(snapshot, label, configuration, restored)
    for name, digest in json.loads((output / 'prepared.json').read_text(encoding='utf-8')).items():
        assert sha(snapshot / name) == digest, 'Prepared source changed during profile verification: ' + name
    version = identity['version']
    windows_vcpkg = windows_vcpkg_git_bindings(snapshot) if target == 'windows-amd64' else None
    binary_name = profile_api.jni_library(target)
    measurements = {}
    for name, root in [('default', ROOT), ('profile', snapshot)]:
        build = root / 'gdal4k-engine/build'
        manifest = json.loads((build / f'engine/{target}/manifest.json').read_text(encoding='utf-8'))
        for path, expected in manifest['artifacts'].items():
            assert sha(build / path) == expected, path
        sizes = {}
        for kind, path in [('jvm', build / f'jvm-resources/gdal4k/{target}/{binary_name}'),
                           ('native', build / f'engine/{target}/libgdal4k.a')]:
            sizes[kind] = measure(path)
        measurements[name] = {'fingerprint': manifest['source_fingerprint'], 'sizes': sizes,
                              'artifactHashes': manifest['artifacts']}
    assert measurements['default']['fingerprint'] != measurements['profile']['fingerprint']
    postgis_fixture = None
    if target == 'macos-arm64' and has_capability(profile, 'postgresql'):
        postgis_fixture = postgis_fixture_bindings(snapshot, target)
    elif target == 'windows-amd64' and has_capability(profile, 'postgresql'):
        postgis_fixture = postgis_windows_fixture_bindings(snapshot, target)
    elif arm_runtime and has_capability(profile, 'postgresql'):
        postgis_fixture = postgis_arm64_fixture_bindings(snapshot, target)
    repo = snapshot / 'verification/build/consumer-maven'
    publication = publication_bindings(snapshot, repo, version, target, measurements['profile']['artifactHashes'])
    proof = {}
    http_test_contexts = {}
    fixture_contexts = {}
    native_target = profile_api.TARGETS[target][2]
    native_target = native_target[0].lower() + native_target[1:]
    for name, path in [('sourceJvm', snapshot / 'gdal4k-runtime/build/test-results/jvmTest'),
                       ('sourceNative', snapshot / ('gdal4k-runtime/build/test-results/' + native_target + 'Test')),
                       ('consumerJvm', snapshot / 'verification/results/published-consumer-jvm'),
                       ('consumerNative', snapshot / ('verification/results/published-consumer-' + native_target))]:
        if arm_runtime and name.endswith('Native'):
            actual = native_runtime['source' if name == 'sourceNative' else 'consumer']['counts']
            proof[name] = {key: actual[key] for key in ['tests', 'failures', 'errors', 'skipped']}
            fixture_contexts[name] = actual['fixture']
            http_test_contexts[name] = actual['httpCases']
            continue
        xml = list(path.rglob('TEST-*.xml'))
        assert xml, name
        counts = {k: sum(int(ET.parse(p).getroot().get(k, 0)) for p in xml) for k in ['tests', 'failures', 'errors', 'skipped']}
        assert counts['tests'] > 0 and all(counts[k] == 0 for k in ['failures', 'errors', 'skipped']), counts
        fixtures = [c.get('name') for p in xml for c in ET.parse(p).getroot().iter('testcase')
                    if c.get('classname', '').rsplit('.', 1)[-1] == 'DriverProfileApiTest']
        assert len(fixtures) == 1 and fixtures[0].split('[', 1)[0] == 'requiredDriversAndRealDataRoundTrip'
        fixture_contexts[name] = fixtures[0]
        proof[name] = counts
        if has_capability(profile, 'http'):
            cases = [c.get('name') for p in xml for c in ET.parse(p).getroot().iter('testcase') if 'NativeHttpProfileTest' in c.get('classname', '')]
            assert len(cases) == 10 and len(set(cases)) == 10, (name, cases)
            http_test_contexts[name] = cases
    for name in ['jvm'] + ([] if arm_runtime else [native_target]):
        result = json.loads((snapshot / f'verification/results/published-consumer-{name}/result.json').read_text(encoding='utf-8'))
        assert result['result'] == 'passed' and not result['compiled_only'] and result['version'] == version
        if name != 'jvm':
            assert result['producer_paths_hidden']
    audit = json.loads((snapshot / f'verification/results/engine-audit-{target}.json').read_text(encoding='utf-8'))
    assert audit['result'] == 'passed'
    scientific_fixture = None
    if has_capability(profile, 'scientific'):
        import h5py
        path = snapshot / 'verification/build/driver-profile.h5'
        with h5py.File(path, 'r') as fixture:
            assert list(fixture.keys()) == ['values']
            values = fixture['values']
            assert values.shape == (2, 2) and values.dtype.kind == 'i' and values.dtype.itemsize == 2
            assert values[:].tolist() == [[1, 2], [3, 4]]
        scientific_fixture = dict(path=path.relative_to(snapshot).as_posix(), bytes=path.stat().st_size,
                                  sha256=sha(path), shape=[2, 2], type='Int16', values=[[1, 2], [3, 4]])
    ipc = None
    if has_capability(profile, 'parquet'):
        ipc = json.loads((snapshot / 'verification/results/arrow-ipc-regression.json').read_text(encoding='utf-8'))
        assert ipc['result'] == 'passed' and ipc['target'] == target
        assert ipc['archiveSha256'] == sha(snapshot / f'gdal4k-engine/build/engine/{target}/libgdal4k.a')
        assert ipc['sourceSha256'] == sha(snapshot / 'verification/driver-profile/arrow-ipc-check.cpp')
        assert ipc['stdout'].strip() == 'GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK'
        if arm_runtime:
            ipc['stagedExecutionBindings'] = arm64_ipc_bindings(snapshot, ipc)
        elif target == 'windows-amd64':
            assert ipc['windowsDependencyAudit']['path'] == 'verification/results/arrow-ipc-probe-pe.json'
            ipc['windowsDependencyBindings'] = windows_probe_bindings(snapshot, ipc['windowsDependencyAudit'],
                snapshot / 'verification/build/arrow-ipc-regression/arrow-ipc-check.exe')
    combined = None
    combined_diagnostics = None
    if profile == 'combined':
        diagnostics = snapshot / f'verification/results/combined-driver-memory-{target}.json'
        combined_diagnostics = json.loads(diagnostics.read_text(encoding='utf-8'))
        assert combined_diagnostics['result'] == 'passed' and combined_diagnostics['profile'] == profile
        assert combined_diagnostics['target'] == target and combined_diagnostics['version'] == version
        assert combined_diagnostics['settings']['trials'] == 3 and combined_diagnostics['settings']['rounds'] == 3
        assert combined_diagnostics['settings']['warmupRounds'] == 1
        fixture = combined_diagnostics['fixture']
        assert sha(snapshot / fixture['path']) == fixture['sha256'], 'Combined HDF5 fixture changed'
        assert len(combined_diagnostics['records']) == 3
        assert {r['trial'] for r in combined_diagnostics['records']} == {0, 1, 2}
        assert len({r['contextKey'] for r in combined_diagnostics['records']}) == 3
        bindings = diagnostic_bindings(snapshot, combined_diagnostics, measurements['profile'], publication)
        for record in combined_diagnostics['records']:
            assert record['rounds'] == record['completedRounds'] == 3
            assert all(record[key] > 0 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes'])
            stdout = (snapshot / record['log']).read_text(encoding='utf-8')
            raw = re.findall(r'^GDAL4K_COMBINED_DRIVER_MEMORY (.+)$', stdout, re.M)
            assert len(raw) == 1 and all(record[key] == value for key, value in json.loads(raw[0]).items())
        for key, value in combined_diagnostics['median'].items():
            assert value == statistics.median(r[key] for r in combined_diagnostics['records'])
        generated_test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
        test_classes = snapshot / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k'
        fixture_classes = sorted({p for pattern in ['DriverProfileApiTest*.class', 'DriverProfileFixtures*.class',
                                                   'DriverProfileRuntimeFixture*.class']
                                  for p in test_classes.glob(pattern)})
        assert any(p.name == 'DriverProfileFixtures.class' for p in fixture_classes), 'Missing actual same-process fixture implementation'
        for required in ['DriverProfileRuntimeFixture.class', 'DriverProfileRuntimeFixtureKt.class',
                         'DriverProfileRuntimeFixtureJvmKt.class']:
            assert any(p.name == required for p in fixture_classes), 'Missing actual test runtime configuration class: ' + required
        assert all(bindings['compiledProbeSha256'].get(path.relative_to(snapshot).as_posix()) == sha(path)
                   for path in fixture_classes), 'Combined diagnostic receipt omitted or changed a fixture helper class'
        combined = dict(diagnosticsSha256=sha(diagnostics), currentBindings=bindings,
                        sameProcessFixtureContexts=fixture_contexts, generatedFixtureSourceSha256=sha(generated_test),
                        actualFixtureClassSha256={p.relative_to(snapshot).as_posix(): sha(p) for p in fixture_classes},
                        diagnosticRecords=combined_diagnostics['records'], median=combined_diagnostics['median'],
                        scope=combined_diagnostics['scope'])
    http = None
    if has_capability(profile, 'http'):
        evidence = snapshot / 'verification/results/native-http-profile'
        fixture = json.loads((evidence / 'fixture.json').read_text(encoding='utf-8'))
        assert fixture['serverSourceSha256'] == sha(ROOT / 'verification/driver-profile/http-fixture.py')
        tls_generator = windows_tls_fixture_bindings(snapshot, evidence) if target == 'windows-amd64' else None
        assert fixture['testSourceSha256'] == sha(snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt')
        records = json.loads((evidence / 'http-requests.json').read_text(encoding='utf-8'))
        cache = json.loads((snapshot / 'verification/results/native-http-cache-check.json').read_text(encoding='utf-8'))
        assert cache['result'] == 'passed' and cache['vsiDispatchVerified'] and cache['evictionReloadAndRegistryReleaseVerified']
        assert cache['sourceSha256'] == sha(snapshot / 'gdal4k/native/http-cache-probe.cpp')
        assert cache['probeSha256'] == sha(snapshot / 'gdal4k-engine/build/engine' / target / 'cmake' / profile_api.cache_probe(target))
        if target == 'windows-amd64':
            assert cache['windowsDependencyAudit']['path'] == 'verification/results/native-http-cache-probe-pe.json'
            cache['windowsDependencyBindings'] = windows_probe_bindings(snapshot, cache['windowsDependencyAudit'],
                snapshot / 'gdal4k-engine/build/engine/windows-amd64/cmake/gdal4k_http_probe.exe')
        assert 0 < cache['peakCachedBytes'] <= cache['cacheLimitBytes'] == 8388608
        assert cache['chunkLimitBytes'] == 65536
        assert cache['checksum'] == fixture['imageChecksum'] and cache['fileBytes'] == fixture['imageBytes']
        main = [r for r in records if r['name'] == 'fixture.tif']
        keys = {r['key'] for r in main if r['case'] == 'required'}
        assert len(keys) == 4, 'Actual source JVM/Native and independent JVM/Native fixture contexts required'
        for key in keys:
            def case(name, suffix=''):
                return [r for r in main if r['key'] == key + suffix and r['case'] == name]
            for name in ['denied', 'full', 'encoded', 'length', 'truncated', 'redirect', 'backoff', 'slow']:
                assert len(case(name)) == 1, (key, name)
            assert len(case('timeout')) == 2 and len(case('slow-body')) >= 2
            assert sum(r['status'] == 503 for r in case('retry')) == 1
            assert len(case('weak', '-strong')) == 1
            for name in ['ok', 'weak', 'no-validator', 'sidecar', 'identity', 'size-change']:
                assert len(case(name)) >= 2, (key, name)
                assert max(r['requestedBytes'] for r in case(name)) <= 65536
            assert all(r['ifRange'] == 'Mon, 19 Oct 2026 00:00:00 GMT' for r in case('weak')[1:])
            assert any(r['case'] == 'sidecar' and r['key'] == key and r['name'] == 'fixture.tif.aux.xml' and r['bodyBytes'] > 0 for r in records)
        tls = json.loads((evidence / 'https-requests.json').read_text(encoding='utf-8'))
        assert {r['key'] for r in tls if r['name'] == 'fixture.tif' and r['case'] != 'combined'} == keys
        if combined is not None:
            combined['httpAndTlsInvocations'] = combined_request_bindings(records, tls, keys, combined_diagnostics)
            if arm_runtime:
                combined['runtimeFixtureBindings'] = arm64_runtime_fixture_bindings(snapshot, combined_diagnostics, keys, native_runtime)
        else:
            assert not any(r['case'] == 'combined' for r in records + tls)
        for name in ['untrusted', 'wrong-name']:
            assert json.loads((evidence / f'{name}-requests.json').read_text(encoding='utf-8')) == [], 'TLS must fail before any HTTP request'
        diagnostics = snapshot / f'verification/results/http-memory-profile-controlled-{target}.json'
        measured_http = json.loads(diagnostics.read_text(encoding='utf-8'))
        assert measured_http['target'] == target and len(measured_http['records']) == 6
        assert measured_http['engineFingerprint'] == measurements['profile']['fingerprint']
        assert measured_http['engineLibrarySha256'] == measurements['profile']['artifactHashes'][f'jvm-resources/gdal4k/{target}/{binary_name}']
        http_bindings = diagnostic_bindings(snapshot, measured_http, measurements['profile'], publication)
        for record in measured_http['records']:
            for path, digest in [('log', 'logSha256'), ('rssLog', 'rssLogSha256'), ('requestsLog', 'requestsLogSha256')]:
                assert sha(snapshot / record[path]) == record[digest]
            if record['mode'] == 'range':
                requests = json.loads((snapshot / record['requestsLog']).read_text(encoding='utf-8'))
                assert all(r['status'] in [206, 404] for r in requests)
                assert 0 < record['transferredBodyBytes'] < measured_http['fixture']['bytes'] // 4
        assert len({r['checksum'] for r in measured_http['records']}) == 1
        http = dict(fixture=fixture, publicCases=http_test_contexts, cacheRegression=cache,
                    windowsTlsGenerator=tls_generator,
                    evidenceHashes={p.name: sha(p) for p in evidence.iterdir() if p.is_file()},
                    diagnosticsSha256=sha(diagnostics), diagnosticsMedian=measured_http['median'],
                    currentBindings=http_bindings, scope=measured_http['scope'], contexts=sorted(keys))
    versions = {p.parent.name for p in repo.rglob('*.pom')}
    assert versions == {version}, versions
    archive = output / f'gdal4k-{profile}-{target}-maven.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(repo, arcname='maven')
    report = {'profile': profile, 'target': target, 'version': version, 'sourceInputs': identity,
              'measured': measurements, 'tests': proof, 'dependencyAudit': audit, 'arrowIpcRegression': ipc,
              'archive': archive.name, 'archiveSha256': sha(archive), 'httpRegression': http,
              'combinedRegression': combined, 'publicationBindings': publication, 'scientificFixture': scientific_fixture,
              'postgisFixture': postgis_fixture,
              'windowsVcpkgGit': windows_vcpkg,
              'arm64RuntimeBindings': (dict(transfer={key: value for key, value in restored.items() if key != 'project'},
                                            native=native_runtime) if arm_runtime else None),
              'publication': 'isolated local Maven archive only; no remote upload or default coordinate replacement'}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({key: report[key] for key in ['profile', 'target', 'version', 'measured', 'tests', 'publication']}), flush=True)


if __name__ == '__main__':
    main()
