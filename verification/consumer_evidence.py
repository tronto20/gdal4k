"""Retain a completed consumer invocation's results, including failed invocations."""
import datetime
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time


COPY_RESERVE_SECONDS = 10
CLEANUP_RESERVE_SECONDS = 10
MAX_PHASE_BYTES = 64 * 1024
MAX_FAILURE_VERSION_BYTES = 32 * 1024


def _directory(path, *, create):
    path = Path(path).absolute()
    # Check existing ancestors before mkdir so a symlink cannot redirect a new
    # evidence/workspace directory into an unrelated source or cache tree.
    for part in [path, *path.parents]:
        try:
            attributes = part.lstat()
        except FileNotFoundError:
            continue
        if part.is_symlink() or getattr(attributes, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('Refusing symlinked/reparse consumer directory: ' + str(part))
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.resolve(strict=True) != path:
        raise RuntimeError('Consumer directory must be canonical and nonsymlinked: ' + str(path))
    return path


def consumer_workspace_root(root):
    """Own temporary consumers on the same volume as their retained evidence."""
    root = _directory(root, create=False)
    workspace = _directory(root / 'verification/build/consumer-workspaces', create=True)
    evidence = _directory(root / 'verification/results', create=True)
    if workspace.stat().st_dev != evidence.stat().st_dev:
        raise RuntimeError('Consumer workspace and evidence must use the same volume')
    return workspace


@contextmanager
def consumer_workspace(root, *, phase_recorder=None, deadline=None):
    """A same-volume unique project with explicit, observable owned cleanup.

    Public delete=False leaves failure-only deadline omissions in place; no
    private TemporaryDirectory finalizer is modified or detached.
    """
    deadline = _validate_deadline(deadline)
    parent = consumer_workspace_root(root)
    temporary = tempfile.TemporaryDirectory(prefix='gdal4k-published-consumer-', dir=parent, delete=False)
    project = Path(temporary.name)
    primary = None

    def note(message):
        if primary is not None:
            primary.add_note(message)

    def record(event, **details):
        if phase_recorder is not None:
            try:
                phase_recorder.record('workspace-delete', event, path=str(project), **details)
            except Exception as error:
                if primary is None:
                    raise
                note('Consumer cleanup phase record failed: ' + type(error).__name__ + ': ' + str(error))

    try:
        _directory(project, create=False)
        yield temporary.name
    except BaseException as error:
        primary = error
        raise
    finally:
        if deadline is not None and deadline - time.monotonic() <= CLEANUP_RESERVE_SECONDS:
            message = 'Consumer workspace cleanup omitted inside the completion reserve; owned path remains: ' + str(project)
            if primary is None:
                error = _PreservationDeadline(message)
                try:
                    record('omitted', deadline=deadline, reason='completion-reserve-exhausted', ownedPathRemaining=True)
                except Exception as secondary:
                    error.add_note('Cleanup omission record failed: ' + type(secondary).__name__ + ': ' + str(secondary))
                raise error
            note(message)
            record('omitted', deadline=deadline, reason='completion-reserve-exhausted', ownedPathRemaining=True,
                   primaryFailure=type(primary).__name__)
        else:
            try:
                record('before', deadline=deadline)
                if project.exists() or project.is_symlink():
                    _directory(project, create=False)
                temporary.cleanup()
                record('after', ownedPathRemaining=project.exists())
            except BaseException as error:
                if primary is None:
                    error.add_note('Consumer workspace cleanup failed; owned path may remain: ' + str(project))
                    try:
                        record('error', exception=type(error).__name__, message=str(error)[:4096], ownedPathRemaining=project.exists())
                    except Exception as secondary:
                        error.add_note('Cleanup error record failed: ' + type(secondary).__name__ + ': ' + str(secondary))
                    raise
                note('Consumer workspace cleanup failed for owned path ' + str(project) + ': ' + type(error).__name__ + ': ' + str(error))
                record('error', exception=type(error).__name__, message=str(error)[:4096], ownedPathRemaining=project.exists(),
                       primaryFailure=type(primary).__name__)


def _validate_deadline(deadline):
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
        raise ValueError('Consumer evidence deadline must be a finite positive monotonic time')
    return float(deadline)


def _fsync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_json(path, value, *, max_bytes=None):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked consumer evidence file: ' + str(path))
    raw = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if max_bytes is not None and (len(raw) > max_bytes or (path.exists() and path.stat().st_size > max_bytes)):
        raise ValueError('Failure JSON exceeds its reserved atomic-version budget')
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _allocation_unit(directory):
    if os.name != 'nt':
        value = os.statvfs(directory)
        unit = max(1, value.f_frsize or value.f_bsize)
        if unit > MAX_FAILURE_VERSION_BYTES:
            raise ValueError('Filesystem allocation unit cannot fit the reserved failure-record versions')
        return unit
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    volume = ctypes.create_unicode_buffer(32768)
    get_volume = kernel.GetVolumePathNameW
    get_volume.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_volume.restype = ctypes.c_int
    if not get_volume(str(directory), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    get_space = kernel.GetDiskFreeSpaceW
    get_space.argtypes = [ctypes.c_wchar_p] + [ctypes.POINTER(ctypes.c_uint32)] * 4
    get_space.restype = ctypes.c_int
    sectors, size, available, total = (ctypes.c_uint32() for _ in range(4))
    if not get_space(volume.value, ctypes.byref(sectors), ctypes.byref(size), ctypes.byref(available), ctypes.byref(total)):
        raise ctypes.WinError(ctypes.get_last_error())
    unit = sectors.value * size.value
    if not 0 < unit <= MAX_FAILURE_VERSION_BYTES:
        raise ValueError('Filesystem allocation unit cannot fit the reserved failure-record versions')
    return unit


class PhaseRecorder:
    """Bounded create-only phase records outside a disposable consumer project."""
    def __init__(self, directory, *, clock=None, max_total_bytes=None):
        if max_total_bytes is not None and (isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int) or max_total_bytes <= 0):
            raise ValueError('Consumer phase aggregate budget must be a positive integer')
        parent = _directory(directory, create=True)
        self.max_total_bytes = max_total_bytes
        self.allocation_unit = _allocation_unit(parent) if max_total_bytes is not None else 1
        self.charged_bytes = 0
        self.directory = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
        self.clock = time.monotonic if clock is None else clock
        self.started = self.clock()
        self.sequence = 0
        self.lock = threading.Lock()
        self.phase_started = {}

    def record(self, phase, event, **details):
        if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', value) for value in [phase, event]):
            raise ValueError('Invalid consumer evidence phase/event')
        with self.lock:
            now = self.clock()
            value = dict(schemaVersion=1, phase=phase, event=event,
                         atUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         monotonic=now, elapsedSeconds=now - self.started, details=details)
            if event in ['after', 'error', 'omitted'] and phase in self.phase_started:
                value['phaseElapsedSeconds'] = now - self.phase_started[phase]
            raw = (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8')
            if len(raw) > MAX_PHASE_BYTES:
                raise ValueError('Consumer phase record exceeds64KiB')
            if self.sequence >= 10000:
                raise ValueError('Consumer phase record count exceeds10000')
            charge = ((len(raw) + self.allocation_unit - 1) // self.allocation_unit) * self.allocation_unit
            if self.max_total_bytes is not None and self.charged_bytes + charge > self.max_total_bytes:
                raise ValueError('Consumer phase aggregate allocation budget exhausted')
            # Reserve before open; a partial write/fsync failure must not allow
            # another record to reuse the same aggregate reservation.
            self.charged_bytes += charge
            if event == 'before':
                self.phase_started[phase] = now
            elif event in ['after', 'error', 'omitted']:
                self.phase_started.pop(phase, None)
            self.sequence += 1
            path = self.directory / f'{self.sequence:05d}-{phase}-{event}.json'
            with path.open('xb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(self.directory)
            return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


class _PreservationDeadline(TimeoutError):
    pass


def _secondary_failure(primary, stage, error, *, field='consumer_evidence_errors'):
    item = dict(stage=stage, exception=type(error).__name__, message=str(error)[:4096])
    errors = getattr(primary, field, None)
    if not isinstance(errors, list):
        errors = []
        setattr(primary, field, errors)
    errors.append(item)
    primary.add_note('Consumer ' + stage + ' failed: ' + item['exception'] + ': ' + item['message'])
    return item


def _archive_success(destination):
    previous = destination / 'result.json'
    if previous.is_symlink():
        raise RuntimeError('Refusing symlinked consumer result: ' + str(previous))
    if previous.exists():
        index = 0
        while ((destination / f'result-before-failure-{index}.json').exists()
               or (destination / f'result-before-failure-{index}.json').is_symlink()):
            index += 1
        previous.rename(destination / f'result-before-failure-{index}.json')


def _failure_identity(failure, preservation=None):
    value = {'result': 'failed', 'exception': type(failure).__name__,
             'returncode': getattr(failure, 'returncode', None),
             'artifact_preservation': dict(preservation if preservation is not None else getattr(failure, 'consumer_preservation', {}))}
    for field in ['consumer_evidence_errors', 'consumer_restore_errors']:
        errors = getattr(failure, field, None)
        if isinstance(errors, list) and errors:
            value[field] = list(errors)
    restoration = getattr(failure, 'consumer_restore_report', None)
    if isinstance(restoration, dict):
        value['consumer_restore_report'] = restoration
    return value


def _write_failure(path, value, phase_recorder):
    # Native reserves64KiB separately from the phase quota. At most32KiB each
    # for the old file and its atomic replacement keeps that transient bound.
    quota = getattr(phase_recorder, 'max_total_bytes', None) if phase_recorder is not None else None
    if isinstance(quota, int) and not isinstance(quota, bool) and quota > 0:
        _atomic_json(path, value, max_bytes=MAX_FAILURE_VERSION_BYTES)
    else:
        _atomic_json(path, value)


@contextmanager
def consumer_failure_guard(destination, *, phase_recorder=None):
    """Record any body or inner workspace-exit failure without replacing it."""
    try:
        yield
    except BaseException as primary:
        def record(event, **details):
            if phase_recorder is not None:
                try:
                    phase_recorder.record('failure-guard', event, **details)
                except Exception as error:
                    _secondary_failure(primary, 'failure-guard-phase-' + event, error)

        record('before', destination=str(destination), primaryFailure=type(primary).__name__)
        target = None
        try:
            target = _directory(destination, create=True)
        except Exception as error:
            _secondary_failure(primary, 'failure-guard-directory', error)
            record('error', stage='directory', failureRecordWritten=False)
        if target is None:
            raise
        try:
            _archive_success(target)
        except Exception as error:
            _secondary_failure(primary, 'failure-guard-archive-success', error)
        written = False
        try:
            _write_failure(target / 'failure.json', _failure_identity(primary), phase_recorder)
            written = True
        except Exception as error:
            _secondary_failure(primary, 'failure-guard-write', error)
        errors_before = len(getattr(primary, 'consumer_evidence_errors', []))
        record('after', failureRecordWritten=written)
        if written and len(getattr(primary, 'consumer_evidence_errors', [])) != errors_before:
            try:
                _write_failure(target / 'failure.json', _failure_identity(primary), phase_recorder)
            except Exception as error:
                _secondary_failure(primary, 'failure-guard-final-write', error)
        raise


def restore_hidden_paths(hidden, *, phase_recorder=None, failure=None):
    """Attempt every owned restore even when diagnostics or one rename fails."""
    report = dict(attempted=0, restored=[], errors=[])
    first_error = None

    def failed(stage, error, **details):
        nonlocal first_error
        if first_error is None:
            first_error = error
        item = dict(stage=stage, exception=type(error).__name__, message=str(error)[:4096], **details)
        report['errors'].append(item)
        if failure is not None:
            _secondary_failure(failure, stage, error, field='consumer_restore_errors')

    def record(event, **details):
        if phase_recorder is not None:
            try:
                phase_recorder.record('restore-hidden', event, **details)
            except Exception as error:
                failed('restore-phase-' + event, error, **details)

    try:
        entries = list(hidden)
    except Exception as error:
        failed('restore-list', error)
        entries = []
    for index, pair in enumerate(reversed(entries)):
        report['attempted'] += 1
        try:
            original, moved = (Path(value).absolute() for value in pair)
            if moved != original.with_name(original.name + '-consumer-hidden'):
                raise ValueError('Restore must be the exact owned consumer-hidden sibling')
            _directory(original.parent, create=False)
            # This exact directory entry was hidden by the caller. Rename that
            # entry back even if it is a link/junction; do not follow or mutate
            # its target. Only canonical parents and the fixed sibling pair are
            # accepted, and an existing original is never overwritten.
            moved.lstat()
            if original.exists() or original.is_symlink():
                raise RuntimeError('Refusing to replace an existing producer path during restore')
        except Exception as error:
            failed('restore-validation', error, index=index)
            continue
        details = dict(index=index, original=str(original), moved=str(moved))
        record('before', **details)
        try:
            moved.rename(original)
        except Exception as error:
            failed('restore-rename', error, **details)
            record('error', **details)
        else:
            report['restored'].append(dict(original=str(original), moved=str(moved)))
            record('after', **details)
    if report['errors'] and failure is None:
        error = RuntimeError('Hidden producer restoration/phase recording failed: ' + str(len(report['errors'])) + ' errors')
        error.consumer_restore_report = report
        raise error from first_error
    if failure is not None:
        failure.consumer_restore_report = report
    return report


def preserve_consumer_evidence(consumer, destination, failure=None, *, deadline=None, phase_recorder=None):
    preservation = {'hardlinked_files': 0, 'copied_files': 0}
    deadline_omissions = []

    def initial_error(stage, error):
        if failure is None:
            raise error
        item = _secondary_failure(failure, stage, error)
        preservation.update(incomplete=True, errors=[item])
        setattr(failure, 'consumer_preservation', preservation)
        if phase_recorder is not None:
            try:
                phase_recorder.record('preserve-initial', 'error', stage=stage, exception=type(error).__name__, message=str(error)[:4096])
            except Exception as secondary:
                _secondary_failure(failure, 'preserve-initial-record', secondary)
        return preservation

    try:
        consumer = _directory(consumer, create=False)
        destination = _directory(destination, create=True)
        deadline = _validate_deadline(deadline)
    except Exception as error:
        return initial_error('preserve-validation', error)
    if failure is not None:
        setattr(failure, 'consumer_preservation', preservation)

    def record(phase, event, **details):
        if phase_recorder is not None:
            phase_recorder.record(phase, event, **details)

    def failure_record():
        return _failure_identity(failure, preservation)

    def budget(label, *, copying=False):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= (COPY_RESERVE_SECONDS if copying else 0):
                message = 'Evidence deadline leaves no ' + ('copy reserve: ' if copying else 'time: ') + label
                deadline_omissions.append(message)
                raise _PreservationDeadline(message)

    def run_phase(phase, action):
        record(phase, 'before')
        try:
            budget(phase)
            value = action()
        except Exception as error:
            record(phase, 'omitted' if isinstance(error, _PreservationDeadline) else 'error',
                   exception=type(error).__name__, message=str(error)[:4096])
            raise
        record(phase, 'after')
        return value

    def preserve_file(source, output):
        # The Gradle invocation has returned. Only its owned temporary files may
        # be hardlinked; SDK/cache files retain the existing ordinary-copy path.
        source, output = Path(source), Path(output)
        budget(str(source))
        if output.is_symlink():
            raise RuntimeError('Refusing symlinked consumer evidence file: ' + str(output))
        # Keep any earlier retained bytes until a new complete link/copy exists.
        # A deadline/copy failure must not erase that earlier file first.
        descriptor, name = tempfile.mkstemp(prefix='.' + output.name + '-', suffix='.tmp', dir=output.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.unlink()
            copied = False
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(consumer.resolve()):
                budget(str(source), copying=True)
                shutil.copy2(source, temporary)
                copied = True
            else:
                try:
                    os.link(source, temporary)
                except OSError as error:
                    if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EINVAL}:
                        raise
                    budget(str(source), copying=True)
                    shutil.copy2(source, temporary)
                    copied = True
            os.replace(temporary, output)
            preservation['copied_files' if copied else 'hardlinked_files'] += 1
            return str(output)
        finally:
            temporary.unlink(missing_ok=True)

    if failure is not None:
        # Preserve a previous success and write the primary failure identity
        # BEFORE any test-result/bin traversal or potentially blocking copy.
        try:
            _archive_success(destination)
        except Exception as error:
            return initial_error('preserve-archive-success', error)
        try:
            _write_failure(destination / 'failure.json', failure_record(), phase_recorder)
        except Exception as error:
            preservation.update(incomplete=True, errors=[dict(exception=type(error).__name__, message=str(error)[:4096])])
            try:
                record('failure-identity', 'error', exception=type(error).__name__, message=str(error)[:4096])
            except Exception:
                pass  # The caller's original command failure remains primary.
            return preservation
    try:
        record('preserve', 'before', deadline=deadline,
               consumerVolume=consumer.stat().st_dev, evidenceVolume=destination.stat().st_dev,
               primaryFailure=type(failure).__name__ if failure is not None else None)
        budget('preserve')
        for folder in ['build/test-results', 'build/outputs/androidTest-results', 'build/bin']:
            if (consumer / folder).exists():
                run_phase('copy-' + Path(folder).name,
                          lambda folder=folder: shutil.copytree(consumer / folder, destination / Path(folder).name,
                                                               dirs_exist_ok=True, copy_function=preserve_file))
        for name in ['build/jvm-runtime-files.txt', 'build/jvm-runtime-coordinates.txt']:
            if (consumer / name).is_file():
                run_phase('copy-' + Path(name).name, lambda name=name: preserve_file(consumer / name, destination / Path(name).name))
        if failure is not None:
            for name in ['build.gradle.kts', 'gradle.properties']:
                if (consumer / name).is_file():
                    run_phase('copy-' + name, lambda name=name: preserve_file(consumer / name, destination / name))
        record('preserve', 'after', artifactPreservation=dict(preservation))
    except Exception as error:
        preservation.update(incomplete=True, errors=[dict(exception=type(error).__name__, message=str(error)[:4096])])
        if deadline_omissions:
            preservation['omitted'] = deadline_omissions
        if failure is None:
            raise
        # A preservation/copy error cannot replace an already-existing command
        # timeout. The caller's finally returns and the original error propagates.
    finally:
        if failure is not None:
            try:
                _write_failure(destination / 'failure.json', failure_record(), phase_recorder)
            except Exception as error:
                preservation['incomplete'] = True
                preservation.setdefault('errors', []).append(dict(exception=type(error).__name__, message=str(error)[:4096]))
                try:
                    record('failure-update', 'error', exception=type(error).__name__, message=str(error)[:4096])
                except Exception:
                    pass  # A secondary metadata error cannot hide the timeout.
    return preservation
