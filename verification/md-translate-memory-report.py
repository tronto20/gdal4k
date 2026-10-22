#!/usr/bin/env python3
"""Fresh current-engine multidimensional conversion diagnostics, without speed thresholds."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODES = ['memory', 'files', 'direct']


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def expected_checksum(side):
    """Closed-form weighted sum of the transposed time=1 plane, in quarter units."""
    rows, columns = side + 7, side + 11
    sx = columns * (columns - 1) // 2
    sx2 = columns * (columns - 1) * (2 * columns - 1) // 6
    sy = rows * (rows - 1) // 2
    sy2 = rows * (rows - 1) * (2 * rows - 1) // 6
    return (rows * rows * 1_000_001 * sx + rows * rows * sx2 + rows * 1_000 * sx * sy
            + columns * 1_000_001 * (sy + rows) + sx * (sy + rows)
            + columns * 1_000 * (sy2 + sy))


def verify_fixture(path, side):
    """Check the actual retained VRT bytes independently of the JVM result receipt."""
    root = ET.parse(path).getroot()
    group = root.find('Group')
    assert root.tag == 'VRTDataset' and group is not None and group.get('name') == '/'
    rows, columns = side + 7, side + 11
    assert [(d.get('name'), int(d.get('size'))) for d in group.findall('Dimension')] == [
        ('time', 3), ('y', rows), ('x', columns)]
    array = group.find('Array')
    assert array is not None and array.get('name') == 'cube' and array.findtext('DataType') == 'Float64'
    assert [d.get('ref') for d in array.findall('DimensionRef')] == ['/time', '/y', '/x']
    values = array.findtext('InlineValues').split()
    assert len(values) == 3 * rows * columns
    for index, text in enumerate(values):
        t, within = divmod(index, rows * columns)
        y, x = divmod(within, columns)
        assert float(text) == (t * 1_000_000 + y * 1_000 + x + 1) / 4
    return dict(path=str(path.relative_to(ROOT)), bytes=path.stat().st_size, sha256=sha(path),
                sourceShape=[3, rows, columns], outputShape=[columns, rows], type='Float64')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--side', type=int, default=128)
    parser.add_argument('--trials', type=int, default=3)
    args = parser.parse_args()
    assert 16 <= args.side <= 256 and args.trials > 0
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', args.label)
    system = platform.system()
    assert system in ['Darwin', 'Linux']
    target = 'macos-arm64' if system == 'Darwin' else 'linux-amd64'
    manifest = json.loads(args.manifest.read_text())
    assert manifest['target'] == target and manifest['gdal'] == '3.12.4'
    library_sha = sha(args.library)
    assert library_sha == manifest['artifacts'][f'jvm-resources/gdal4k/{target}/{args.library.name}']
    report_path = ROOT / f'verification/results/md-translate-memory-{args.label}-{target}.json'
    assert not report_path.exists(), 'Preserve old evidence; use a new label for changed inputs'
    assert shutil.disk_usage(ROOT).free > 256 * 1024 ** 2 + args.side ** 2 * 3 * 64 * args.trials * 3
    subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeMultiDimTranslateMemoryClasspath',
                    '-I', str(ROOT / 'verification/md-translate-memory.init.gradle'), '--no-configuration-cache'],
                   cwd=ROOT, check=True)
    cp = json.loads((ROOT / 'gdal4k-runtime/build/md-translate-memory-classpath.json').read_text())
    classes = list((ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('MultiDimTranslateMemoryProbe*.class'))
    production = [p for module in ['gdal4k', 'gdal4k-runtime']
                  for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class')]
    assert classes and production
    sources = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    # Include the reviewed new implementation and tests before their first staging too.
    sources += ['gdal4k/native/runtime-md-metadata.hpp', 'gdal4k/native/runtime-md-translate.hpp',
                'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/MultiDimTranslateApiTest.kt',
                'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MultiDimTranslateLocalPathsTest.kt',
                'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MultiDimTranslateNativeCallbacksTest.kt',
                'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/MultiDimTranslateMemoryProbe.kt',
                'verification/md-translate-memory-report.py', 'verification/md-translate-memory.init.gradle']
    sources = sorted(set(sources))
    frozen = {p: sha(p) for p in [*classes, *production, *(ROOT / name for name in sources), args.library, args.manifest]}
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/md-translate-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    elements = (args.side + 7) * (args.side + 11)
    for trial in range(args.trials):
        for mode in (MODES if trial % 2 == 0 else list(reversed(MODES))):
            log, rss, fixture = (output / f'{trial}-{mode}{suffix}' for suffix in ['.log', '.time.txt', '.vrt'])
            result = subprocess.run(['/usr/bin/time', '-l' if system == 'Darwin' else '-v', '-o', str(rss),
                                     cp['java'], *settings, '-cp', cp['classpath'], 'dev.gdal4k.MultiDimTranslateMemoryProbe',
                                     mode, str(args.side), str(args.library.resolve()), str(fixture)],
                                    cwd=ROOT, env=dict(os.environ, LC_ALL='C'), text=True, capture_output=True, timeout=600)
            log.write_text(result.stdout + result.stderr)
            assert result.returncode == 0, (log, result.stderr[-2000:])
            matches = re.findall(r'^GDAL4K_MD_TRANSLATE_MEMORY (.+)$', result.stdout, re.M)
            assert len(matches) == 1
            record = json.loads(matches[0])
            assert record['mode'] == mode and record['side'] == args.side and record['times'] == 3 and record['rounds'] == 3
            assert record['sourceRows'] == args.side + 7 and record['sourceColumns'] == args.side + 11
            assert record['outputRows'] == args.side + 11 and record['outputColumns'] == args.side + 7
            assert record['elements'] == elements and record['logicalResultBytes'] == elements * 8
            assert record['checksum'] == str(expected_checksum(args.side) * 3)
            assert all(record[k] > 0 for k in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes'])
            assert record['fileOutputBytes'] == 0 if mode == 'memory' else record['fileOutputBytes'] > 0
            fixture_receipt = verify_fixture(fixture, args.side)
            assert record['fixtureBytes'] == fixture_receipt['bytes'] and record['fixtureSha256'] == fixture_receipt['sha256']
            record.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), system), fixture=fixture_receipt,
                          log=str(log.relative_to(ROOT)), logSha256=sha(log),
                          rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss))
            records.append(record)
            (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
            print(json.dumps(record), flush=True)
    assert all(sha(path) == digest for path, digest in frozen.items()), 'Source, classes or selected engine changed during diagnostics'
    report = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), label=args.label,
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  target=target, platform=platform.platform(), records=records,
                  median={mode: {key: statistics.median(r[key] for r in records if r['mode'] == mode)
                                 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes', 'logicalResultBytes', 'fileOutputBytes']}
                          for mode in MODES},
                  engineFingerprint=manifest['source_fingerprint'], engineManifestSha256=sha(args.manifest), engineLibrarySha256=library_sha,
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production},
                  sourceSha256={name: sha(ROOT / name) for name in sources},
                  settings=dict(side=args.side, sourceShape=[3, args.side + 7, args.side + 11],
                                outputShape=[args.side + 11, args.side + 7], rounds=3, trials=args.trials, jvm=settings,
                                arguments=['-array', 'name=cube,dstname=selected,transpose=[0,2,1],view=[1,:,:]']),
                  scope='Functional diagnostics on a self-contained closed OS VRT with three nonzero Float64 planes. Select time=1 and transpose spatial dimensions; every result value, type and shape is checked, with an independent weighted checksum. Memory returns independent MEM; files/direct use GTiff. Each measured round includes source open/close, GDAL conversion and wrapper postflight validation, output reopen/read/all-value validation/close, and direct output deletion. Fixture setup and one warmup are outside elapsed/allocation but inside whole-process OS RSS. Nine fresh JVMs per host for three alternating trials of three result forms, three rounds each. Logical result bytes are element bytes, not a serialized MEM size. Cumulative JVM allocation, sampled heap (2ms may miss peaks) and OS RSS differ; native allocations are outside JVM allocation. Distinct APIs are not before/after variants. Complete public buffers and GDAL intermediates remain. No speed improvement, streaming, zero-copy, total-memory bound or universal/Native/Web/Android/scientific-driver performance claim.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
