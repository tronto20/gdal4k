"""Bound a CI child command and retain its output before the job-level timeout."""
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def run_with_evidence(args, *, cwd, directory, timeout, env=None):
    """The caller owns this process tree; never terminate unrelated processes."""
    if timeout <= 0:
        raise ValueError('Command timeout must be positive')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    number = 1
    while (directory / f'{number:03d}.json').exists():
        number += 1
    record_path = directory / f'{number:03d}.json'
    log_path = directory / f'{number:03d}.log'
    command = [str(x) for x in args]
    record = {'command': command, 'cwd': str(cwd), 'timeoutSeconds': timeout,
              'startedAtUtc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'status': 'starting', 'log': log_path.name}

    def save():
        record_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')

    save()
    started = time.monotonic()
    options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
               else {'start_new_session': True})
    with log_path.open('xb') as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, env=env, **options)
        except OSError as error:
            record.update(status='failed', exception=type(error).__name__, outputComplete=False)
            save()
            raise
        record.update(pid=process.pid, status='running')
        save()
        pump_errors = []

        def pump():
            try:
                with process.stdout:
                    while chunk := process.stdout.read1(65536):
                        log.write(chunk)
                        log.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
            except Exception as error:
                pump_errors.append(error)

        output = threading.Thread(target=pump, daemon=True)
        output.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            record['status'] = 'timed_out'
            save()  # Retain the command identity even if process cleanup fails.
            if os.name == 'nt':
                # /T addresses descendants of this specific child, not other JVMs.
                cleanup = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                         capture_output=True, timeout=30)
                record['cleanupReturncode'] = cleanup.returncode
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=30)
        finally:
            output.join(timeout=10)
            complete = not output.is_alive() and not pump_errors
            record.update(returncode=process.poll(), elapsedSeconds=time.monotonic() - started,
                          completedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          status='timed_out' if timed_out else 'passed' if process.returncode == 0 and complete else 'failed',
                          outputComplete=bool(complete))
            save()
        if timed_out:
            raise subprocess.TimeoutExpired(command, timeout)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        if output.is_alive() or pump_errors:
            raise RuntimeError('CI command output was not completely retained')
    return record_path
