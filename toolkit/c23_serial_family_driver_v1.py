"""Serial, resource-guarded launcher for genuinely new current cohort family byte audits.

Configuration and full11 metadata stay in Python. No SDK build, network request,
existing-file cleanup, capability gate or merge is performed by this launcher.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time

import recovery_archive_core_v3 as core
import cohort_policy_v1 as intent
COHORT = intent.initialize_for_process()
import c23_family_source_v1 as family

LOCK = str(Path(__file__).with_name('current-family-byte-driver.lock'))


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ref3(value):
    return {key: value[key] for key in ('path', 'bytes', 'sha256')}


def source_pin(path):
    with core.open_readonly(path) as source:
        before = core.full11(os.fstat(source.fileno()))
        digest, size = hashlib.sha256(), 0
        while data := source.read(core.CHUNK):
            size += len(data)
            digest.update(data)
        core.need(core.full11(os.fstat(source.fileno())) == before and
                  core.full11(os.lstat(path)) == before, 'Pinned output source changed')
    return {'path': str(path), 'bytes': size, 'sha256': digest.hexdigest()}


def sealed_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    core.need(len(raw) <= 2 * core.MiB, 'Driver metadata record exceeds bound')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'wb') as destination:
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())
    fsync_directory(path.parent)
    return source_pin(path)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir_durable(path):
    os.mkdir(path, 0o700)
    fsync_directory(path)
    fsync_directory(path.parent)


def visible_cgroups(reserve):
    with open('/proc/self/cgroup', 'r', encoding='ascii') as source:
        relative = next(line.rstrip().split('::', 1)[1] for line in source if line.startswith('0::'))
    root = Path('/sys/fs/cgroup')
    current = root / relative.lstrip('/')
    result = []
    while True:
        with open(current / 'memory.max', 'r', encoding='ascii') as source:
            raw_maximum = source.read(64).strip()
        with open(current / 'memory.current', 'r', encoding='ascii') as source:
            used = int(source.read(64))
        with open(current / 'memory.stat', 'r', encoding='ascii') as source:
            memory_stat = dict((line.split()[0], int(line.split()[1])) for line in source if line.strip())
        inactive = memory_stat['inactive_file']
        limit = None if raw_maximum == 'max' else int(raw_maximum)
        result.append({'path': str(current), 'limitBytes': limit, 'currentBytes': used,
                       'inactiveFileBytes': inactive, 'cacheReserveBytes': reserve,
                       'grossHeadroomBytes': None if limit is None else max(0, limit - used + inactive),
                       'safeHeadroomBytes': None if limit is None else cgroup_safe_headroom(limit, used, inactive, reserve),
                       'activeFileCredited': False})
        if current == root:
            break
        core.need(current.is_relative_to(root), 'Visible cgroup escaped declared hierarchy')
        current = current.parent
    core.need(any(item['limitBytes'] is not None for item in result), 'No visible finite cgroup limit was observed')
    return result


def cgroup_safe_headroom(limit, used, inactive_file, reserve):
    return max(0, limit - used + inactive_file - reserve)


def allocated_tree(path):
    """Observation only; current child output may legitimately grow."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        with os.scandir(fd) as entries:
            items = [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
    finally:
        os.close(fd)
    total = os.lstat(path).st_blocks * 512
    for name, value in items:
        target = path / name
        core.need(value.st_uid == os.getuid() and not stat.S_ISLNK(value.st_mode), 'Unexpected proof-tree owner/link')
        if stat.S_ISDIR(value.st_mode):
            total += allocated_tree(target)
        else:
            core.need(stat.S_ISREG(value.st_mode) and value.st_nlink == 1, 'Unexpected proof-tree special/hardlink')
            total += value.st_blocks * 512
    return total


def filesystem_observation(path):
    """Read filesystem identity and free space from the same directory fd."""
    path = Path(path)
    core.need(path.is_absolute() and '..' not in path.parts, 'Invalid filesystem observation path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info, free = os.fstat(fd), os.fstatvfs(fd)
        return {'path': str(path), 'device': info.st_dev,
                'availableBytes': free.f_bavail * free.f_frsize}
    finally:
        os.close(fd)


def observation(proof_root, scratch_root, cache_reserve=128 * core.MiB):
    with open('/proc/meminfo', 'r', encoding='ascii') as source:
        memory = source.read(16384)
    available = next(int(line.split()[1]) * 1024 for line in memory.splitlines() if line.startswith('MemAvailable:'))
    destinations = {role: filesystem_observation(path) for role, path in
                    (('workspace', '/workspace'), ('proof', proof_root), ('scratch', scratch_root),
                     ('tmp', '/tmp'), ('shm', '/dev/shm'))}
    return {'atUtc': stamp(), 'workspaceAvailableBytes': destinations['workspace']['availableBytes'],
            'scratchFilesystemAvailableBytes': destinations['scratch']['availableBytes'],
            'shmAvailableBytes': destinations['shm']['availableBytes'],
            'filesystemDestinations': destinations, 'cgroups': visible_cgroups(cache_reserve),
            'hostMemAvailableBytes': available, 'proofAllocatedBytes': allocated_tree(proof_root)}


def physical_requirements(sample, policy, *, prospective):
    """Combine actual filesystem charges by device; floors are never added twice."""
    core.need(type(prospective) is bool, 'Prospective admission mode must be a Boolean')
    core.need(type(policy['shmMetadataReservationBytes']) is int and
              policy['shmMetadataReservationBytes'] >= 8 * core.MiB,
              'Explicit SHM outer-capture/new-metadata reservation must be at least8MiB')
    destinations = sample['filesystemDestinations']
    core.need(type(destinations) is dict and
              set(destinations) == {'workspace', 'proof', 'scratch', 'tmp', 'shm'},
              'Filesystem destination observations are incomplete')
    floors = {'workspace': policy['workspaceFloorBytes'], 'proof': policy['workspaceFloorBytes'],
              'scratch': policy['scratchFloorBytes'], 'tmp': policy['scratchFloorBytes'],
              'shm': policy['shmFloorBytes']}
    charges = {'workspace': 0, 'proof': policy['perFamilyMetadataReservationBytes'] + policy['ledgerReserveBytes'],
               'scratch': 192 * core.MiB, 'tmp': 0, 'shm': policy['shmMetadataReservationBytes']}
    groups = {}
    for role, observed in destinations.items():
        core.need(type(observed) is dict and set(observed) == {'path', 'device', 'availableBytes'} and
                  type(observed['path']) is str and Path(observed['path']).is_absolute() and
                  '..' not in Path(observed['path']).parts and type(observed['device']) is int and
                  observed['device'] > 0 and type(observed['availableBytes']) is int and
                  observed['availableBytes'] >= 0, 'Invalid actual filesystem identity/free-space observation')
        group = groups.setdefault(observed['device'], {'device': observed['device'], 'roles': [],
                                  'availableBytes': observed['availableBytes'], 'floorBytes': 0,
                                  'prospectiveAllocationBytes': 0})
        group['roles'].append(role)
        group['availableBytes'] = min(group['availableBytes'], observed['availableBytes'])
        group['floorBytes'] = max(group['floorBytes'], floors[role])
        group['prospectiveAllocationBytes'] += charges[role] if prospective else 0
    return [groups[device] for device in sorted(groups)]


def admitted(sample, policy, *, prospective):
    core.need(policy['proofPhysicalBudgetBytes'] <= 256 * core.MiB, 'Proof metadata policy exceeds256MiB ceiling')
    extra = policy['perFamilyMetadataReservationBytes'] + policy['ledgerReserveBytes'] if prospective else 0
    for filesystem in physical_requirements(sample, policy, prospective=prospective):
        core.need(filesystem['availableBytes'] >= filesystem['floorBytes'] + filesystem['prospectiveAllocationBytes'],
                  'Actual filesystem floor plus summed destination allocations does not fit: ' +
                  ','.join(filesystem['roles']))
    # Keep the existing base416MiB memory reserve and add the declared SHM writer.
    memory_extra = (128 * core.MiB + 192 * core.MiB + policy['perFamilyMetadataReservationBytes'] +
                    policy['shmMetadataReservationBytes']) if prospective else 0
    core.need(sample['hostMemAvailableBytes'] >= policy['hostMemAvailableFloorBytes'] + memory_extra,
              'Host memory floor plus prospective RSS/scratch/metadata-cache reservation does not fit')
    for cgroup in sample['cgroups']:
        core.need(cgroup['cacheReserveBytes'] == policy['cgroupInactiveFileReserveBytes'] and
                  cgroup['activeFileCredited'] is False, 'Cgroup inactive-file-only cache formula differs')
        if cgroup['limitBytes'] is not None:
            core.need(cgroup['grossHeadroomBytes'] >= policy['cgroupGrossFloorBytes'] + memory_extra and
                      cgroup['safeHeadroomBytes'] >= policy['cgroupSafeFloorBytes'] + memory_extra,
                      'Visible cgroup safe/gross floor plus prospective reservations does not fit')
    core.need(sample['proofAllocatedBytes'] + extra <= policy['proofPhysicalBudgetBytes'],
              'Proof metadata physical budget plus prospective reservation does not fit')


def terminate_exact(child, pidfd):
    if child.poll() is not None:
        return
    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        child.wait(timeout=5)


BOOTSTRAP = '''import os,sys
fd=int(sys.argv[1])
token=os.read(fd,1)
os.close(fd)
if token != b'G':
    raise SystemExit(125)
os.execvp(sys.argv[2],sys.argv[2:])
'''


def launch_monitored(arguments, logging, *, pidfd_factory=os.pidfd_open):
    """No byte-audit body can start until its parent holds the exact pidfd.

    On pidfd acquisition failure the only inherited pipe endpoint is closed at
    EOF, so the private bootstrap exits without executing the body. No PID-based
    signal fallback is used.
    """
    readfd, writefd = os.pipe()
    child, pidfd = None, None
    try:
        child = subprocess.Popen(['python3', '-B', '-c', BOOTSTRAP, str(readfd), *arguments],
                                 stdout=logging, stderr=subprocess.STDOUT, start_new_session=True,
                                 close_fds=True, pass_fds=(readfd,))
        os.close(readfd)
        readfd = None
        try:
            pidfd = pidfd_factory(child.pid)
        except BaseException:
            os.close(writefd)
            writefd = None
            child.wait(timeout=5)
            raise
        core.need(os.write(writefd, b'G') == 1, 'Private body start token was not fully written')
        os.close(writefd)
        writefd = None
        return child, pidfd
    except BaseException:
        if readfd is not None:
            os.close(readfd)
        if writefd is not None:
            os.close(writefd)
        if child is not None and pidfd is not None:
            terminate_exact(child, pidfd)
            os.close(pidfd)
        raise


def verify_actual_child(result, fid, plan_ref, approved_sources):
    core.need(result['kind'] == 'GDAL-cohort-family-byte-audit-result' and
              result['status'] == 'passed-byte-audit' and result['result']['familyId'] == fid and
              result['capabilityAccepted'] is False and intent.require_intent_reference(result.get('cohortManifest')), 'Actual child final result differs')
    actual = family.pin_read(ref3(result['invocation']))
    core.need(actual['kind'] == 'GDAL-cohort-family-byte-audit-invocation' and
              (actual['head'], actual['tree'], actual['base']) == (family.HEAD, family.TREE, family.BASE) and
              actual['plan'] == plan_ref and intent.require_intent_reference(actual.get('cohortManifest')) and actual['sources'] == approved_sources and
              actual['capabilityAccepted'] is False, 'Actual child invocation is not this exact approved plan/source')


def verify_imported_sources(approval, body_source):
    paths = [core.__file__, family.__file__, core.strict_zip.__file__, core.transport.__file__, family.collector.__file__, intent.__file__]
    actual = []
    for path in paths:
        resolved = Path(path).resolve()
        info = os.lstat(resolved)
        core.need(info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o400,
                  'Actual imported executable source seal/owner/link differs')
        actual.append(source_pin(resolved))
    canonical = lambda items: sorted((item['path'], item['bytes'], item['sha256']) for item in items)
    core.need(len(actual) == 6 and canonical(actual) == canonical(approval['sources']) and
              body_source == source_pin(Path(family.__file__).resolve()),
              'Actual imported executable closure/body source differs from exact approved six sources')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    intent.add_arguments(parser)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--config-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config_ref = {'path': str(args.config), 'bytes': os.lstat(args.config).st_size, 'sha256': args.config_sha256}
    config = family.pin_read(config_ref)
    core.need(set(config) == {'schemaVersion', 'kind', 'head', 'tree', 'base', 'catalogue', 'materializationMap',
                             'sourceApproval', 'bodySource', 'orderedFamilyIds', 'scratchBase', 'resourcePolicy', 'cohortManifest'},
              'Unexpected driver configuration schema')
    core.need(type(config['schemaVersion']) is int and config['schemaVersion'] == 1 and config['kind'] == 'GDAL-cohort-serial-family-driver-plan' and
              (config['head'], config['tree'], config['base']) == (family.HEAD, family.TREE, family.BASE),
              'Driver source H/T/B differs')
    intent.require_plan_intent(config)
    policy = config['resourcePolicy']
    core.need(set(policy) == {'workspaceFloorBytes', 'scratchFloorBytes', 'hostMemAvailableFloorBytes',
                             'shmFloorBytes', 'cgroupSafeFloorBytes', 'cgroupGrossFloorBytes', 'cgroupInactiveFileReserveBytes',
                             'proofPhysicalBudgetBytes', 'perFamilyMetadataReservationBytes', 'ledgerReserveBytes',
                             'monitorIntervalSeconds', 'shmMetadataReservationBytes'}, 'Unexpected resource policy')
    core.need(all(type(v) is int and v > 0 for k, v in policy.items() if k != 'monitorIntervalSeconds') and
              policy['workspaceFloorBytes'] >= 32 * core.MiB and
              policy['scratchFloorBytes'] >= 2 * 1024 ** 3 and policy['shmFloorBytes'] >= 2 * 1024 ** 3 and
              policy['hostMemAvailableFloorBytes'] >= 2 * 1024 ** 3 and
              policy['cgroupSafeFloorBytes'] >= 1920 * core.MiB and policy['cgroupGrossFloorBytes'] >= 2048 * core.MiB and
              policy['cgroupInactiveFileReserveBytes'] >= 128 * core.MiB and
              policy['perFamilyMetadataReservationBytes'] >= 96 * core.MiB and
              policy['ledgerReserveBytes'] >= core.MiB and
              policy['shmMetadataReservationBytes'] >= 8 * core.MiB and
              policy['proofPhysicalBudgetBytes'] <= 256 * core.MiB and
              type(policy['monitorIntervalSeconds']) in (int, float) and 0.1 <= policy['monitorIntervalSeconds'] <= 1,
              'Resource policy values differ from declared minimum contract')
    inputs = family.FamilyInputs(ref3(config['catalogue']), ref3(config['materializationMap']))
    ordered = config['orderedFamilyIds']
    core.need(isinstance(ordered, list) and all(type(fid) is int and fid > 0 for fid in ordered) and len(ordered) == len(COHORT['familyNames']) and len(set(ordered)) == len(COHORT['familyNames']) and
              set(ordered) == set(inputs.families), 'Serial plan does not cover the exact current logical family set')
    approval = family.pin_read(ref3(config['sourceApproval']))
    core.need(approval['kind'] == 'GDAL-cohort-archive-source-approval' and
              approval['approvedForActualFamilyByteAudit'] is True and intent.require_intent_reference(approval.get('cohortManifest')), 'Root body source approval missing')
    for reference in approval['sources']:
        family.pin_read(ref3(reference), json_value=False)
    core.need(ref3(config['bodySource']) in approval['sources'], 'Actual body source is not approved')
    verify_imported_sources(approval, ref3(config['bodySource']))
    body_path = Path(config['bodySource']['path'])
    core.need(body_path.name == 'c23_family_source_v1.py', 'Expected current frozen familyv5 source')
    scratch_base = Path(config['scratchBase'])
    core.need(args.output.is_absolute() and scratch_base.is_absolute() and
              args.output.is_relative_to(Path(__file__).parent) and
              scratch_base.is_relative_to('/tmp') and '..' not in args.output.parts and '..' not in scratch_base.parts,
              'Proof/scratch roots must be new controlled recovery paths')
    for material in inputs.materialization['artifacts'].values():
        raw_path = Path(material['path'])
        core.need(not raw_path.is_relative_to(scratch_base) and not raw_path.is_relative_to(args.output) and
                  not scratch_base.is_relative_to(raw_path) and not args.output.is_relative_to(raw_path),
                  'Driver output/scratch overlaps an immutable raw input')
    lockfd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lockst = os.fstat(lockfd)
    core.need(lockst.st_uid == os.getuid() and stat.S_ISREG(lockst.st_mode) and lockst.st_nlink == 1,
              'Global driver lock identity differs')
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    mkdir_durable(args.output)
    mkdir_durable(scratch_base)
    plans_root = args.output / 'plans'
    outputs_root = args.output / 'families'
    mkdir_durable(plans_root)
    mkdir_durable(outputs_root)
    invocation_ref = sealed_json(args.output / 'invocation.json', {
        'schemaVersion': 1, 'kind': 'GDAL-cohort-serial-driver-invocation', 'startedAtUtc': stamp(),
        'config': config_ref, 'cohortManifest': intent.current_ref(), 'driverSource': source_pin(Path(__file__)), 'bodySourceApproval': config['sourceApproval'],
        'pid': os.getpid(), 'uid': os.getuid(), 'actualFamilyOrder': ordered, 'resourcePolicy': policy,
        'nanosecondMetadataTransport': 'Python exact integers; no JavaScript JSON parse/re-emission',
        'capabilityAccepted': False})
    plans = {}
    for fid in ordered:
        scratch = scratch_base / str(fid)
        plan = {'schemaVersion': 1, 'kind': 'GDAL-cohort-family-byte-audit-plan',
                'head': family.HEAD, 'tree': family.TREE, 'base': family.BASE, 'cohortManifest': intent.current_ref(),
                'catalogue': ref3(config['catalogue']), 'materializationMap': ref3(config['materializationMap']),
                'familyId': fid, 'scratchRoot': str(scratch), 'sourceApproval': ref3(config['sourceApproval'])}
        plans[fid] = sealed_json(plans_root / (str(fid) + '.json'), plan)
    progress_path = args.output / 'events.jsonl'
    eventfd = os.open(progress_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    fsync_directory(progress_path.parent)
    completed, failure = [], None
    with os.fdopen(eventfd, 'wb', buffering=0) as events:
        def event(value):
            raw = (json.dumps({'atUtc': stamp(), **value}, separators=(',', ':')) + '\n').encode()
            core.need(len(raw) <= 2 * core.MiB, 'Driver event exceeds bound')
            events.write(raw)
            os.fsync(events.fileno())
        try:
            for fid in ordered:
                sample = observation(args.output, scratch_base, policy['cgroupInactiveFileReserveBytes'])
                admitted(sample, policy, prospective=True)
                event({'event': 'family-admitted', 'familyId': fid, 'completed': len(completed), 'resources': sample})
                scratch = scratch_base / str(fid)
                mkdir_durable(scratch)
                out = outputs_root / str(fid)
                plan_ref = plans[fid]
                log = args.output / (str(fid) + '.stdout.log')
                logfd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                fsync_directory(log.parent)
                with os.fdopen(logfd, 'wb', buffering=0) as logging:
                    child, pidfd = launch_monitored(['python3', '-B', str(body_path), '--plan', plan_ref['path'],
                                                     '--plan-sha256', plan_ref['sha256'], '--cohort-manifest', intent.current_ref()['path'],
                                                     '--cohort-manifest-sha256', intent.current_ref()['sha256'], '--output', str(out)], logging)
                    try:
                        event({'event': 'actual-family-child-started', 'familyId': fid, 'pid': child.pid,
                               'plan': plan_ref, 'source': config['bodySource']})
                        while child.poll() is None:
                            try:
                                current = observation(args.output, scratch_base, policy['cgroupInactiveFileReserveBytes'])
                                admitted(current, policy, prospective=False)
                            except BaseException:
                                terminate_exact(child, pidfd)
                                raise
                            time.sleep(policy['monitorIntervalSeconds'])
                        code = child.wait()
                    finally:
                        if child.poll() is None:
                            terminate_exact(child, pidfd)
                        os.close(pidfd)
                    logging.flush()
                    os.fsync(logging.fileno())
                    os.fchmod(logging.fileno(), 0o400)
                    os.fsync(logging.fileno())
                core.need(code == 0, 'Actual family child failed; preserved its output and scratch')
                fsync_directory(out)
                fsync_directory(out.parent)
                result_ref = source_pin(out / 'result.json')
                result = family.pin_read(result_ref)
                verify_actual_child(result, fid, plan_ref, approval['sources'])
                inventory = source_pin(result['inventory']['path'])
                core.need(inventory == ref3(result['inventory']), 'Actual inventory readback differs')
                core.need(not any(scratch.iterdir()), 'Successful child retained unexpected scratch')
                os.rmdir(scratch)
                fsync_directory(scratch.parent)
                completed.append(result_ref)
                after = observation(args.output, scratch_base, policy['cgroupInactiveFileReserveBytes'])
                admitted(after, policy, prospective=False)
                event({'event': 'family-byte-proof-sealed', 'familyId': fid, 'completed': len(completed),
                       'result': result_ref, 'resources': after, 'capabilityAccepted': False})
        except BaseException as error:
            failure = {'type': type(error).__name__, 'message': str(error)}
            event({'event': 'driver-stopped-fail-closed', 'completed': len(completed), 'failure': failure})
        finally:
            events.flush()
            os.fsync(events.fileno())
            os.fchmod(events.fileno(), 0o400)
            os.fsync(events.fileno())
    final = {'schemaVersion': 1, 'kind': 'GDAL-cohort-family-byte-audit-closeout',
             'completedAtUtc': stamp(), 'invocation': invocation_ref, 'config': config_ref,
             'head': family.HEAD, 'tree': family.TREE, 'base': family.BASE, 'cohortManifest': intent.current_ref(),
             'catalogue': config['catalogue'], 'materializationMap': config['materializationMap'],
             'sourceApproval': config['sourceApproval'], 'familyResults': completed,
             'familiesCompleted': len(completed), 'allFamilyByteAuditsCompleted': len(completed) == len(COHORT['familyNames']) and failure is None,
             'events': source_pin(progress_path), 'failure': failure,
             'capabilityAccepted': False, 'overallGoalComplete': False}
    final_ref = sealed_json(args.output / 'closeout.json', final)
    directory = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    os.fsync(directory)
    os.close(directory)
    fcntl.flock(lockfd, fcntl.LOCK_UN)
    os.close(lockfd)
    print(json.dumps(final_ref))
    if failure is not None:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
