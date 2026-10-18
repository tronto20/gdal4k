#!/usr/bin/env python3
"""Exercise real vcpkg downloads, ZIP/TAR extraction and binary cache with no CLI archive tools."""
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run ', dir=output))
    values = json.loads(args.environment.read_text(encoding='utf-8'))
    env = os.environ.copy()
    env.update({k: v for k, v in values.items() if k != 'PATH_PREFIX'})
    env.pop('VCPKG_FORCE_DOWNLOADED_BINARIES', None)
    # Catch any accidental dependency on curl/zip/unzip/tar from the user's PATH.
    # POSIX archive adapters precede these traps; Windows uses its OS tar and acquired 7-Zip.
    traps = work / 'forbidden system tools'
    traps.mkdir()
    forbidden = ('curl', 'zip', 'unzip', 'tar', 'bash') if os.name != 'nt' else ('bash', 'curl', 'zip', 'unzip')
    for name in forbidden:
        path = traps / (name + ('.cmd' if os.name == 'nt' else ''))
        path.write_text('@echo forbidden tool & exit /b 91\n' if os.name == 'nt' else '#!/bin/sh\necho "Forbidden system tool: ' + name + '" >&2\nexit 91\n')
        path.chmod(0o755)
    env['PATH'] = values['PATH_PREFIX'] + os.pathsep + str(traps) + os.pathsep + env.get('PATH', '')
    if os.name != 'nt':
        for tool in ('tar', 'zip', 'unzip'):
            assert 'vcpkg-archive-bin' in shutil.which(tool, path=env['PATH']), tool
    downloads = work / 'served'
    downloads.mkdir()
    zip_path, tar_path = downloads / 'source.zip', downloads / 'source.tar.gz'
    with zipfile.ZipFile(zip_path, 'w') as z:
        z.writestr('archive-probe/probe.h', '#define GDAL4K_ARCHIVE_PROBE 1\n')
    with tarfile.open(tar_path, 'w:gz') as t:
        payload = b'archive-probe license\n'
        info = tarfile.TarInfo('archive-probe/LICENSE')
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    handler = partial(SimpleHTTPRequestHandler, directory=str(downloads))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    overlay = work / 'overlay/archive-probe'
    overlay.mkdir(parents=True)
    (overlay / 'vcpkg.json').write_text(json.dumps({'name': 'archive-probe', 'version': '1.0.0'}))
    port = []
    for kind, archive in [('ZIP', zip_path), ('TAR', tar_path)]:
        url = 'http://127.0.0.1:' + str(server.server_port) + '/' + archive.name
        checksum = hashlib.sha512(archive.read_bytes()).hexdigest()
        port.append(f'vcpkg_download_distfile(ARCHIVE_{kind} URLS "{url}" FILENAME "{archive.name}" SHA512 {checksum})\nvcpkg_extract_source_archive(SOURCE_{kind} ARCHIVE "${{ARCHIVE_{kind}}}")')
    port += ['file(INSTALL "${SOURCE_ZIP}/probe.h" DESTINATION "${CURRENT_PACKAGES_DIR}/include")',
             'file(INSTALL "${SOURCE_TAR}/LICENSE" DESTINATION "${CURRENT_PACKAGES_DIR}/share/archive-probe" RENAME copyright)']
    (overlay / 'portfile.cmake').write_text('\n'.join(port) + '\n')
    manifest = work / 'manifest'
    manifest.mkdir()
    (manifest / 'vcpkg.json').write_text(json.dumps({'name': 'archive-smoke', 'version': '1.0.0', 'dependencies': ['archive-probe']}))
    vcpkg = Path(values['VCPKG_ROOT']) / ('vcpkg.exe' if os.name == 'nt' else 'vcpkg')
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
    triplet = arch + '-' + {'Darwin': 'osx', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    command = [str(vcpkg), 'install', '--disable-metrics', '--x-wait-for-lock', '--triplet=' + triplet,
               '--x-manifest-root=' + str(manifest), '--overlay-ports=' + str(overlay.parent),
               '--x-buildtrees-root=' + str(work / 'buildtrees'), '--x-packages-root=' + str(work / 'packages'),
               '--binarysource=clear', '--binarysource=files,' + str(work / 'binary cache') + ',readwrite']
    logs = []
    try:
        for phase in ('built', 'restored'):
            installed = work / ('installed ' + phase)
            result = subprocess.run(command + ['--x-install-root=' + str(installed)], env=env, capture_output=True, text=True)
            (output / (phase + '.log')).write_text(result.stdout + result.stderr)
            if result.returncode:
                sys.exit(result.stdout + result.stderr)
            assert (installed / triplet / 'include/probe.h').read_text() == '#define GDAL4K_ARCHIVE_PROBE 1\n'
            logs.append(result.stdout)
        assert list((work / 'binary cache').rglob('*.zip')), 'Binary cache archive was not written'
        assert 'Restored 1 package' in logs[1], logs[1]
        evidence = {'host': platform.system() + '/' + platform.machine(), 'vcpkg': str(vcpkg),
                    'real_download_zip_tar_install': 'passed', 'binary_cache_write_restore': 'passed',
                    'forbidden_cli_tools': list(forbidden), 'gdal_engine_built': False}
        (output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
