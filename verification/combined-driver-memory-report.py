#!/usr/bin/env python3
"""Sequential fresh-JVM combined-driver functional diagnostics, no speed thresholds."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    target = 'linux-amd64'
    assert (platform.system(), platform.machine().lower()) == ('Linux', 'x86_64')
    identity = json.loads((ROOT.parent / 'inputs.json').read_text())
    assert identity['profile'] == 'combined' and identity['target'] == target
    manifest = json.loads(args.manifest.read_text())
    assert manifest['target'] == target and manifest['gdal'] == '3.12.4'
    library_sha = sha(args.library)
    assert library_sha == manifest['artifacts'][f'jvm-resources/gdal4k/{target}/libgdal4k.so']
    report_path = ROOT / f'verification/results/combined-driver-memory-{target}.json'
    assert not report_path.exists(), 'Preserve old evidence before a new diagnostic run'
    assert shutil.disk_usage(ROOT).free > 512 * 1024 ** 2, 'Insufficient metadata/log headroom'
    subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeCombinedDriverMemoryClasspath',
                    '-I', str(ROOT / 'verification/combined-driver-memory.init.gradle'), '--no-configuration-cache'],
                   cwd=ROOT, check=True)
    cp = json.loads((ROOT / 'gdal4k-runtime/build/combined-driver-memory-classpath.json').read_text())
    test_root = ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k'
    classes = sorted({p for pattern in ['CombinedDriverMemoryProbe*.class', 'DriverProfileApiTest*.class',
                                      'DriverProfileFixtures*.class',
                                      'DatasetApiTestKt*.class', 'DatasetOutputPathKt*.class']
                      for p in test_root.glob(pattern)})
    production = sorted(p for module in ['gdal4k', 'gdal4k-runtime']
                        for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class'))
    assert classes and production
    assert any(p.name == 'CombinedDriverMemoryProbe.class' for p in classes)
    assert any(p.name == 'DriverProfileApiTestKt.class' for p in classes)
    assert any(p.name == 'DriverProfileFixtures.class' for p in classes)
    sources = json.loads((ROOT.parent / 'prepared.json').read_text())
    for name, digest in sources.items():
        assert sha(ROOT / name) == digest, name
    fixture = ROOT / 'verification/build/driver-profile.h5'
    assert fixture.is_file()
    frozen = {p: sha(p) for p in [*classes, *production, args.library, args.manifest, fixture]}
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/combined-driver-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    records = []
    for trial in range(3):
        log, rss = output / f'{trial}.log', output / f'{trial}.time.txt'
        # Keep partial output and terminate only this newly owned process group on timeout.
        # Killing /usr/bin/time alone would leave its JVM running beside later diagnostics.
        with log.open('x') as stream:
            with subprocess.Popen(['/usr/bin/time', '-v', '-o', str(rss), cp['java'], *settings,
                                   '-cp', cp['classpath'], 'dev.gdal4k.CombinedDriverMemoryProbe', str(args.library.resolve())],
                                  cwd=ROOT, env=dict(os.environ, LC_ALL='C'), stdout=stream,
                                  stderr=subprocess.STDOUT, start_new_session=True) as process:
                try:
                    process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    # The time process may exit before its JVM, so its exit alone
                    # must not suppress final termination of the owned group.
                    time.sleep(5)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise RuntimeError('Combined diagnostic timed out; inspect retained ' + str(log))
        raw = log.read_text()
        assert process.returncode == 0, (log, raw[-2000:])
        matches = re.findall(r'^GDAL4K_COMBINED_DRIVER_MEMORY (.+)$', raw, re.M)
        assert len(matches) == 1, log
        record = json.loads(matches[0])
        assert record['rounds'] == record['completedRounds'] == 3
        assert re.fullmatch(r'gdal4k-[0-9a-f]+', record['contextKey'])
        assert all(record[key] > 0 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes'])
        record.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), 'Linux'),
                      log=str(log.relative_to(ROOT)), logSha256=sha(log),
                      rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss))
        records.append(record)
        (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
        print(json.dumps(record), flush=True)
    assert len({r['contextKey'] for r in records}) == 3
    assert all(sha(p) == digest for p, digest in frozen.items()), 'Class, native engine or HDF5 fixture changed during diagnostics'
    assert all(sha(ROOT / name) == digest for name, digest in sources.items()), 'Prepared source changed during diagnostics'
    report = dict(result='passed', utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  target=target, version=identity['version'], profile='combined', records=records,
                  engineFingerprint=manifest['source_fingerprint'], engineLibrarySha256=library_sha,
                  engineManifestSha256=sha(args.manifest),
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production},
                  sourceSha256=sources, fixture=dict(path=str(fixture.relative_to(ROOT)), sha256=sha(fixture)),
                  median={key: statistics.median(r[key] for r in records)
                          for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes']},
                  settings=dict(warmupRounds=1, rounds=3, trials=3, jvm=settings),
                  scope='Functional Linux x64 JVM diagnostics, one selected combined JNI library and one Runtime per fresh process. '
                  'Each of three measured rounds checks default JXL, real HDF5 and exact NC4, Parquet, loopback HTTP/TLS, '
                  'Parquet-to-PostGIS and detached GeoJSON values/geometry, retained dataset lifetime, and repeat default/scientific use. '
                  'The owned fixture inputs are the same as the current source/published tests. All correctness assertions and '
                  'file/table cleanup are timed. One warmup is outside elapsed/allocation and inside whole-process OS RSS. '
                  'Cumulative JVM allocation, sampled heap (2ms may miss peaks), and whole-process OS RSS are distinct; '
                  'native allocations are outside the JVM counters. Fixture servers and PostGIS run separately and their memory is excluded. '
                  'Small deterministic fixtures exercise integration, not '
                  'representative production throughput. No before/after improvement, universal performance, streaming, '
                  'zero-copy, or total-memory-bound claim.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
