"""Actual Windows kernel/ACL/owned-tree acceptance before the GDAL producer.

This launches only fresh owned Python children. Public raw command/peak/log
receipts remain under verification/results/windows-process-selfcheck; private
unit files use a separate temporary folder and contain no real credentials.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile

from command_evidence import run_with_evidence
import windows_process as windows


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.name != 'nt' or platform.machine().lower() != 'amd64':
        raise RuntimeError('This acceptance requires actual Windows x64; no skipped or emulated success')
    root = Path(__file__).resolve().parents[1]
    output = root / 'verification/results/windows-process-selfcheck' / datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schemaVersion=1, status='running', system=platform.system(), machine=platform.machine(),
                  helperSha256=sha(root/'verification/windows_process.py'),
                  commandHelperSha256=sha(root/'verification/command_evidence.py'),
                  selfcheckSha256=sha(Path(__file__)), commandReceipts=[])
    report_path = output / 'result.json'

    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')

    save()
    try:
        with tempfile.TemporaryDirectory(prefix='gdal4k-windows-acl-unit-') as temporary:
            private = Path(temporary); report['privateDirectoryAcl'] = windows.private_directory(private)
            unit = private/'unit.pgpass'; unit.touch(); windows.private_file(unit)
            unit.write_text('not-a-real-credential\n', encoding='utf-8')
            report['privateFileAcl'] = windows.verify_private_path(unit)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); listener.listen()
                port = listener.getsockname()[1]; owner = windows.port_owner(port)
                assert owner['pid'] == os.getpid() and owner['localAddress'] == '127.0.0.1'
                assert windows.same_process(owner)
                report['ownedLoopbackListener'] = owner
            assert windows.port_closed(port)
            report['ownedLoopbackPortClosed'] = True
        command = [sys.executable, '-c',
                   'x=bytearray(16*1024*1024); x[:]=b"x"*len(x); print("GDAL4K_WINDOWS_FINAL_KERNEL_PEAK_OK",len(x))']
        execution = windows.run_diagnostic(command, output/'success.log', output/'success.peak.json',
                                            output/'success.command.json', cwd=root, timeout=30)
        assert execution['peakMemory']['peakWorkingSetSizeBytes'] > 16*1024*1024
        report['finalKernelPeak'] = execution['peakMemory']
        report['commandReceipts'].append('success.command.json'); save()
        failed = [sys.executable, '-c', 'print("GDAL4K_EXPECTED_NONZERO",flush=True); raise SystemExit(17)']
        try:
            run_with_evidence(failed, cwd=root, directory=output, timeout=30,
                              record_path=output/'failure.command.json', log_path=output/'failure.log')
        except subprocess.CalledProcessError as error:
            assert error.returncode == 17
        else:
            raise AssertionError('Nonzero owned child must fail')
        record = json.loads((output/'failure.command.json').read_text(encoding='utf-8'))
        assert record['status'] == 'failed' and record['returncode'] == 17 and record['outputComplete']
        assert record['cleanup'] == dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False)
        assert record['logSha256'] == sha(output/'failure.log')
        report['commandReceipts'].append('failure.command.json'); save()
        source = ('import sys,json,subprocess,time; '
                  'sys.path.insert(0,' + repr(str(root/'verification')) + '); '
                  'from windows_process import process_identity; '
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
                  'print("GDAL4K_OWNED_DESCENDANT "+json.dumps(process_identity(p.pid)),flush=True); '
                  'time.sleep(120)')
        try:
            run_with_evidence([sys.executable, '-c', source], cwd=root, directory=output, timeout=5,
                              record_path=output/'timeout.command.json', log_path=output/'timeout.log')
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('Owned descendant timeout must fail')
        record = json.loads((output/'timeout.command.json').read_text(encoding='utf-8'))
        assert record['status'] == 'timed_out' and record['outputComplete']
        assert record['cleanup'] == dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=True)
        assert record['logSha256'] == sha(output/'timeout.log')
        markers = re.findall(r'^GDAL4K_OWNED_DESCENDANT (.+)$', (output/'timeout.log').read_text(encoding='utf-8'), re.M)
        assert len(markers) == 1
        descendant = json.loads(markers[0]); assert windows.wait_process_exit(descendant, 10)
        report['ownedDescendantIdentity'] = descendant
        report['ownedDescendantExited'] = True
        report['commandReceipts'].append('timeout.command.json')
        report['status'] = 'passed'
        report['scope'] = 'Actual Windows-only Python/kernel/ACL helper acceptance; no GDAL/JVM/PostGIS runtime or feature approval'
    except BaseException as error:
        report.update(status='failed', failure=dict(type=type(error).__name__, message=str(error)))
        raise
    finally:
        report['actualPublicFiles'] = {p.name: dict(bytes=p.stat().st_size, sha256=sha(p))
                                       for p in output.iterdir() if p.is_file() and p != report_path}
        save()
    print(json.dumps(dict(status='passed', result=str(report_path), sha256=sha(report_path))), flush=True)


if __name__ == '__main__':
    main()
