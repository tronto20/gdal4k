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
repo = snapshot / 'verification/build/consumer-maven'
versions = {p.parent.name for p in repo.rglob('*.pom')}
assert versions == {version}, versions
archive = output / f'gdal4k-{profile}-{target}-maven.tar.gz'
with tarfile.open(archive, 'w:gz') as tar:
    tar.add(repo, arcname='maven')
report = {'profile': profile, 'target': target, 'version': version, 'sourceInputs': identity,
          'measured': measurements, 'tests': proof, 'dependencyAudit': audit, 'arrowIpcRegression': ipc,
          'archive': archive.name, 'archiveSha256': sha(archive),
          'publication': 'isolated local Maven archive only; no remote upload or default coordinate replacement'}
(output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({key: report[key] for key in ['profile', 'target', 'version', 'measured', 'tests', 'publication']}), flush=True)
