"""Archive only isolated local publications and measured evidence, never build dependencies."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
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
proof = {}
http_test_contexts = {}
for name, path in [('sourceJvm', snapshot / 'gdal4k-runtime/build/test-results/jvmTest'),
                   ('sourceNative', snapshot / ('gdal4k-runtime/build/test-results/' + ('macosArm64Test' if target.startswith('macos') else 'linuxX64Test'))),
                   ('consumerJvm', snapshot / 'verification/results/published-consumer-jvm'),
                   ('consumerNative', snapshot / ('verification/results/published-consumer-' + ('macosArm64' if target.startswith('macos') else 'linuxX64')))]:
    xml = list(path.rglob('TEST-*.xml'))
    assert xml, name
    counts = {k: sum(int(ET.parse(p).getroot().get(k, 0)) for p in xml) for k in ['tests', 'failures', 'errors', 'skipped']}
    assert counts['tests'] > 0 and all(counts[k] == 0 for k in ['failures', 'errors', 'skipped']), counts
    assert sum('DriverProfileApiTest' in c.get('classname', '') for p in xml for c in ET.parse(p).getroot().iter('testcase')) == 1
    proof[name] = counts
    if profile == 'http':
        cases = [c.get('name') for p in xml for c in ET.parse(p).getroot().iter('testcase') if 'NativeHttpProfileTest' in c.get('classname', '')]
        assert len(cases) == 10 and len(set(cases)) == 10, (name, cases)
        http_test_contexts[name] = cases
for name in ['jvm', 'macosArm64' if target.startswith('macos') else 'linuxX64']:
    result = json.loads((snapshot / f'verification/results/published-consumer-{name}/result.json').read_text())
    assert result['result'] == 'passed' and not result['compiled_only']
    if name != 'jvm':
        assert result['producer_paths_hidden']
audit = json.loads((snapshot / f'verification/results/engine-audit-{target}.json').read_text())
assert audit['result'] == 'passed'
ipc = None
if profile == 'parquet':
    ipc = json.loads((snapshot / 'verification/results/arrow-ipc-regression.json').read_text())
    assert ipc['result'] == 'passed' and ipc['target'] == target
    assert ipc['archiveSha256'] == sha(snapshot / f'gdal4k-engine/build/engine/{target}/libgdal4k.a')
    assert ipc['sourceSha256'] == sha(snapshot / 'verification/driver-profile/arrow-ipc-check.cpp')
http = None
if profile == 'http':
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
    assert {r['key'] for r in tls if r['name'] == 'fixture.tif'} == keys
    for name in ['untrusted', 'wrong-name']:
        assert json.loads((evidence / f'{name}-requests.json').read_text()) == [], 'TLS must fail before any HTTP request'
    diagnostics = snapshot / f'verification/results/http-memory-profile-controlled-{target}.json'
    measured_http = json.loads(diagnostics.read_text())
    assert len(measured_http['records']) == 6 and measured_http['engineFingerprint'] == measurements['profile']['fingerprint']
    assert measured_http['engineLibrarySha256'] == measurements['profile']['artifactHashes'][f'jvm-resources/gdal4k/{target}/{binary_name}']
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
                scope=measured_http['scope'], contexts=sorted(keys))
repo = snapshot / 'verification/build/consumer-maven'
versions = {p.parent.name for p in repo.rglob('*.pom')}
assert versions == {version}, versions
archive = output / f'gdal4k-{profile}-{target}-maven.tar.gz'
with tarfile.open(archive, 'w:gz') as tar:
    tar.add(repo, arcname='maven')
report = {'profile': profile, 'target': target, 'version': version, 'sourceInputs': identity,
          'measured': measurements, 'tests': proof, 'dependencyAudit': audit, 'arrowIpcRegression': ipc,
          'archive': archive.name, 'archiveSha256': sha(archive), 'httpRegression': http,
          'publication': 'isolated local Maven archive only; no remote upload or default coordinate replacement'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({key: report[key] for key in ['profile', 'target', 'version', 'measured', 'tests', 'publication']}), flush=True)
