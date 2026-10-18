#!/usr/bin/env python3
"""Fresh-JVM vector read diagnostics. Time/allocation/RSS are observations, not pass thresholds."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import platform
import re
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--rows', type=int, default=8192)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--skip-compile', action='store_true')
    args = parser.parse_args()
    assert args.rows > 0 and args.trials > 0
    system = platform.system()
    assert system in ['Darwin', 'Linux']
    target = 'macos-arm64' if system == 'Darwin' else 'linux-amd64'
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', args.label)
    manifest = json.loads(args.manifest.read_text())
    key = f'jvm-resources/gdal4k/{target}/{args.library.name}'
    assert sha(args.library) == manifest['artifacts'][key]
    report_path = ROOT / f'verification/results/vector-memory-{args.label}-{target}.json'
    assert not report_path.exists(), 'Preserve previous reports; use a new label for changed inputs'
    if not args.skip_compile:
        subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeVectorMemoryClasspath',
                        '-I', str(ROOT / 'verification/vector-memory.init.gradle'), '--no-configuration-cache'],
                       cwd=ROOT, check=True)
    cp = json.loads((ROOT / 'gdal4k-runtime/build/vector-memory-classpath.json').read_text())
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/vector-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    for trial in range(args.trials):
        for driver in (['GeoJSON', 'MEM', 'GPKG'] if trial % 2 == 0 else ['GPKG', 'MEM', 'GeoJSON']):
            name = f'{trial}-{driver}'
            log, rss = output / (name + '.log'), output / (name + '.time.txt')
            command = ['/usr/bin/time', '-l' if system == 'Darwin' else '-v', '-o', str(rss),
                       cp['java'], *settings, '-cp', cp['classpath'], 'dev.gdal4k.VectorMemoryProbe',
                       driver, str(args.rows), str(args.library.resolve())]
            result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, LC_ALL='C'),
                                    text=True, capture_output=True, timeout=600)
            log.write_text(result.stdout + result.stderr)
            assert result.returncode == 0, (log, result.stderr[-2000:])
            matches = re.findall(r'^GDAL4K_VECTOR_MEMORY (.+)$', result.stdout, re.M)
            assert len(matches) == 1
            record = json.loads(matches[0])
            assert record['rows'] == args.rows and record['rounds'] == 3 and record['driver'] == driver
            record.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), system))
            for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'jsonCalls', 'bridgeBytes', 'peakRssBytes']:
                assert record[key] > 0
            record.update(log=str(log.relative_to(ROOT)), logSha256=sha(log),
                          rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss))
            records.append(record)
            # Preserve partial progress if a later fresh JVM fails.
            (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
            print(json.dumps(record), flush=True)
    assert len({record['checksum'] for record in records}) == 1
    classes = list((ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('VectorMemoryProbe*.class'))
    assert classes
    sources = [ROOT / 'verification/vector-memory-report.py', ROOT / 'verification/vector-memory.init.gradle',
               ROOT / 'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/VectorMemoryProbe.kt',
               ROOT / 'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/LayerBatches.kt',
               ROOT / 'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/RuntimeEngine.kt',
               ROOT / 'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt',
               ROOT / 'gdal4k/native/runtime.generated.inc']
    production_classes = []
    for module in ['gdal4k', 'gdal4k-runtime']:
        production_classes += sorted((ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class'))
    assert production_classes
    report = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), label=args.label,
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  platform=platform.platform(), target=target, records=records,
                  median={driver: {key: statistics.median(r[key] for r in records if r['driver'] == driver)
                                   for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes', 'jsonCalls', 'bridgeBytes']}
                          for driver in ['GeoJSON', 'MEM', 'GPKG']},
                  engineFingerprint=manifest['source_fingerprint'], engineManifestSha256=sha(args.manifest),
                  engineLibrarySha256=sha(args.library), engineLibrary=str(args.library.resolve()),
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production_classes},
                  sourceSha256={str(p.relative_to(ROOT)): sha(p) for p in sources},
                  settings=dict(rows=args.rows, rounds=3, trials=args.trials, jvm=settings,
                                fields=['nullable exact Int64', 'Float64', 'nullable UTF-8 string', 'nullable Point WKB']),
                  scope='Existing per-feature batches with validation/value/geometry access. Fixture loading/copying and warmup excluded from elapsed time and cumulative JVM allocation; whole-process OS RSS includes those phases. JVM allocation, 2ms sampled heap and OS RSS differ; sampler may miss peaks. Int64 checksum deliberately wraps Long. No columnar/native/web/Android or universal improvement claim.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
