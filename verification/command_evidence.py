"""Bound a CI child command and retain its output before the job-level timeout."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def _windows_host():
    return os.name == 'nt'


def run_with_evidence(args, *, cwd, directory, timeout, env=None,
                      record_path=None, log_path=None, windows_peak=False):
    """The caller owns this process tree; never terminate unrelated processes."""
    if timeout <= 0:
        raise ValueError('Command timeout must be positive')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    number = 1
    while (directory / f'{number:03d}.json').exists():
        number += 1
    record_path = Path(record_path) if record_path else directory / f'{number:03d}.json'
    log_path = Path(log_path) if log_path else directory / f'{number:03d}.log'
    if record_path.parent != directory or log_path.parent != directory or record_path.exists() or log_path.exists():
        raise ValueError('Command evidence paths must be new files in their owned directory')
    if windows_peak and not _windows_host():
        raise ValueError('Windows kernel peak collection requires actual Windows')
    command = [str(x) for x in args]
    record = {'command': command, 'cwd': str(cwd), 'timeoutSeconds': timeout,
              'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'status': 'starting', 'log': log_path.name}

    def save():
        record_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8', newline='\n')

    save()
    started = time.monotonic()
    options = {} if _windows_host() else {'start_new_session': True}
    owned = None
    with log_path.open('xb') as log:
        try:
            if _windows_host():
                from windows_process import OwnedProcess
                owned = OwnedProcess(command, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
                process = owned.process
                record.update(windowsProcess=owned.identity,
                              pidRole='owned JVM process' if windows_peak else 'owned child process',
                              ownership=dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True))
            else:
                process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, env=env, **options)
        except Exception as error:
            record.update(status='failed', exception=type(error).__name__, outputComplete=False)
            save()
            raise
        record.update(pid=process.pid, status='running')
        save()
        pump_errors = []
        output_hash = hashlib.sha256()

        def pump():
            try:
                with process.stdout:
                    while chunk := process.stdout.read1(65536):
                        log.write(chunk)
                        output_hash.update(chunk)
                        log.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
            except Exception as error:
                pump_errors.append(error)

        output = threading.Thread(target=pump, daemon=True)
        output.start()
        timed_out = False
        evidence_error = None
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            record['status'] = 'timed_out'
            save()  # Retain the command identity even if process cleanup fails.
            if _windows_host():
                owned.terminate_tree()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=30)
        finally:
            if owned is not None:
                try:
                    if windows_peak and not timed_out and process.poll() == 0:
                        record['peakMemory'] = owned.peak_after_exit()
                except Exception as error:
                    evidence_error = error
                    record['finalPeakFailure'] = dict(type=type(error).__name__, message=str(error))
                finally:
                    try:
                        record['cleanup'] = owned.close()
                    except Exception as error:
                        evidence_error = error
                        record['cleanupFailure'] = dict(type=type(error).__name__, message=str(error))
            output.join(timeout=10)
            complete = not output.is_alive() and not pump_errors
            record.update(returncode=process.poll(), elapsedSeconds=time.monotonic() - started,
                          completedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          status='timed_out' if timed_out else 'passed' if process.returncode == 0 and complete and evidence_error is None else 'failed',
                          outputComplete=bool(complete), logSha256=output_hash.hexdigest())
            save()
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        if evidence_error is not None:
            raise RuntimeError('Windows kernel peak or owned-tree evidence failed; inspect retained receipt') from evidence_error
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        if output.is_alive() or pump_errors:
            raise RuntimeError('CI command output was not completely retained')
    return record_path
