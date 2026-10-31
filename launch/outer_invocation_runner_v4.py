"""Bounded outer provenance capture. Imports no validator; owns no actor cancellation."""
import argparse
import datetime
import hashlib
import json
import os
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time

MiB = 1048576
MAX_STREAM = 2 * MiB
MAX_TOTAL = 8 * MiB
MAX_SNAPSHOT = 65536
MAX_SNAPSHOTS = 16
MAX_RESOURCES = 64
F11 = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
       'st_size', 'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks')


def require(value, message):
    if not value:
        raise ValueError(message)


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def full(value):
    return {key: getattr(value, key) for key in F11}


def read_pin(reference, *, executable=False):
    require(set(reference) == {'path', 'bytes', 'sha256'} and
            type(reference['bytes']) is int and 0 < reference['bytes'] <= 2 * MiB,
            'Invalid metadata/source reference')
    path = reference['path']
    require(isinstance(path, str) and os.path.isabs(path) and os.path.normpath(path) == path,
            'Reference path must be canonical absolute')
    fd = os.open(path, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = full(os.fstat(source.fileno()))
        require(stat.S_ISREG(before['st_mode']) and before['st_uid'] == os.getuid() and
                before['st_nlink'] == 1 and stat.S_IMODE(before['st_mode']) == (0o500 if executable else 0o400) and
                before['st_size'] == reference['bytes'], 'Pinned source/metadata seal differs')
        raw = source.read(reference['bytes'] + 1)
        require(len(raw) == reference['bytes'] and hashlib.sha256(raw).hexdigest() ==
                reference['sha256'], 'Pinned whole bytes/SHA differs')
        require(full(os.fstat(source.fileno())) == before == full(os.lstat(path)),
                'Pinned full11 changed during NoTime read')
    return raw


def ref(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        before = full(os.fstat(source.fileno()))
        digest, size = hashlib.sha256(), 0
        while data := source.read(65536):
            digest.update(data)
            size += len(data)
        require(full(os.fstat(source.fileno())) == before == full(os.lstat(path)),
                'Output pin full11 changed')
    return dict(path=path, bytes=size, sha256=digest.hexdigest())


def observation():
    def virtual(path):
        with open(path) as source:
            return source.read(65536)
    result = {'atUtc': stamp(), 'filesystemAvailableBytes': {}}
    for path in ('/workspace', '/tmp', '/dev/shm'):
        item = os.statvfs(path)
        result['filesystemAvailableBytes'][path] = item.f_bavail * item.f_frsize
    result['hostMemAvailableBytes'] = int(next(row.split()[1] for row in
        virtual('/proc/meminfo').splitlines() if row.startswith('MemAvailable:'))) * 1024
    try:
        maximum = virtual('/sys/fs/cgroup/memory.max').strip()
        current = int(virtual('/sys/fs/cgroup/memory.current'))
        inactive = int(dict(row.split() for row in
            virtual('/sys/fs/cgroup/memory.stat').splitlines())['inactive_file'])
        result['visibleCurrentCgroupOnly'] = {'limitBytes': None if maximum == 'max' else int(maximum),
            'currentBytes': current, 'inactiveFileBytes': inactive,
            'cacheReserveBytes': 128 * MiB, 'activeFileCredited': False,
            'safeHeadroomBytes': None if maximum == 'max' else int(maximum) - current + inactive - 128 * MiB}
    except (OSError, KeyError, ValueError) as error:
        result['cgroupObservationUnavailable'] = type(error).__name__
    return result


def proc_snapshot(pid, pidfd, baseline_start):
    record = {'atUtc': stamp(), 'pid': pid, 'status': 'unavailable', 'argv': None}
    if pidfd is None:
        record['reason'] = 'No pidfd identity; no proc attribution attempted'
        return record, baseline_start
    if select.select([pidfd], [], [], 0)[0]:
        record['reason'] = 'Process already exited/too fast; no proc attribution attempted'
        return record, baseline_start
    try:
        def bounded(path, maximum):
            with open(path, 'rb') as source:
                value = source.read(maximum + 1)
            require(len(value) <= maximum, 'proc snapshot exceeds bound')
            return value
        def start_ticks():
            raw = bounded('/proc/' + str(pid) + '/stat', 4096)
            # After the final ')' the first field is process state (field3).
            return int(raw[raw.rfind(b')') + 2:].split()[19])
        before = start_ticks()
        raw = bounded('/proc/' + str(pid) + '/cmdline', MAX_SNAPSHOT)
        after = start_ticks()
        require(before == after and (baseline_start is None or before == baseline_start),
                'PID starttime changed during snapshot')
        if select.select([pidfd], [], [], 0)[0]:
            record['reason'] = 'Exit raced with proc read; bytes not attributed'
            return record, baseline_start
        if not raw:
            record['reason'] = 'Empty cmdline while process identity checked'
            return record, before
        # Store exact bytes, not a lossy UTF8 reconstruction.
        record.update(status='observed', startTimeClockTicks=before,
                      rawCmdlineHex=raw.hex(), rawBytes=len(raw),
                      argv=[part.decode('utf-8', errors='backslashreplace')
                            for part in raw.split(b'\0') if part],
                      displayArgvIsOnlyDecodedProjection=True, pidfdIdentityChecked=True)
        return record, before
    except (OSError, ValueError, IndexError) as error:
        record['reason'] = type(error).__name__ + ': ' + str(error)[:512]
        return record, baseline_start


def run(config_reference):
    self_reference = ref(__file__)
    self_full11 = full(os.lstat(__file__))
    read_pin(self_reference)
    config = json.loads(read_pin(config_reference))
    expected = {'schemaVersion', 'kind', 'requestedArgv', 'cwd', 'outputRoot',
                'wrapper', 'loader', 'policy', 'envelopeApproval', 'actorInputApproval',
                'realInterpreter', 'interpreterFull11', 'captureLimitBytes'}
    require(set(config) == expected and type(config['schemaVersion']) is int and
            config['schemaVersion'] == 1 and config['kind'] == 'C21-outer-invocation-provenance-plan-v1',
            'Unexpected outer invocation plan')
    sources = {name: read_pin(config[name], executable=name == 'wrapper') for name in
               ('wrapper', 'loader', 'policy', 'envelopeApproval', 'actorInputApproval')}
    policy = json.loads(sources['policy'])
    argv = config['requestedArgv']
    require(isinstance(argv, list) and 3 <= len(argv) <= 512 and all(isinstance(a, str) and
            '\x00' not in a and len(a.encode()) <= 65536 for a in argv) and
            sum(len(a.encode()) + 1 for a in argv) <= MAX_SNAPSHOT and
            argv[:2] == ['python3', '-B'] and argv[2] in policy['entryScripts'],
            'Requested argv is not an exact permitted script mode')
    require(config['realInterpreter'] == policy['realInterpreter'] and
            config['interpreterFull11'] == policy['interpreterFull11'] and
            full(os.lstat(config['realInterpreter'])) == config['interpreterFull11'],
            'Absolute interpreter metadata identity differs')
    limit = config['captureLimitBytes']
    require(type(limit) is int and 1 <= limit <= MAX_STREAM, 'Stream bound differs')
    output = config['outputRoot']
    require(isinstance(output, str) and output.startswith('/dev/shm/') and
            os.path.normpath(output) == output and os.path.realpath(os.path.dirname(output)) ==
            os.path.dirname(output), 'Output must be a new canonical SHM directory')
    require(os.path.isabs(config['cwd']) and os.path.realpath(config['cwd']) == config['cwd'],
            'Working directory must be canonical absolute')
    before = observation()
    require(before['filesystemAvailableBytes']['/dev/shm'] >= 2 * 1024**3 + MAX_TOTAL,
            'SHM floor plus full output reservation does not fit')
    env = dict(os.environ)
    original_path = env.get('PATH', '')
    prefix = os.path.dirname(config['wrapper']['path'])
    env['PATH'] = prefix + os.pathsep + original_path
    require(shutil.which('python3', path=env['PATH']) == config['wrapper']['path'],
            'Command-local python3 does not resolve exact wrapper')
    os.mkdir(output, 0o700)
    channels = {}
    for name in ('stdout', 'stderr'):
        path = output + '/' + name + '.bin'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        channels[name] = {'fd': fd, 'path': path, 'total': 0, 'retained': 0,
                          'sha': hashlib.sha256(), 'writeFailure': None}
    snapshots, resources, incidents, signals = [], [before], [], []
    snapshot_charge = 0
    previous_handlers = {}
    # Do not terminate a driver whose body child lives in a different session.
    def defer(signum, frame):
        if len(signals) < 16:
            signals.append({'atUtc': stamp(), 'signal': signum, 'forwarded': False})
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, defer)
    child, pidfd, selector = None, None, selectors.DefaultSelector()
    started, mono = stamp(), time.monotonic()
    start_ticks, next_snapshot, next_resource = None, 0.0, 0.0
    try:
        child = subprocess.Popen(argv, cwd=config['cwd'], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, close_fds=True)
        try:
            pidfd = os.pidfd_open(child.pid)
        except OSError as error:
            incidents.append({'kind': 'pidfd-unavailable', 'type': type(error).__name__})
        for name, pipe in (('stdout', child.stdout), ('stderr', child.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map():
            elapsed = time.monotonic() - mono
            if elapsed >= next_snapshot and len(snapshots) < MAX_SNAPSHOTS:
                item, start_ticks = proc_snapshot(child.pid, pidfd, start_ticks)
                charge = len(json.dumps(item, ensure_ascii=True).encode()) + 64
                if snapshot_charge + charge <= MiB:
                    snapshots.append(item)
                    snapshot_charge += charge
                else:
                    snapshots.append({'atUtc': stamp(), 'pid': child.pid, 'status': 'unavailable',
                                      'reason': 'Aggregate snapshot byte budget reached'})
                    next_snapshot = float('inf')
                if next_snapshot != float('inf'):
                    next_snapshot = elapsed + (0.02 if len(snapshots) < 4 else 1.0)
            if elapsed >= next_resource and len(resources) < MAX_RESOURCES:
                try:
                    resources.append(observation())
                except Exception as error:
                    if len(incidents) < 64:
                        incidents.append({'kind': 'resource-observation-unavailable', 'type': type(error).__name__})
                next_resource = elapsed + 5.0
            for key, mask in selector.select(timeout=0.05):
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                channel = channels[key.data]
                channel['sha'].update(data)
                channel['total'] += len(data)
                keep = data[:max(0, limit - channel['retained'])]
                if keep and channel['writeFailure'] is None:
                    try:
                        view = memoryview(keep)
                        while view:
                            written = os.write(channel['fd'], view)
                            require(written > 0, 'Capture write made no progress')
                            channel['retained'] += written
                            view = view[written:]
                    except (OSError, ValueError) as error:
                        channel['writeFailure'] = type(error).__name__ + ': ' + str(error)[:512]
                # Overflow is drained+hashed, never mislabelled as a retained full stream.
        exit_code = child.wait()
    except BaseException as error:
        incidents.append({'kind': 'outer-supervisor-exception', 'type': type(error).__name__,
                          'message': str(error)[:512], 'actorTerminationAttempted': False})
        # Preserve direct actor supervision: continue draining, never orphan it by killing
        # only its driver. Remaining output is hashed but may no longer be retained.
        if child is not None:
            # Rebuild independently from BOTH child pipes: setup may have failed
            # before either stream reached the original selector.
            remaining = {}
            poller = select.poll()
            for name, pipe in (('stdout', child.stdout), ('stderr', child.stderr)):
                if pipe is not None and not pipe.closed:
                    remaining[pipe.fileno()] = (name, pipe)
                    poller.register(pipe.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
            while remaining:
                try:
                    ready = poller.poll(100)
                except (InterruptedError, OSError):
                    # A broken observation backend does not authorize orphaning a
                    # separately supervised actor. Retry without a kill/timeout.
                    time.sleep(0.1)
                    continue
                for fd, flags in ready:
                    name, pipe = remaining[fd]
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b''
                    if not data:
                        poller.unregister(fd)
                        del remaining[fd]
                        pipe.close()
                        continue
                    channel = channels[name]
                    channel['sha'].update(data)
                    channel['total'] += len(data)
                    if channel['writeFailure'] is None:
                        channel['writeFailure'] = 'Capture retention stopped after supervisor incident'
            exit_code = child.wait()
        else:
            exit_code = None
    finally:
        # No timeout/kill path: existing actor monitoring/cancellation ownership is unchanged.
        selector.close()
        if pidfd is not None:
            os.close(pidfd)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        for channel in channels.values():
            os.fsync(channel['fd'])
            os.close(channel['fd'])
    completed = stamp()
    require(ref(__file__) == self_reference and full(os.lstat(__file__)) == self_full11,
            'Runner source changed across actual invocation')
    captures = {}
    for name, channel in channels.items():
        captures[name] = {'retainedRawBytes': ref(channel['path']),
            'totalObservedBytes': channel['total'], 'wholeObservedStreamSha256': channel['sha'].hexdigest(),
            'complete': channel['writeFailure'] is None and channel['total'] == channel['retained'],
            'limitBytes': limit, 'writeFailure': channel['writeFailure'],
            'rawBytesNoDecodeOrBOMNormalization': True,
            'overflowPolicy': 'Keep exact bounded prefix, drain/hash remainder, await natural actor exit'}
    require(len(json.dumps(snapshots).encode()) <= 2 * MiB, 'Snapshot ledger exceeds budget')
    snapshots_raw = (json.dumps(snapshots, indent=2) + '\n').encode()
    snapshot_path = output + '/proc-snapshots.json'
    fd = os.open(snapshot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as destination:
        destination.write(snapshots_raw); destination.flush(); os.fsync(destination.fileno())
    resources.append(observation())
    capture_ok = all(item['complete'] for item in captures.values()) and not incidents and not signals
    result = {'schemaVersion': 1, 'kind': 'C21-actual-outer-invocation-provenance-v1',
        'config': config_reference, 'requestedArgv': argv, 'cwd': config['cwd'],
        'commandLocalPATHPrefix': prefix, 'globalPATHUnchanged': os.environ.get('PATH', '') == original_path,
        'wrapper': config['wrapper'], 'loader': config['loader'], 'policy': config['policy'],
        'envelopeApproval': config['envelopeApproval'], 'actorInputApproval': config['actorInputApproval'],
        'realInterpreter': config['realInterpreter'], 'interpreterFull11': config['interpreterFull11'],
        'runnerSource': self_reference, 'runnerSourceFull11UnchangedAcrossInvocation': True, 'startedAtUtc': started, 'completedAtUtc': completed,
        'actualPopenPid': None if child is None else child.pid, 'actualExitCode': exit_code, 'pidfdAcquired': pidfd is not None,
        'procSnapshots': ref(snapshot_path), 'observedSnapshotCount': sum(x['status'] == 'observed' for x in snapshots),
        'requestedArgvEqualsObservedOSArgv': False,
        'argvClaim': 'Requested argv retained separately; actual snapshots may be absent/too-fast and use real -c exec argv.',
        'captures': captures, 'resources': resources, 'resourceObservationMaximumSamplesIncludingFinal': 65, 'resourceObservationScope': 'filesystem/host/current-visible-cgroup only; frozen actor monitor remains authoritative',
        'supervisorIncidents': incidents, 'deferredSignals': signals,
        'captureComplete': capture_ok, 'runnerExitCode': 0 if exit_code == 0 and capture_ok else 2,
        'noActorTimeoutKillOrValidationResourceMonkeypatch': True,
        'cancellationLimit': 'No outer cancellation implementation; SIGINT/TERM deferred while draining until actor exits. External SIGKILL or fatal runner failure is not claimed recoverable.',
        'actorOutputDirectoriesAndAcceptanceRemainSeparatelyApproved': True,
        'newCaptureLogicalBudgetBytes': MAX_TOTAL, 'newCapturePhysicalBudgetBytes': MAX_TOTAL, 'packageOrRuntimeAcceptance': False}
    raw = (json.dumps(result, indent=2) + '\n').encode()
    current = [os.lstat(output + '/' + name) for name in os.listdir(output)]
    unit = os.statvfs(output).f_frsize
    require(len(raw) <= MiB and sum(x.st_size for x in current) + len(raw) <= MAX_TOTAL and
            sum(x.st_blocks * 512 for x in current) + os.lstat(output).st_blocks * 512 +
            ((len(raw) + unit - 1) // unit) * unit <= MAX_TOTAL,
            'Capture metadata exceeds logical/physical total8MiB bound')
    path = output + '/result.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as destination:
        destination.write(raw); destination.flush(); os.fsync(destination.fileno())
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    os.fsync(directory); os.close(directory)
    print(json.dumps({'result': ref(path), 'actualExitCode': exit_code,
                      'captureComplete': capture_ok, 'runnerExitCode': result['runnerExitCode']}))
    return result['runnerExitCode']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--config-sha256', required=True)
    args = parser.parse_args()
    return run({'path': args.config, 'bytes': os.lstat(args.config).st_size,
                'sha256': args.config_sha256})


if __name__ == '__main__':
    raise SystemExit(main())
