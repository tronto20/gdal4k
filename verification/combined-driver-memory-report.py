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


def host_settings(manifest, system, machine):
    hosts = {('Linux', 'x86_64'): ('linux-amd64', 'LinuxX64', 'linux-x64-gnu', 'libgdal4k.so', '-v', 'GNU time -v', 'KiB', 1024),
             ('Linux', 'aarch64'): ('linux-arm64', 'LinuxArm64', 'linux-arm64-gnu', 'libgdal4k.so', '-v', 'GNU time -v', 'KiB', 1024),
             ('Darwin', 'arm64'): ('macos-arm64', 'MacosArm64', 'macos-arm64', 'libgdal4k.dylib', '-l', 'Darwin time -l', 'bytes', 1),
             ('Windows', 'amd64'): ('windows-amd64', 'MingwX64', 'windows-x64', 'gdal4k.dll', None, 'Win32 GetProcessMemoryInfo', 'bytes', 1)}
    host = (system, machine.lower())
    if host not in hosts:
        raise ValueError('Combined JVM diagnostics require Linux x64, Linux ARM64, macOS ARM64 or Windows x64: ' + repr(host))
    target, gradle_target, classifier, library, flag, time_format, unit, multiplier = hosts[host]
    if manifest['target'] != target:
        raise ValueError('Combined engine target does not match actual host: ' + manifest['target'])
    return dict(target=target, gradleTarget=gradle_target, classifier=classifier,
                library=library, flag=flag, timeFormat=time_format,
                reportedRssUnit=unit, rssMultiplierToBytes=multiplier)


def prepare_classpath():
    # Preserve the orchestrator's verified staged-engine Gradle property on ARM.
    subprocess.run([str(ROOT / ('gradlew.bat' if os.name == 'nt' else 'gradlew')), '--console=plain', ':gdal4k-runtime:writeCombinedDriverMemoryClasspath',
                    '-I', str(ROOT / 'verification/combined-driver-memory.init.gradle'), '--no-configuration-cache'],
                   cwd=ROOT, env=os.environ.copy(), check=True)


def compiled_probe_classes(test_root):
    patterns = ['CombinedDriverMemoryProbe*.class', 'DriverProfileApiTest*.class',
                'DriverProfileFixtures*.class', 'DriverProfileRuntimeFixture*.class',
                'DatasetApiTestKt*.class', 'DatasetOutputPathKt*.class']
    classes = sorted({path for pattern in patterns for path in test_root.glob(pattern)})
    required = {'CombinedDriverMemoryProbe.class', 'DriverProfileApiTestKt.class',
                'DriverProfileFixtures.class', 'DriverProfileRuntimeFixture.class',
                'DriverProfileRuntimeFixtureKt.class', 'DriverProfileRuntimeFixtureJvmKt.class'}
    missing = required - {path.name for path in classes}
    if missing:
        raise ValueError('Missing combined diagnostic classes: ' + ', '.join(sorted(missing)))
    return classes


def run_timed(command, log, evidence_path, *, cwd, timeout=600):
    """Retain the actual owned time-wrapper command and exit, including failures."""
    evidence = dict(command=command, cwd=str(cwd), timeoutSeconds=timeout,
                    startedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    parentPid=os.getpid(), pidRole='/usr/bin/time wrapper; JVM PID is not observed',
                    requestedNewSession=True, environmentOverrides={'LC_ALL': 'C'}, status='starting')

    def save():
        evidence_path.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')

    save()
    started = time.monotonic()
    try:
        with log.open('x', encoding='utf-8') as stream:
            with subprocess.Popen(command, cwd=cwd, env=dict(os.environ, LC_ALL='C'), stdout=stream,
                                  stderr=subprocess.STDOUT, start_new_session=True) as process:
                evidence.update(command=list(process.args), pid=process.pid, status='running')
                try:
                    evidence['observedProcessGroupId'] = os.getpgid(process.pid)
                except ProcessLookupError:
                    evidence['observedProcessGroupId'] = None
                save()
                timed_out = False
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    evidence['status'] = 'timed_out'
                    save()
                    # The time process may exit before its JVM. Its exit must not
                    # suppress final termination of this newly owned process group.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(5)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise RuntimeError('Combined diagnostic timed out; inspect retained ' + str(log))
                finally:
                    evidence.update(returncode=process.returncode,
                                    completedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                    elapsedSeconds=time.monotonic() - started,
                                    status='timed_out' if timed_out else 'passed' if process.returncode == 0 else 'failed')
                    save()
    except OSError as error:
        evidence.update(status='failed', exception=type(error).__name__)
        save()
        raise
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    system, machine = platform.system(), platform.machine()
    manifest = json.loads(args.manifest.read_text())
    host = host_settings(manifest, system, machine)
    target = host['target']
    identity = json.loads((ROOT.parent / 'inputs.json').read_text())
    assert identity['profile'] == 'combined' and identity['target'] == target
    assert manifest['target'] == target and manifest['gdal'] == '3.12.4'
    library_sha = sha(args.library)
    assert library_sha == manifest['artifacts'][f"jvm-resources/gdal4k/{target}/{host['library']}"]
    report_path = ROOT / f'verification/results/combined-driver-memory-{target}.json'
    assert not report_path.exists(), 'Preserve old evidence before a new diagnostic run'
    assert shutil.disk_usage(ROOT).free > 512 * 1024 ** 2, 'Insufficient metadata/log headroom'
    prepare_classpath()
    cp = json.loads((ROOT / 'gdal4k-runtime/build/combined-driver-memory-classpath.json').read_text())
    test_root = ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k'
    classes = compiled_probe_classes(test_root)
    production = sorted(p for module in ['gdal4k', 'gdal4k-runtime']
                        for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class'))
    assert production
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
        java_command = [cp['java'], *settings, '-cp', cp['classpath'],
                        'dev.gdal4k.CombinedDriverMemoryProbe', str(args.library.resolve())]
        evidence_path = output / f'{trial}.command.json'
        if system == 'Windows':
            from windows_process import run_diagnostic
            execution = run_diagnostic(java_command, log, rss, evidence_path, cwd=ROOT)
        else:
            command = ['/usr/bin/time', host['flag'], '-o', str(rss), *java_command]
            execution = run_timed(command, log, evidence_path, cwd=ROOT)
        raw = log.read_text(encoding='utf-8')
        assert execution['returncode'] == 0, (log, raw[-2000:])
        matches = re.findall(r'^GDAL4K_COMBINED_DRIVER_MEMORY (.+)$', raw, re.M)
        assert len(matches) == 1, log
        record = json.loads(matches[0])
        assert record['rounds'] == record['completedRounds'] == 3
        assert re.fullmatch(r'gdal4k-[0-9a-f]+', record['contextKey'])
        assert all(record[key] > 0 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes'])
        if system == 'Windows':
            from windows_process import peak_record_bytes
            peak = peak_record_bytes(rss.read_text(encoding='utf-8'))
        else:
            peak = rss_units.peak_rss_bytes(rss.read_text(), system)
        record.update(trial=trial, peakRssBytes=peak,
                      log=log.relative_to(ROOT).as_posix(), logSha256=sha(log),
                      rssLog=rss.relative_to(ROOT).as_posix(), rssLogSha256=sha(rss),
                      execution=execution, executionLog=evidence_path.relative_to(ROOT).as_posix(),
                      executionLogSha256=sha(evidence_path))
        records.append(record)
        (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
        print(json.dumps(record), flush=True)
    assert len({r['contextKey'] for r in records}) == 3
    assert all(sha(p) == digest for p, digest in frozen.items()), 'Class, native engine or HDF5 fixture changed during diagnostics'
    assert all(sha(ROOT / name) == digest for name, digest in sources.items()), 'Prepared source changed during diagnostics'
    report = dict(result='passed', utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  target=target, gradleTarget=host['gradleTarget'], classifier=host['classifier'],
                  platform=platform.platform(), system=system, machine=machine,
                  osRss=(__import__('windows_process').OS_RSS.copy() if system == 'Windows' else
                         dict(timeExecutable='/usr/bin/time', timeArgument=host['flag'],
                              timeFormat=host['timeFormat'], reportedUnit=host['reportedRssUnit'],
                              multiplierToBytes=host['rssMultiplierToBytes'])),
                  version=identity['version'], profile='combined', records=records,
                  engineFingerprint=manifest['source_fingerprint'], engineLibrarySha256=library_sha,
                  engineManifestSha256=sha(args.manifest),
                  compiledProbeSha256={p.relative_to(ROOT).as_posix(): sha(p) for p in classes},
                  compiledProductionSha256={p.relative_to(ROOT).as_posix(): sha(p) for p in production},
                  sourceSha256=sources, fixture=dict(path=fixture.relative_to(ROOT).as_posix(), sha256=sha(fixture)),
                  median={key: statistics.median(r[key] for r in records)
                          for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes']},
                  settings=dict(warmupRounds=1, rounds=3, trials=3, jvm=settings),
                  scope='Functional ' + target + ' JVM diagnostics, one selected combined JNI library and one Runtime per fresh process. '
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
