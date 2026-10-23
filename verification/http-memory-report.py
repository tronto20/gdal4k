#!/usr/bin/env python3
"""Real loopback HTTP transfer + GDAL window diagnostics; no timing thresholds."""
import argparse
import datetime
import hashlib
import http.server
import importlib.util
import json
import os
import platform
import re
import statistics
import struct
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TILE = 256


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def host_settings(manifest, system, machine):
    hosts = {('Linux', 'x86_64'): ('linux-amd64', 'LinuxX64', 'linux-x64-gnu', 'libgdal4k.so', '-v', 'GNU time -v', 'KiB', 1024),
             ('Linux', 'aarch64'): ('linux-arm64', 'LinuxArm64', 'linux-arm64-gnu', 'libgdal4k.so', '-v', 'GNU time -v', 'KiB', 1024),
             ('Darwin', 'arm64'): ('macos-arm64', 'MacosArm64', 'macos-arm64', 'libgdal4k.dylib', '-l', 'Darwin time -l', 'bytes', 1)}
    host = (system, machine.lower())
    if host not in hosts:
        raise ValueError('HTTP JVM diagnostics require Linux x64, Linux ARM64 or macOS ARM64: ' + repr(host))
    target, gradle_target, classifier, library, flag, time_format, unit, multiplier = hosts[host]
    if manifest['target'] != target:
        raise ValueError('HTTP engine target does not match actual host: ' + manifest['target'])
    return dict(target=target, gradleTarget=gradle_target, classifier=classifier,
                library=library, flag=flag, timeFormat=time_format,
                reportedRssUnit=unit, rssMultiplierToBytes=multiplier)


def prepare_classpath():
    # Preserve the orchestrator's verified staged-engine Gradle property on ARM.
    subprocess.run([str(ROOT / 'gradlew'), '--console=plain', ':gdal4k-runtime:writeHttpMemoryClasspath',
                    '-I', str(ROOT / 'verification/http-memory.init.gradle'), '--no-configuration-cache'],
                   cwd=ROOT, env=os.environ.copy(), check=True)


def fixture(path, side):
    """Classic little-endian, uncompressed tiled Byte TIFF; deterministic pixel formula."""
    tiles = (side // TILE) ** 2
    # TIFF header, 12 IFD entries, next-IFD pointer, tile offsets and byte counts.
    data_start = 8 + 2 + 12 * 12 + 4 + tiles * 8
    entries = [(256, 4, 1, side), (257, 4, 1, side), (258, 3, 1, 8),
               (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
               (284, 3, 1, 1), (322, 4, 1, TILE), (323, 4, 1, TILE),
               (324, 4, tiles, data_start - tiles * 8),
               (325, 4, tiles, data_start - tiles * 4), (339, 3, 1, 1)]
    with path.open('xb') as file:
        file.write(b'II' + struct.pack('<HIH', 42, 8, len(entries)))
        for entry in entries:
            file.write(struct.pack('<HHII', *entry))
        file.write(struct.pack('<I', 0))
        file.write(struct.pack('<' + 'I' * tiles, *(data_start + i * TILE * TILE for i in range(tiles))))
        file.write(struct.pack('<' + 'I' * tiles, *([TILE * TILE] * tiles)))
        assert file.tell() == data_start
        # Formula repeats every 256 pixels, but every spatial window checks its coordinates.
        block = bytes((x * 31 + y * 17 + (x ^ y)) & 255 for y in range(TILE) for x in range(TILE))
        for _ in range(tiles):
            file.write(block)
    return data_start + side * side


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, path):
        self.path = path
        self.digest = sha(path)
        self.records = []
        self.lock = threading.Lock()
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        size = self.server.path.stat().st_size
        first, last, status = 0, size - 1, 200
        request_range = self.headers.get('Range')
        if self.path != '/fixture.tif':
            status = 404
        elif request_range:
            match = re.fullmatch(r'bytes=(\d+)-(\d+)', request_range)
            if not match or not 0 <= int(match[1]) <= int(match[2]) < size:
                status = 416
            else:
                first, last, status = int(match[1]), int(match[2]), 206
        length = last - first + 1 if status in (200, 206) else 0
        sent = 0
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', '"' + self.server.digest + '"')
            self.send_header('Last-Modified', 'Mon, 19 Oct 2026 00:00:00 GMT')
            if status == 206:
                self.send_header('Content-Range', f'bytes {first}-{last}/{size}')
            self.end_headers()
            if length:
                with self.server.path.open('rb') as file:
                    file.seek(first)
                    while sent < length:
                        block = file.read(min(65536, length - sent))
                        assert block
                        self.wfile.write(block)
                        sent += len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.lock:
                self.server.records.append(dict(path=self.path, status=status, range=request_range,
                                                ifRange=self.headers.get('If-Range'), bodyBytes=sent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--side', type=int, default=8192)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--modes', nargs='+', choices=['download-local', 'range'], default=['download-local'])
    parser.add_argument('--skip-compile', action='store_true')
    args = parser.parse_args()
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', args.label)
    assert args.side in range(1024, 16385, 256) and args.side % 512 == 0 and args.trials > 0
    assert len(set(args.modes)) == len(args.modes)
    system, machine = platform.system(), platform.machine()
    manifest = json.loads(args.manifest.read_text())
    host = host_settings(manifest, system, machine)
    target = host['target']
    library_sha = sha(args.library)
    assert library_sha == manifest['artifacts'][f"jvm-resources/gdal4k/{target}/{host['library']}"]
    report_path = ROOT / f'verification/results/http-memory-{args.label}-{target}.json'
    assert not report_path.exists(), 'Use a new label; never overwrite evidence'
    # Fixture plus one streamed local download, and headroom for metadata/logs.
    import shutil
    assert shutil.disk_usage(ROOT).free > args.side ** 2 * 2 + 128 * 1024 ** 2, 'Insufficient safe disk headroom'
    if not args.skip_compile:
        prepare_classpath()
    cp = json.loads((ROOT / 'gdal4k-runtime/build/http-memory-classpath.json').read_text())
    frozen_paths = [p for module in ['gdal4k', 'gdal4k-runtime']
                    for p in (ROOT / module / 'build/classes/kotlin/jvm').rglob('*.class')]
    frozen_paths += [ROOT / p for p in subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines() if (ROOT / p).is_file()]
    before_identities = {str(p.relative_to(ROOT)): sha(p) for p in frozen_paths}
    spec = importlib.util.spec_from_file_location('rss_units', ROOT / 'verification/window-memory-report.py')
    rss_units = importlib.util.module_from_spec(spec); spec.loader.exec_module(rss_units)
    output = ROOT / 'verification/results/http-memory' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    image = output / 'fixture.tif'
    fixture_bytes = fixture(image, args.side)
    assert image.stat().st_size == fixture_bytes
    settings = ['-Xms32m', '-Xmx1536m', '-XX:+UseG1GC']
    records = []
    expected_checksum = 1125899906842597
    for _ in range(3):
        for y in range(TILE):
            for x in range(TILE):
                expected_checksum = (expected_checksum * 31 + ((x * 31 + y * 17 + (x ^ y)) & 255)) & ((1 << 64) - 1)
    if expected_checksum >= 1 << 63:
        expected_checksum -= 1 << 64
    for trial in range(args.trials):
        for mode in args.modes if trial % 2 == 0 else list(reversed(args.modes)):
            server = Server(image)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            log, rss = output / f'{trial}-{mode}.log', output / f'{trial}-{mode}.time.txt'
            try:
                result = subprocess.run(['/usr/bin/time', host['flag'], '-o', str(rss),
                                         cp['java'], *settings, '-cp', cp['classpath'], 'dev.gdal4k.HttpMemoryProbe', mode,
                                         f'http://127.0.0.1:{server.server_port}/fixture.tif', str(image), str(args.side), str(args.library.resolve())],
                                        cwd=ROOT, env=dict(os.environ, LC_ALL='C'), text=True, capture_output=True, timeout=300)
            finally:
                server.shutdown(); thread.join(); server.server_close()
            log.write_text(result.stdout + result.stderr)
            network = output / f'{trial}-{mode}.requests.json'
            network.write_text(json.dumps(server.records, indent=2) + '\n')
            assert result.returncode == 0, (log, result.stderr[-2000:])
            matches = re.findall(r'^GDAL4K_HTTP_MEMORY (.+)$', result.stdout, re.M)
            assert len(matches) == 1
            record = json.loads(matches[0])
            assert record['mode'] == mode and record['side'] == args.side and record['validatedPixels'] == 196608
            assert record['checksum'] == str(expected_checksum)
            assert all(record[key] > 0 for key in ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes'])
            if mode == 'download-local':
                assert len(server.records) == 1 and server.records[0]['status'] == 200
                assert record['downloadedBytes'] == server.records[0]['bodyBytes'] == fixture_bytes
            record.update(trial=trial, peakRssBytes=rss_units.peak_rss_bytes(rss.read_text(), system),
                          requests=len(server.records), transferredBodyBytes=sum(r['bodyBytes'] for r in server.records),
                          log=str(log.relative_to(ROOT)), logSha256=sha(log),
                          rssLog=str(rss.relative_to(ROOT)), rssLogSha256=sha(rss),
                          requestsLog=str(network.relative_to(ROOT)), requestsLogSha256=sha(network))
            records.append(record)
            (output / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
            print(json.dumps(record), flush=True)
    assert len({r['checksum'] for r in records}) == 1
    assert all(sha(ROOT / name) == digest for name, digest in before_identities.items()), 'Source/classes changed during measurements'
    assert sha(args.library) == library_sha, 'Selected native library changed during measurements'
    classes = list((ROOT / 'gdal4k-runtime/build/classes/kotlin/jvm/test/dev/gdal4k').glob('HttpMemoryProbe*.class'))
    production = [p for module in ['gdal4k', 'gdal4k-runtime'] for p in (ROOT / module / 'build/classes/kotlin/jvm/main').rglob('*.class')]
    assert classes and production
    sources = ['verification/http-memory-report.py', 'verification/http-memory.init.gradle',
               'gdal4k-runtime/src/jvmTest/kotlin/dev/gdal4k/HttpMemoryProbe.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/RuntimeEngine.kt',
               'gdal4k-runtime/src/jniMain/kotlin/dev/gdal4k/JniTransport.kt',
               'gdal4k-runtime/src/commonMain/kotlin/dev/gdal4k/internal/BundledRuntime.generated.kt']
    keys = ['elapsedNanos', 'jvmAllocatedBytes', 'sampledPeakHeapUsedBytes', 'peakRssBytes', 'requests', 'transferredBodyBytes']
    report = dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), label=args.label,
                  testedHead=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  platform=platform.platform(), system=system, machine=machine, target=target,
                  gradleTarget=host['gradleTarget'], classifier=host['classifier'], records=records,
                  osRss=dict(timeExecutable='/usr/bin/time', timeArgument=host['flag'],
                             timeFormat=host['timeFormat'], reportedUnit=host['reportedRssUnit'],
                             multiplierToBytes=host['rssMultiplierToBytes']),
                  median={mode: {key: statistics.median(r[key] for r in records if r['mode'] == mode) for key in keys} for mode in args.modes},
                  engineFingerprint=manifest['source_fingerprint'], engineLibrarySha256=library_sha,
                  engineLibrary=str(args.library.resolve()), engineManifestSha256=sha(args.manifest),
                  fixture=dict(path=str(image.relative_to(ROOT)), bytes=fixture_bytes, sha256=sha(image),
                               format='Uncompressed tiled Byte GTiff; no COG layout or overview claim'),
                  compiledProbeSha256={str(p.relative_to(ROOT)): sha(p) for p in classes},
                  compiledProductionSha256={str(p.relative_to(ROOT)): sha(p) for p in production},
                  sourceSha256={name: sha(ROOT / name) for name in sources},
                  settings=dict(side=args.side, tile=TILE, windows=3, trials=args.trials, jvm=settings),
                  scope='Loopback HTTP/1.0, no added latency or throttling. Fresh JVM per case, same local GDAL warmup; network cold. Download control streams through 64KiB buffer to owned temporary file, opens locally and reads/validates all 196608 pixels in three tiles, then removes its download. Elapsed/allocation include HTTP, temporary file I/O, GDAL open/read/close, validation/checksum and removal; exclude local warmup. Whole-process OS RSS includes setup/warmup. Cumulative JVM allocation, 2ms sampled heap and OS RSS are distinct; C++/libcurl allocations are outside JVM allocation. Server body bytes exclude HTTP/TLS headers and count successfully written response bytes, not client heap or wire packet bytes. Real WAN/TLS, Native/Android, COG and universal speed claims require separate evidence.')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['median']), flush=True)


if __name__ == '__main__':
    main()
