"""Opt-in owned Windows Native-consumer diagnostics; never a runtime fallback."""
import argparse
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time

import windows_native_stall as native
from windows_process import private_directory, private_file, OwnedProcess, same_identity

TOTAL_CEILING = 512 * 1024**2
METADATA_RESERVE = 4 * 1024**2
CAPTURE_RESERVE = native.MAX_DUMP
MAX_RECORD = 64 * 1024
MAX_DIRECT_RECORDS = 8
MAX_EVENT_BYTES = 320 * 1024


def need(ok, why):
    if not ok:
        raise ValueError(why)


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def regular(path, *, directory=False):
    path = Path(path)
    need(path.is_absolute() and path.resolve(strict=True) == path, 'Expected canonical diagnostic path')
    for item in [path, *path.parents]:
        need(not item.is_symlink() and not getattr(item.lstat(), 'st_file_attributes', 0) & 0x400,
             'Diagnostic path contains a symlink/reparse point')
    need(path.is_dir() if directory else stat.S_ISREG(path.lstat().st_mode), 'Wrong diagnostic path kind')
    return path


def read_record(path):
    path = regular(path)
    need(path.stat().st_size <= MAX_RECORD, 'Oversized Native launch metadata')
    def unique(pairs):
        out = {}
        for key, value in pairs:
            need(key not in out, 'Duplicate Native launch metadata key')
            out[key] = value
        return out
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def write_record(path, value):
    path = Path(path)
    regular(path.parent, directory=True)
    raw = (json.dumps(value, indent=2) + '\n').encode()
    need(len(raw) <= MAX_RECORD, 'Oversized Native diagnostic record')
    need(not path.exists(), 'Refuse to overwrite Native diagnostic metadata')
    need(len(list(path.parent.glob('*.json'))) < MAX_DIRECT_RECORDS, 'Native direct metadata record quota exhausted')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.record-', delete=False) as output:
            temporary = Path(output.name)
            if os.name == 'nt': private_file(temporary)
            output.write(raw); output.flush(); os.fsync(output.fileno())
        os.link(temporary, path)  # Publish complete bytes, refusing overwrite.
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def valid_budget(value):
    need(type(value) is int and 2 * CAPTURE_RESERVE + METADATA_RESERVE < value <= TOTAL_CEILING,
         'Native diagnostics require an explicit budget within the512MiB total ceiling')
    return value


def write_owner(directory, value):
    directory = regular(directory, directory=True)
    need(type(value.get('gradlePid')) is int and value['gradlePid'] > 0, 'Invalid Gradle owner PID')
    java = regular(value['javaExecutable'])
    need(ntpath.basename(str(java)).casefold() == 'java.exe', 'Native diagnostics require the actual Windows Gradle Java image')
    need(os.name == 'nt' and value['gradlePid'] == os.getppid(), 'Gradle owner must be this helper actual Windows parent')
    from windows_process import _api
    identity = _api().identity(value['gradlePid'])
    need(ntpath.normcase(identity['imagePath']) == ntpath.normcase(str(java)), 'Gradle parent image differs')
    write_record(directory / 'gradle-owner.json', dict(gradlePid=value['gradlePid'], javaExecutable=str(java),
                 javaExecutableSha256=sha(java), workingDirectory=str(regular(value['workingDirectory'], directory=True)),
                 actualConfigurationProcessIdentity=identity, requestedOwnerFromGradle=True,
                 ownedJobIdentityNotYetVerified=True))


def write_launch(directory, value):
    directory = regular(directory, directory=True)
    context = read_record(directory / 'context.json')
    ready = read_record(directory / 'callback-quota-ready.json')
    need(ready['actualWindowsOwnedChildSelfCheck'] is True and ready['status'] == 'passed',
         'Actual bounded Windows callback self-check is not ready')
    import windows_dump_quota
    need(ready['sourceSha256'] == sha(windows_dump_quota.__file__), 'Callback self-check source changed')
    budget = valid_budget(context['budgetBytes'])
    root = regular(context['consumerRoot'], directory=True)
    executable = regular(value['executable'])
    bin_root = regular(root / 'build/bin', directory=True)
    need(executable.is_relative_to(bin_root), 'Native launch executable is outside the owned consumer build/bin')
    need(value.get('task') == 'mingwX64Test' and type(value.get('gradlePid')) is int and value['gradlePid'] > 0,
         'Wrong Native diagnostic task/Gradle PID')
    owner = read_record(directory / 'gradle-owner.json')
    need(value['gradlePid'] == owner['gradlePid'], 'Native launch belongs to another Gradle process')
    size = executable.stat().st_size
    charged = size + 2 * CAPTURE_RESERVE + METADATA_RESERVE
    need(0 < size and charged <= budget, 'Native executable plus capture/metadata charge exceeds diagnostic budget')
    need(executable.stat().st_dev == directory.stat().st_dev, 'Native launch retention requires the evidence filesystem')
    need(shutil.disk_usage(directory).free >= 2 * CAPTURE_RESERVE + METADATA_RESERVE + 1024**3,
         'Native diagnostics would consume the minimum1GiB free reserve')
    before = executable.stat()
    write_record(directory / 'launch-preparing.json', dict(executable=str(executable), executableBytes=size,
                 conservativeChargedBytes=charged, sourceIsOwnedConsumerBuild=True))
    identity = sha(executable)
    evidence = regular(context['evidenceRoot'], directory=True)
    need(directory.is_relative_to(evidence), 'Native diagnostics are outside the owned evidence root')
    retained = evidence / 'bin' / executable.relative_to(bin_root)
    retained.parent.mkdir(parents=True, exist_ok=True)
    regular(retained.parent, directory=True)
    os.link(executable, retained)  # Own immutable build output only; no copy fallback.
    try:
        after = executable.stat()
        need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'Native executable changed before launch')
        need(retained.stat().st_ino == after.st_ino and sha(retained) == identity, 'Native executable hardlink identity differs')
        write_record(directory / 'launch-intent.json', dict(task=value['task'], gradlePid=value['gradlePid'],
                     executable=str(executable), executableBytes=size, executableSha256=identity,
                     retainedExecutable=str(retained), retainedByHardlink=True, additionalPayloadAllocationBytes=0,
                     conservativeChargedBytes=charged, budgetBytes=budget,
                     workingDirectory=value['workingDirectory'], requestedExtraArgs=value['args'],
                     requestedIncludePatterns=value['includePatterns'], requestedExcludePatterns=value['excludePatterns'],
                     requestedArgumentsAreNotObservedNativeArgv=True, sourceBindings=context['sourceBindings']))
    except BaseException:
        retained.unlink(missing_ok=True)
        raise


def prepare_native_diagnostics(directory, *, budget, deadline):
    """Actual callback controls complete before Gradle/Native compilation starts."""
    valid_budget(budget)
    directory = regular(directory, directory=True)
    private_directory(directory)
    need(shutil.disk_usage(directory).free >= budget + 1024**3, 'Native diagnostic disk reservation refused')
    from windows_dump_quota import self_check, require_self_check
    checked = require_self_check(self_check(directory / 'writer-selfcheck', timeout=min(20, deadline - time.monotonic() - 5)))
    write_record(directory / 'callback-quota-ready.json', dict(status='passed', actualWindowsOwnedChildSelfCheck=True,
                 sourceSha256=checked['sourceSha256'], compiledSdkAbiVerified=False))
    return checked


class NativeConsumerWatchdog:
    """Only observes the held Gradle Job; diagnostics never shorten test bounds."""
    def __init__(self, owned, directory, *, budget, deadline, functional_deadline,
                 callback_self_check, clock=time.monotonic,
                 stall_seconds=60, collector_seconds=20):
        need(math.isfinite(deadline) and deadline > clock(), 'Invalid Native completion deadline')
        need(0 < collector_seconds <= 20 and stall_seconds > collector_seconds, 'Invalid Native capture timing')
        self.budget = valid_budget(budget)
        self.directory = regular(directory, directory=True)
        private_directory(self.directory)
        need(shutil.disk_usage(self.directory).free >= self.budget + 1024**3,
             'Native diagnostic reservation would consume the minimum free reserve')
        from windows_dump_quota import require_self_check
        checked = require_self_check(callback_self_check)
        need(read_record(self.directory / 'callback-quota-ready.json')['sourceSha256'] == checked['sourceSha256'],
             'Native callback preflight is not this process source-qualified result')
        self.owned, self.deadline, self.clock = owned, deadline, clock
        self.functional_deadline = functional_deadline
        self.started = self.progress = clock()
        self.stall, self.limit = stall_seconds, collector_seconds
        self.lock = threading.Lock(); self.buffer = b''
        self.events = []; self.sessions = []; self.rounds = 0; self.last_capture = None
        self.launch = None; self.owner = None; self.closed = False
        self.native_identity = None
        self.last_absence = None
        self.events_path = self.directory / 'events.jsonl'
        with self.events_path.open('xb') as output:
            private_file(self.events_path)
            output.flush(); os.fsync(output.fileno())

    def _event(self, kind, **details):
        row = dict(sequence=len(self.events) + 1, monotonic=self.clock(), kind=kind, **details)
        raw = (json.dumps(row) + '\n').encode()
        need(len(raw) <= MAX_RECORD and self.events_path.stat().st_size + len(raw) <= MAX_EVENT_BYTES,
             'Native observer metadata quota exhausted')
        with self.events_path.open('ab') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        self.events.append(row)

    def observe(self, data):
        with self.lock:
            if self.closed: return
            self.buffer += data
            if len(self.buffer) > MAX_RECORD and b'\n' not in self.buffer:
                self.buffer = b''; self._event('oversized-output-line'); return
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if len(line) > MAX_RECORD: continue
                text = line.rstrip(b'\r').decode(errors='replace')
                if any(marker in text for marker in [' STARTED', ' PASSED', ' FAILED', ' SKIPPED', '##teamcity[testStarted', '##teamcity[testFinished']):
                    self.progress = self.clock()
                    self._event('existing-test-progress', text=text[:4096])

    def _verified(self, pid, image, expected=None):
        identity = self.owned.member_identity(pid, expected=expected)
        need(ntpath.normcase(ntpath.normpath(identity['imagePath'])) == ntpath.normcase(ntpath.normpath(image)),
             'Owned diagnostic process image differs from launch record')
        return identity

    def _load_launch(self):
        launch_path = self.directory / 'launch-intent.json'
        if launch_path.exists() and self.launch is None:
            launch = read_record(launch_path)
            owner = read_record(self.directory / 'gradle-owner.json')
            need(launch['gradlePid'] == owner['gradlePid'] and launch['budgetBytes'] == self.budget,
                 'Native launch metadata owner/budget differs')
            need(launch['conservativeChargedBytes'] <= self.budget and launch['retainedByHardlink'] is True,
                 'Native critical retention charge failed')
            for field in ['executable', 'retainedExecutable']:
                file = regular(launch[field])
                need(file.stat().st_size == launch['executableBytes'] and sha(file) == launch['executableSha256'],
                     'Native retained executable bytes changed')
            self.launch = launch
            self.progress = self.clock()
            self._event('native-launch-intent-verified', executable=launch['executable'], chargedBytes=launch['conservativeChargedBytes'])

    def _target(self):
        owner_path = self.directory / 'gradle-owner.json'
        if not owner_path.exists():
            if self.last_absence != 'no-owner':
                self._event('gradle-owner-record-not-observed'); self.last_absence = 'no-owner'
            return None
        value = read_record(owner_path)
        need(sha(regular(value['javaExecutable'])) == value['javaExecutableSha256'], 'Gradle Java file identity changed')
        self.owner = self._verified(value['gradlePid'], value['javaExecutable'],
                                    self.owner or value['actualConfigurationProcessIdentity'])
        self._load_launch()
        members = self.owned.member_identities()
        if self.launch is not None:
            matches = [row for row in members if ntpath.normcase(ntpath.normpath(row['imagePath'])) ==
                       ntpath.normcase(ntpath.normpath(self.launch['executable']))]
            need(len(matches) <= 1, 'Ambiguous owned Native executable process')
            if matches:
                target = self._verified(matches[0]['pid'], self.launch['executable'], matches[0])
                if self.native_identity is None:
                    self.native_identity = target; self.progress = self.clock()
                    self._event('native-child-discovered', target=target)
                else:
                    need(same_identity(target, self.native_identity), 'Native process creation identity changed')
                self.last_absence = None
                return 'native-consumer', target
        missing = ('no-native', bool(self.launch))
        if self.last_absence != missing:
            self._event('native-child-not-observed', launchIntentObserved=self.launch is not None,
                        ownedMembers=members, verifiedGradleOwner=self.owner)
            self.last_absence = missing
        return 'owned-gradle-wait', self._verified(self.owner['pid'], value['javaExecutable'], self.owner)

    def _gradle_wait_text(self, target, directory):
        """Existing sibling-JDK tool, owned separately; at most64KiB/10s."""
        owner = read_record(self.directory / 'gradle-owner.json')
        tool = Path(owner['javaExecutable']).with_name('jcmd.exe')
        if not tool.exists():
            self._event('gradle-thread-tool-unavailable'); return
        regular(tool)
        self._verified(target['pid'], owner['javaExecutable'], target)
        path = directory / 'gradle-threads.txt'
        command = [str(tool), str(target['pid']), 'Thread.print', '-l']
        need(self.functional_deadline - self.clock() >= 12, 'Insufficient functional timeout reserve for Gradle wait text')
        until = min(self.deadline - 5, self.functional_deadline - 2, self.clock() + 10)
        need(until > self.clock(), 'No completion reserve for owned Gradle wait text')
        errors, count = [], [0]
        # Keep the process object referenced across the numeric-PID attach,
        # preventing PID reuse while jcmd is in flight.
        held = self.owned.api.open_process(target['pid'])
        try:
            native._verify_inherited(self.owned.api, held, self.owned.job, target)
            return self._run_gradle_wait_text(target, directory, owner, tool, path, command, until, held)
        finally:
            self.owned.api.close(held)

    def _run_gradle_wait_text(self, target, directory, owner, tool, path, command, until, held):
        errors, count = [], [0]
        with path.open('xb') as output:
            private_file(path)
            tool_process = OwnedProcess(command, cwd=owner['workingDirectory'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            def pump():
                try:
                    with tool_process.process.stdout:
                        while data := tool_process.process.stdout.read1(4096):
                            if count[0] + len(data) > 64 * 1024:
                                errors.append('thread-output-quota'); tool_process.terminate_tree(); return
                            output.write(data); output.flush(); count[0] += len(data)
                except Exception as error:
                    errors.append(type(error).__name__ + ':' + str(error))
            thread = threading.Thread(target=pump, daemon=True); thread.start()
            try:
                tool_process.process.wait(timeout=max(0, until - self.clock()))
            except subprocess.TimeoutExpired:
                errors.append('thread-tool-timeout'); tool_process.terminate_tree()
            finally:
                try: tool_process.close_before(min(until, self.clock() + 1))
                except Exception as error: errors.append(type(error).__name__ + ':' + str(error))
                thread.join(timeout=max(0, min(1, until - self.clock())))
                if thread.is_alive(): errors.append('thread-output-incomplete')
            output.flush(); os.fsync(output.fileno())
        native._verify_inherited(self.owned.api, held, self.owned.job, target)
        self._event('owned-gradle-thread-text', path=str(path), bytes=count[0], sha256=sha(path),
                    tool=str(tool), toolSha256=sha(tool), target=target, errors=errors,
                    targetIdentityBeforeAndAfterMatched=True)

    def poll(self):
        with self.lock:
            for row in self.sessions:
                if 'result' not in row:
                    result = row['session'].poll()
                    if result is not None:
                        row['result'] = result
                        self._event('capture-result', role=row['role'], status=result.get('status'),
                                    receipt=str(row['directory'] / 'collector.json'))
            try:
                selected = self._target()
            except (ValueError, OSError, ProcessLookupError) as error:
                selected = None
                if self.last_absence != str(error):
                    self._event('target-refused', message=str(error), error=type(error).__name__)
                    self.last_absence = str(error)
            now = self.clock()
            if self.rounds >= 2 or now - self.progress < self.stall or (self.last_capture is not None and now - self.last_capture < self.stall):
                return None
            try:
                if selected is None: return None
                role, target = selected
                # Reserve one round for a Native child appearing after compilation.
                if role == 'owned-gradle-wait' and any(row['role'] == role for row in self.sessions): return None
                self.rounds += 1; self.last_capture = now
                need(self.deadline - now >= self.limit + 5, 'Insufficient completion reserve for another Native capture')
                need(shutil.disk_usage(self.directory).free >= CAPTURE_RESERVE + METADATA_RESERVE + 1024**3,
                     'Native capture would consume free-space reserve')
                used = sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())
                exe = self.launch['executableBytes'] if self.launch else 0
                need(used + exe + CAPTURE_RESERVE + METADATA_RESERVE <= self.budget, 'Native diagnostic total charge exhausted')
                self._event('capture-start', role=role, target=target, limitSeconds=self.limit)
                output = self.directory / ('capture-%d' % self.rounds)
                session = native._start_capture(self.owned, target, output,
                                                require_gdal=False, expected_sha=None, copy_native=False,
                                                clock=self.clock, limit=self.limit)
                self.sessions.append(dict(role=role, target=target, directory=output, session=session))
                if role == 'owned-gradle-wait': self._gradle_wait_text(target, output)
            except (ValueError, OSError, ProcessLookupError) as error:
                self._event('capture-refused', message=str(error), error=type(error).__name__)
            return None

    def close(self):
        with self.lock:
            if self.closed: return self.result
            self.closed = True
            self._load_launch()
            for row in self.sessions:
                result = row.get('result') or row['session'].close()
                self._event('capture-final', role=row['role'], status=(result or {}).get('status'),
                            receipt=str(row['directory'] / 'collector.json'))
            self._event('observer-closed', captureRounds=self.rounds, rootCauseOrRuntimeAcceptance=False)
            self.result = dict(eventCount=len(self.events), eventsFile=str(self.events_path),
                               eventsSha256=sha(self.events_path), launchIdentityRetained=self.launch is not None,
                               captureRounds=self.rounds, rootCauseOrRuntimeAcceptance=False)
            return self.result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--owner-json')
    group.add_argument('--launch-json')
    args = parser.parse_args()
    value = json.loads(args.owner_json or args.launch_json)
    if args.owner_json: write_owner(args.directory, value)
    else: write_launch(args.directory, value)


if __name__ == '__main__':
    main()
