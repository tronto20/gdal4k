"""Bound a CI child command and retain its output before the job-level timeout."""
import datetime
import hashlib
import json
import math
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
                      record_path=None, log_path=None, windows_peak=False,
                      windows_column_diagnostics=False, windows_column_dll_sha256=None,
                      windows_native_diagnostics=None, native_diagnostic_budget=None,
                      completion_deadline=None, native_callback_self_check=None):
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
    if windows_column_diagnostics and (not _windows_host() or windows_peak):
        raise ValueError('Column stall diagnostics require an actual Windows source-test command, without peak measurement')
    if completion_deadline is not None and (isinstance(completion_deadline, bool)
            or not math.isfinite(completion_deadline) or completion_deadline <= time.monotonic()):
        raise ValueError('Completion deadline must be a finite future monotonic time')
    if windows_native_diagnostics is not None:
        if not _windows_host() or windows_peak or windows_column_diagnostics or completion_deadline is None:
            raise ValueError('Native consumer diagnostics require a bounded actual Windows consumer command')
        from native_consumer_diagnostics import valid_budget
        valid_budget(native_diagnostic_budget)
        if completion_deadline <= time.monotonic() + timeout + 10:
            raise ValueError('Native evidence deadline must retain cleanup reserve after the functional timeout')
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
    watchdog = None
    diagnostic_errors = []
    diagnostic_omitted = [0]
    def diagnostic_error(error):
        if windows_native_diagnostics is not None:
            if len(diagnostic_errors) >= 16:
                diagnostic_omitted[0] += 1
                return
            diagnostic_errors.append(dict(type=type(error).__name__[:128], message=str(error)[:1024]))
        else:
            diagnostic_errors.append(dict(type=type(error).__name__, message=str(error)))
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
                if windows_column_diagnostics:
                    from windows_native_stall import ColumnReadWatchdog
                    watchdog = ColumnReadWatchdog(owned, directory, record_path.stem,
                                                  expected_dll_sha256=windows_column_dll_sha256)
                elif windows_native_diagnostics is not None:
                    from native_consumer_diagnostics import NativeConsumerWatchdog
                    watchdog = NativeConsumerWatchdog(owned, windows_native_diagnostics,
                                                       budget=native_diagnostic_budget, deadline=completion_deadline,
                                                       functional_deadline=started + timeout,
                                                       callback_self_check=native_callback_self_check)
            else:
                process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, env=env, **options)
        except Exception as error:
            record.update(status='failed', exception=type(error).__name__, outputComplete=False)
            if owned is not None:
                if windows_native_diagnostics is None:
                    owned.terminate_tree()
                    owned.process.wait(timeout=30)
                    record['cleanup'] = owned.close()
                    if owned.process.stdout is not None:
                        owned.process.stdout.close()
                else:
                    cleanup_errors = []
                    try:
                        owned.terminate_tree()
                        owned.process.wait(timeout=max(0, min(30, completion_deadline - time.monotonic() - 4)))
                    except Exception as cleanup_error:
                        cleanup_errors.append(dict(type=type(cleanup_error).__name__, message=str(cleanup_error)))
                    try:
                        record['cleanup'] = owned.close_before(completion_deadline - 2)
                    except Exception as cleanup_error:
                        cleanup_errors.append(dict(type=type(cleanup_error).__name__, message=str(cleanup_error)))
                        if hasattr(cleanup_error, 'cleanup'): record['cleanup'] = cleanup_error.cleanup
                    if owned.process.stdout is not None:
                        try: owned.process.stdout.close()
                        except Exception as cleanup_error:
                            cleanup_errors.append(dict(type=type(cleanup_error).__name__, message=str(cleanup_error)))
                    if cleanup_errors:
                        record['startupCleanupFailures'] = cleanup_errors
                        error.add_note('Native startup cleanup incomplete; inspect retained command identity')
            save()
            raise
        record.update(pid=process.pid, status='running')
        if completion_deadline is not None:
            record['completionDeadlineMonotonic'] = completion_deadline
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
                        if watchdog is not None:
                            try:
                                watchdog.observe(chunk)
                            except Exception as error:
                                diagnostic_error(error)
            except Exception as error:
                pump_errors.append(error)

        output = threading.Thread(target=pump, daemon=True)
        output.start()
        timed_out = False
        native_stall = None
        evidence_error = None
        try:
            if watchdog is None:
                process.wait(timeout=timeout)
            else:
                deadline = started + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        process.wait(timeout=min(1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        native_stall = watchdog.poll()
                        if native_stall is not None:
                            record['nativeColumnStall'] = native_stall
                            save()
                            # The fixture's existing 180s coroutine bound cannot
                            # interrupt a blocked JNI call. Fail its owned tree.
                            raise subprocess.TimeoutExpired(command, 180)
        except subprocess.TimeoutExpired:
            timed_out = True
            record['status'] = 'timed_out'
            save()  # Retain the command identity even if process cleanup fails.
            if _windows_host():
                try:
                    owned.terminate_tree()
                except Exception as error:
                    if windows_native_diagnostics is None: raise
                    record['terminationFailure'] = dict(type=type(error).__name__, message=str(error))
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            wait_seconds = 30 if windows_native_diagnostics is None else max(0, min(30, completion_deadline - time.monotonic() - 6))
            try:
                process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired as error:
                record['cleanupWaitFailure'] = dict(type=type(error).__name__, timeoutSeconds=wait_seconds)
                if windows_native_diagnostics is None: raise
        finally:
            if watchdog is not None:
                try:
                    key = 'nativeConsumerDiagnostics' if windows_native_diagnostics is not None else 'nativeColumnDiagnostics'
                    record[key] = watchdog.close()
                    if windows_native_diagnostics is not None and not timed_out and process.poll() == 0 and watchdog.launch is None:
                        evidence_error = RuntimeError('Successful Native consumer did not retain its pre-run executable/launch identity')
                except Exception as error:
                    diagnostic_error(error)
                    if windows_native_diagnostics is not None:
                        evidence_error = error
                if diagnostic_errors and windows_native_diagnostics is None:
                    record['nativeColumnDiagnosticErrors'] = diagnostic_errors
            if owned is not None:
                try:
                    if windows_peak and not timed_out and process.poll() == 0:
                        record['peakMemory'] = owned.peak_after_exit()
                except Exception as error:
                    evidence_error = error
                    record['finalPeakFailure'] = dict(type=type(error).__name__, message=str(error))
                finally:
                    try:
                        record['cleanup'] = (owned.close_before(completion_deadline - 3)
                                             if windows_native_diagnostics is not None else owned.close())
                    except Exception as error:
                        evidence_error = error
                        record['cleanupFailure'] = dict(type=type(error).__name__, message=str(error))
                        if hasattr(error, 'cleanup'): record['cleanup'] = error.cleanup
            join_seconds = 10 if windows_native_diagnostics is None else max(0, min(10, completion_deadline - time.monotonic() - 1))
            output.join(timeout=join_seconds)
            if diagnostic_errors and windows_native_diagnostics is not None:
                record['nativeConsumerDiagnosticErrors'] = list(diagnostic_errors)
                record['nativeConsumerDiagnosticErrorsOmitted'] = diagnostic_omitted[0]
                if evidence_error is None:
                    evidence_error = RuntimeError('Native diagnostic metadata is incomplete')
            complete = not output.is_alive() and not pump_errors
            record.update(returncode=process.poll(), elapsedSeconds=time.monotonic() - started,
                          completedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          status='timed_out' if timed_out else 'passed' if process.returncode == 0 and complete and evidence_error is None else 'failed',
                          outputComplete=bool(complete), logSha256=output_hash.hexdigest())
            save()
        if timed_out:
            if native_stall is not None:
                raise RuntimeError('Source column read made no progress for 180s; inspect nativeColumnStall and retained native/Java diagnostics')
            raise subprocess.TimeoutExpired(command, timeout)
        if evidence_error is not None:
            raise RuntimeError('Windows kernel peak or owned-tree evidence failed; inspect retained receipt') from evidence_error
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        if output.is_alive() or pump_errors:
            raise RuntimeError('CI command output was not completely retained')
    return record_path
