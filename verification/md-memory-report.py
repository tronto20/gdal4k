#!/usr/bin/env python3
"""Fresh JVM diagnostics of existing numeric MD slices/typed values/attributes; no timing thresholds."""
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
    parser.add_argument('--side', type=int, default=256)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--skip-compile', action='store_true')
    args = parser.parse_args()
    assert 16 <= args.side <= 1024 and args.trials > 0
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', args.label)
    system = platform.system()
    assert system in ['Darwin', 'Linux']
    target = 'macos-arm64' if system == 'Darwin' else 'linux-amd64'
    manifest = json.loads(args.manifest.read_text())
    assert sha(args.library) == manifest['artifacts'][f'jvm-resources/gdal4k/{target}/{args.library.name}']
    report_path = ROOT / f'verification/results/md-memory-{args.label}-{target}.json'
    assert not report_path.exists(), 'Preserve old evidence; use a new label for changed inputs'
    if not args.skip_compile:
        subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeMDMemoryClasspath',
                        '-I', str(ROOT / 'verification/md-memory.init.gradle'), '--no-configuration-cache'],
                       cwd=ROOT, check=True)
    cp = json.loads((ROOT / 'gdal4k-runtime/build/md-memory-classpath.json').read_text())
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/md-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    for trial in range(args.trials):
        for mode in (['double-slice', 'int64-values', 'uint64-attribute'] if trial % 2 == 0 else ['uint64-attribute', 'int64-values', 'double-slice']):
            log, rss = output / f'{trial}-{mode}.log', output / f'{trial}-{mode}.time.txt'
            result = subprocess.run(['/usr/bin/time', '-l' if system == 'Darwin' else '-v', '-o', str(rss),
                                     cp['java'], *settings, '-cp', cp['classpath'], 'dev.gdal4k.MDMemoryProbe',
                                     mode, str(args.side), str(args.library.resolve())],
                                    cwd=ROOT, env=dict(os.environ, LC_ALL='C'), text=True, capture_output=True, timeout=600)
            log.write_text(result.stdout + result.stderr)
            assert result.returncode == 0, (log, result.stderr[-2000:])
            matches = re.findall(r'^GDAL4K_MD_MEMORY (.+)$', result.stdout, re.M)
            assert len(matches) == 1
            record = json.loads(matches[0])
            assert record['mode'] == mode and record['side'] == args.side and record['rounds'] == 3
            assert all(record[k] > 0 for k in ['payloadBytes', 'elapsedNanos', 'jvmAllocatedBytes',
                                              'sampledPeakHeapUsedBytes', 'bridgeBytes', 'largestJsonBytes'])
            assert record['jsonCalls'] >= 0 and record['binaryCalls'] >= 0
            assert record['jsonCalls'] + record['binaryCalls'] > 0
            record.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), system),
                          log=str(log.relative_to(ROOT)), logSha256=sha(log),
                          rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss))
            records.append(record)
            (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
            print(json.dumps(record), flush=True)
    assert all(len({r['checksum'] for r in records if r['mode'] == mode}) == 1 for mode in ['double-slice', 'int64-values', 'uint64-attribute'])
    classes = list((ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('MDMemoryProbe*.class'))
    production_classes = [p for module in ['gdal4k', 'gdal4k-runtime']
                          for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class')]
    assert classes and production_classes
    sources = ['verification/md-memory-report.py', 'verification/md-memory.init.gradle',
               'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MDMemoryProbe.kt',
               'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/MDArray.kt',
               'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/MDDataType.kt',
               'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/Attribute.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/RuntimeEngine.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/MDCodec.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt',
               'gdal4k/native/runtime.hpp', 'gdal4k/native/runtime-md.hpp',
               'gdal4k/native/runtime.generated.inc', 'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/GdalTransport.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BinaryMD.kt']
    report = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), label=args.label,
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  platform=platform.platform(), target=target, records=records,
                  median={mode: {key: statistics.median(r[key] for r in records if r['mode'] == mode)
                                 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes',
                                             'jsonCalls', 'binaryCalls', 'bridgeBytes', 'largestJsonBytes']}
                          for mode in ['double-slice', 'int64-values', 'uint64-attribute']},
                  engineFingerprint=manifest['source_fingerprint'], engineManifestSha256=sha(args.manifest),
                  engineLibrarySha256=sha(args.library), engineLibrary=str(args.library.resolve()),
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production_classes},
                  sourceSha256={name: sha(ROOT / name) for name in sources},
                  settings=dict(side=args.side, rounds=3, trials=args.trials, jvm=settings),
                  scope='Instrumented existing JVM MDArray Float64 slice, exact Int64 typed slice and UInt64 metadata attribute APIs on a deterministic MEM multidimensional fixture. Every measured result validates all values/bits and checksum; Int64/UInt64 values exceed exact Float64 range. Nonzero rectangular windows are row-major. Setup/warmup excluded from elapsed/allocation, included in whole-process OS RSS. Cumulative JVM allocation differs from sampled heap (2ms sampling may miss peaks) and OS RSS. payloadBytes is logical numeric bytes, not public object/string allocation. Source/compiled classes describe current Kotlin/harness; selected native library/manifest are independently bound. Preserved native engines do not imply old Kotlin. Only matching classes/settings/inputs support controlled comparisons. No HDF5/netCDF, Native/Web/Android, zero-copy or universal performance claim.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
