#!/usr/bin/env python3
"""Fresh current-engine tile-index JVM diagnostics; no before/after or speed thresholds."""
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--tiles', type=int, default=32)
    p.add_argument('--trials', type=int, default=3)
    a = p.parse_args()
    assert 4 <= a.tiles <= 256 and a.trials > 0
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', a.label)
    system = platform.system()
    assert system in ['Darwin', 'Linux']
    target = 'macos-arm64' if system == 'Darwin' else 'linux-amd64'
    manifest = json.loads(a.manifest.read_text())
    assert manifest['target'] == target and manifest['gdal'] == '3.12.4'
    assert sha(a.library) == manifest['artifacts'][f'jvm-resources/gdal4k/{target}/{a.library.name}']
    report_path = ROOT / f'verification/results/tile-index-memory-{a.label}-{target}.json'
    assert not report_path.exists(), 'Preserve old evidence; use a new label for changed inputs'
    subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeTileIndexMemoryClasspath',
                    '-I', str(ROOT / 'verification/tile-index-memory.init.gradle'), '--no-configuration-cache'],
                   cwd=ROOT, check=True)
    cp = json.loads((ROOT / 'gdal4k-runtime/build/tile-index-memory-classpath.json').read_text())
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/tile-index-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    for trial in range(a.trials):
        for mode in (['memory', 'direct'] if trial % 2 == 0 else ['direct', 'memory']):
            log, rss = output / f'{trial}-{mode}.log', output / f'{trial}-{mode}.time.txt'
            result = subprocess.run(['/usr/bin/time', '-l' if system == 'Darwin' else '-v', '-o', str(rss),
                                     cp['java'], *settings, '-cp', cp['classpath'], 'dev.gdal4k.TileIndexMemoryProbe',
                                     mode, str(a.tiles), str(a.library.resolve())],
                                    cwd=ROOT, env=dict(os.environ, LC_ALL='C'), text=True, capture_output=True, timeout=600)
            log.write_text(result.stdout + result.stderr)
            assert result.returncode == 0, (log, result.stderr[-2000:])
            matches = re.findall(r'^GDAL4K_TILE_INDEX_MEMORY (.+)$', result.stdout, re.M)
            assert len(matches) == 1
            r = json.loads(matches[0])
            assert r['mode'] == mode and r['tiles'] == a.tiles and r['side'] == 64 and r['rounds'] == 3
            assert r['area'] == a.tiles * 64 * 64 * 6.125
            assert r['checksum'] == str(a.tiles * (a.tiles + 1) // 2 * 64 * 64 * 3)
            assert all(r[k] > 0 for k in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'outputBytes'])
            r.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), system),
                     log=str(log.relative_to(ROOT)), logSha256=sha(log),
                     rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss))
            records.append(r)
            (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
            print(json.dumps(r), flush=True)
    classes = list((ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('TileIndexMemoryProbe*.class'))
    production = [p for module in ['gdal4k', 'gdal4k-runtime']
                  for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class')]
    assert classes and production
    # Record all versioned source inputs, not only the public entry point.
    sources = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    sources += ['gdal4k/native/runtime-tile-index.hpp', 'gdal4k/src/commonMain/kotlin/dev/gdal4k/runtime/TileIndexArguments.kt',
                'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/TileIndexApiTest.kt',
                'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/TileIndexMemoryProbe.kt',
                'verification/tile-index-memory-report.py', 'verification/tile-index-memory.init.gradle']
    report = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), label=a.label,
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  target=target, platform=platform.platform(), records=records,
                  median={mode: {key: statistics.median(r[key] for r in records if r['mode'] == mode)
                                 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes', 'outputBytes']}
                          for mode in ['memory', 'direct']},
                  engineFingerprint=manifest['source_fingerprint'], engineManifestSha256=sha(a.manifest),
                  engineLibrarySha256=sha(a.library),
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production},
                  sourceSha256={name: sha(ROOT / name) for name in sorted(set(sources))},
                  settings=dict(tiles=a.tiles, side=64, rounds=3, trials=a.trials, jvm=settings),
                  scope='Functional diagnostics of an explicit 32-file (configurable) raster metadata index: rotated 64-square Byte GTiff files in owned vsimem, EPSG3857, GPKG vector output. Every round independently checks affine determinant areas, feature count/order and exact source locations/checksum. Includes preflight, GDALTileIndex, output reopen/geometry checks/close and direct deletion. Fixture/setup/warmup excluded from elapsed/allocation but included in OS RSS. Cumulative JVM allocation, sampled heap (2ms may miss peaks) and OS RSS differ. Memory/direct are distinct APIs, not a before/after comparison. Complete GdalFiles buffers and GDAL geometries remain. No speed improvement, pixel mosaic/GTI reader, streaming, zero-copy, total-memory bound or universal/Native/Web/Android performance claim.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
