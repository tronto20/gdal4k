"""Archive only isolated local publications and measured evidence, never build dependencies."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics
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
    engine = {'linux-amd64': 'linux-x64-gnu', 'macos-arm64': 'macos-arm64'}[target]
    expected = {m + suffix for m in ['gdal4k', 'gdal4k-runtime'] for suffix in ['', '-jvm', '-' + native]}
    assert coordinates == expected | {'gdal4k-engine', 'gdal4k-engine-native-' + engine}, coordinates
    base = repo / 'dev/tronto/gdal4k'
    production = {}
    for module in ['gdal4k', 'gdal4k-runtime']:
        current = {str(p.relative_to(snapshot)): sha(p)
                   for p in (snapshot / module / 'build/classes/kotlin/jvm/main').rglob('*.class')}
        assert current, 'Missing compiled production classes: ' + module
        jar = base / (module + '-jvm') / version / f'{module}-jvm-{version}.jar'
        with zipfile.ZipFile(jar) as archive:
            packaged = {f'{module}/build/classes/kotlin/jvm/main/{name}': hashlib.sha256(archive.read(name)).hexdigest()
                        for name in archive.namelist() if name.endswith('.class')}
        assert packaged == current, 'Published classes differ from the current producer: ' + module
        production.update(packaged)
    classifier = profile_api.TARGETS[target][3]
    library = 'libgdal4k.dylib' if target.startswith('macos') else 'libgdal4k.so'
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


def diagnostic_bindings(snapshot, data, measured, publication):
    assert data['engineFingerprint'] == measured['fingerprint']
    assert data['engineLibrarySha256'] == publication['jniSha256']
    manifest = snapshot / f"gdal4k-engine/build/engine/{data['target']}/manifest.json"
    assert data['engineManifestSha256'] == sha(manifest), 'Diagnostic manifest changed'
    assert data['compiledProductionSha256'] == publication['compiledProductionSha256'], 'Diagnostic/package class mismatch'
    assert data['compiledProbeSha256'] and data['sourceSha256']
    for name, digest in {**data['compiledProbeSha256'], **data['sourceSha256']}.items():
        assert sha(snapshot / name) == digest, 'Diagnostic source/probe changed: ' + name
    for record in data['records']:
        for path, digest in [('log', 'logSha256'), ('rssLog', 'rssLogSha256')]:
            assert sha(snapshot / record[path]) == record[digest], 'Diagnostic raw log changed'
    return dict(compiledProbeSha256=data['compiledProbeSha256'], sourceSha256=data['sourceSha256'],
                productionClassCount=len(data['compiledProductionSha256']))


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
    identity = json.loads((output / 'inputs.json').read_text())
    assert identity['profile'] == profile and identity['target'] == target
    for name, digest in json.loads((output / 'prepared.json').read_text()).items():
        assert sha(snapshot / name) == digest, 'Prepared source changed during profile verification: ' + name
    version = identity['version']
    binary_name = 'libgdal4k.dylib' if target.startswith('macos') else 'libgdal4k.so'
    measurements = {}
    for name, root in [('default', ROOT), ('profile', snapshot)]:
        build = root / 'gdal4k-engine/build'
        manifest = json.loads((build / f'engine/{target}/manifest.json').read_text())
        for path, expected in manifest['artifacts'].items():
            assert sha(build / path) == expected, path
        sizes = {}
        for kind, path in [('jvm', build / f'jvm-resources/gdal4k/{target}/{binary_name}'),
                           ('native', build / f'engine/{target}/libgdal4k.a')]:
            sizes[kind] = measure(path)
        measurements[name] = {'fingerprint': manifest['source_fingerprint'], 'sizes': sizes,
                              'artifactHashes': manifest['artifacts']}
    assert measurements['default']['fingerprint'] != measurements['profile']['fingerprint']
    repo = snapshot / 'verification/build/consumer-maven'
    publication = publication_bindings(snapshot, repo, version, target, measurements['profile']['artifactHashes'])
    proof = {}
    http_test_contexts = {}
    fixture_contexts = {}
    for name, path in [('sourceJvm', snapshot / 'gdal4k-runtime/build/test-results/jvmTest'),
                       ('sourceNative', snapshot / ('gdal4k-runtime/build/test-results/' + ('macosArm64Test' if target.startswith('macos') else 'linuxX64Test'))),
                       ('consumerJvm', snapshot / 'verification/results/published-consumer-jvm'),
                       ('consumerNative', snapshot / ('verification/results/published-consumer-' + ('macosArm64' if target.startswith('macos') else 'linuxX64')))]:
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
    for name in ['jvm', 'macosArm64' if target.startswith('macos') else 'linuxX64']:
        result = json.loads((snapshot / f'verification/results/published-consumer-{name}/result.json').read_text())
        assert result['result'] == 'passed' and not result['compiled_only'] and result['version'] == version
        if name != 'jvm':
            assert result['producer_paths_hidden']
    audit = json.loads((snapshot / f'verification/results/engine-audit-{target}.json').read_text())
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
        scientific_fixture = dict(path=str(path.relative_to(snapshot)), bytes=path.stat().st_size,
                                  sha256=sha(path), shape=[2, 2], type='Int16', values=[[1, 2], [3, 4]])
    ipc = None
    if has_capability(profile, 'parquet'):
        ipc = json.loads((snapshot / 'verification/results/arrow-ipc-regression.json').read_text())
        assert ipc['result'] == 'passed' and ipc['target'] == target
        assert ipc['archiveSha256'] == sha(snapshot / f'gdal4k-engine/build/engine/{target}/libgdal4k.a')
        assert ipc['sourceSha256'] == sha(snapshot / 'verification/driver-profile/arrow-ipc-check.cpp')
        assert ipc['stdout'].strip() == 'GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK'
    combined = None
    combined_diagnostics = None
    if profile == 'combined':
        diagnostics = snapshot / f'verification/results/combined-driver-memory-{target}.json'
        combined_diagnostics = json.loads(diagnostics.read_text())
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
            stdout = (snapshot / record['log']).read_text()
            raw = re.findall(r'^GDAL4K_COMBINED_DRIVER_MEMORY (.+)$', stdout, re.M)
            assert len(raw) == 1 and all(record[key] == value for key, value in json.loads(raw[0]).items())
        for key, value in combined_diagnostics['median'].items():
            assert value == statistics.median(r[key] for r in combined_diagnostics['records'])
        generated_test = snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/DriverProfileApiTest.kt'
        test_classes = snapshot / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k'
        fixture_classes = sorted({p for pattern in ['DriverProfileApiTest*.class', 'DriverProfileFixtures*.class']
                                  for p in test_classes.glob(pattern)})
        assert any(p.name == 'DriverProfileFixtures.class' for p in fixture_classes), 'Missing actual same-process fixture implementation'
        combined = dict(diagnosticsSha256=sha(diagnostics), currentBindings=bindings,
                        sameProcessFixtureContexts=fixture_contexts, generatedFixtureSourceSha256=sha(generated_test),
                        actualFixtureClassSha256={str(p.relative_to(snapshot)): sha(p) for p in fixture_classes},
                        diagnosticRecords=combined_diagnostics['records'], median=combined_diagnostics['median'],
                        scope=combined_diagnostics['scope'])
    http = None
    if has_capability(profile, 'http'):
        evidence = snapshot / 'verification/results/native-http-profile'
        fixture = json.loads((evidence / 'fixture.json').read_text())
        assert fixture['serverSourceSha256'] == sha(ROOT / 'verification/driver-profile/http-fixture.py')
        assert fixture['testSourceSha256'] == sha(snapshot / 'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/NativeHttpProfileTest.kt')
        records = json.loads((evidence / 'http-requests.json').read_text())
        cache = json.loads((snapshot / 'verification/results/native-http-cache-check.json').read_text())
        assert cache['result'] == 'passed' and cache['vsiDispatchVerified'] and cache['evictionReloadAndRegistryReleaseVerified']
        assert cache['sourceSha256'] == sha(snapshot / 'gdal4k/native/http-cache-probe.cpp')
        assert cache['probeSha256'] == sha(snapshot / f'gdal4k-engine/build/engine/{target}/cmake/gdal4k_http_probe')
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
        tls = json.loads((evidence / 'https-requests.json').read_text())
        assert {r['key'] for r in tls if r['name'] == 'fixture.tif' and r['case'] != 'combined'} == keys
        if combined is not None:
            combined['httpAndTlsInvocations'] = combined_request_bindings(records, tls, keys, combined_diagnostics)
        else:
            assert not any(r['case'] == 'combined' for r in records + tls)
        for name in ['untrusted', 'wrong-name']:
            assert json.loads((evidence / f'{name}-requests.json').read_text()) == [], 'TLS must fail before any HTTP request'
        diagnostics = snapshot / f'verification/results/http-memory-profile-controlled-{target}.json'
        measured_http = json.loads(diagnostics.read_text())
        assert measured_http['target'] == target and len(measured_http['records']) == 6
        assert measured_http['engineFingerprint'] == measurements['profile']['fingerprint']
        assert measured_http['engineLibrarySha256'] == measurements['profile']['artifactHashes'][f'jvm-resources/gdal4k/{target}/{binary_name}']
        http_bindings = diagnostic_bindings(snapshot, measured_http, measurements['profile'], publication)
        for record in measured_http['records']:
            for path, digest in [('log', 'logSha256'), ('rssLog', 'rssLogSha256'), ('requestsLog', 'requestsLogSha256')]:
                assert sha(snapshot / record[path]) == record[digest]
            if record['mode'] == 'range':
                requests = json.loads((snapshot / record['requestsLog']).read_text())
                assert all(r['status'] in [206, 404] for r in requests)
                assert 0 < record['transferredBodyBytes'] < measured_http['fixture']['bytes'] // 4
        assert len({r['checksum'] for r in measured_http['records']}) == 1
        http = dict(fixture=fixture, publicCases=http_test_contexts, cacheRegression=cache,
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
              'publication': 'isolated local Maven archive only; no remote upload or default coordinate replacement'}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ['profile', 'target', 'version', 'measured', 'tests', 'publication']}), flush=True)


if __name__ == '__main__':
    main()
