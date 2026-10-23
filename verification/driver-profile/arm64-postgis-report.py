"""Bind the owned ARM PostGIS lifecycle to pinned inputs and actual public logs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
NAMES = ['verification/driver-profile/postgis-arm64-fixture.py',
         'verification/driver-profile/postgis-arm64-fixture-lock.json',
         'verification/driver-profile/postgis-arm64.Dockerfile']


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def log_path(folder, name):
    relative = Path(name)
    assert (not relative.is_absolute() and relative.parts and relative.parts[0] == 'commands'
            and '..' not in relative.parts and relative.as_posix() == name)
    path = folder / relative
    assert path.is_file() and not any(folder.joinpath(*relative.parts[:i]).is_symlink()
                                      for i in range(1, len(relative.parts)+1))
    return path


def bindings(snapshot, target):
    assert target == 'linux-arm64'
    folder = snapshot / 'verification/results/native-postgis-profile'
    receipt_path = folder / 'fixture.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    assert (receipt['schemaVersion'], receipt['target'], receipt['result']) == (1, target, 'passed')
    assert receipt['sourceUnchanged'] is True and receipt['completedAt']
    assert set(receipt['sourceSha256']) == set(NAMES)
    for name in NAMES:
        assert receipt['sourceSha256'][name] == sha(ROOT/name) == sha(snapshot/name), 'ARM provider source changed'
    lock_path = snapshot / NAMES[1]
    lock = json.loads(lock_path.read_text())
    spec = importlib.util.spec_from_file_location('arm_postgis_lock_validator', snapshot/NAMES[0])
    manager = importlib.util.module_from_spec(spec); spec.loader.exec_module(manager)
    expected_packages = manager.validate_lock(lock)
    assert receipt['providerLockSha256'] == sha(lock_path)
    provider = receipt['provider']
    assert provider['baseImage'] == lock['baseImage'] and provider['baseConfig'] == lock['baseConfigDigest']
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', provider['imageId'])
    assert provider['dockerServer']['Os'] == 'linux' and provider['dockerServer']['Arch'] in ['arm64', 'aarch64']
    inventory_sha = hashlib.sha256(expected_packages.encode()).hexdigest()
    assert provider['expectedPackageInventorySha256'] == provider['actualPackageInventorySha256'] == inventory_sha
    assert provider['packageCount'] == len(expected_packages.splitlines())
    assert len(provider['packages']) == len(lock['packages'])
    for actual, expected in zip(provider['packages'], lock['packages']):
        assert {key: actual[key] for key in expected} == expected
        assert Path(actual['retainedPath']).is_absolute() and Path(actual['retainedPath']).name == expected['sha256']+'.deb'
    connection = receipt['connection']
    assert connection['passwordIncluded'] is False
    match = re.fullmatch(r'PG:host=127\.0\.0\.1 port=([1-9][0-9]{0,4}) dbname=([a-z][a-z0-9_]{0,62}) user=\2', connection['dsn'])
    assert match and int(match[1]) < 65536
    assert connection['hostLoopbackMapping'] == [dict(HostIp='127.0.0.1', HostPort=match[1])]
    config = json.loads((snapshot/'verification/results/arm64-runtime-configuration.json').read_text())
    assert config['postgresqlDsn'] == connection['dsn'] and config['target'] == target
    server = receipt['server']
    assert server['serverVersionNum'] == lock['serverVersionNum'] and server['postgisVersion'] == lock['postgisVersion']
    assert server['dataDirectory'] == '/var/lib/postgresql/data' and server['encoding'] == 'UTF8' and server['collation'] == 'C'
    assert server['listenAddresses'] == '*' and server['containerListenAddresses'] == ['*']
    assert type(server['postmasterPid']) is int and server['postmasterPid'] > 1
    assert all(receipt['startup'][key] is True for key in
               ['completed', 'healthy', 'dataDirectoryVerified', 'postgisExtensionVerified'])
    round_trip = receipt['roundTrip']
    assert re.fullmatch(r'availability_[0-9a-f]{16}', round_trip['table'])
    assert round_trip['values'] == dict(value=42, x=3, y=4, srid=4326, count=1)
    assert round_trip['newConnectionVerified'] is True and round_trip['tableDeletedAndAbsentNewConnection'] is True
    cleanup = receipt['cleanup']
    assert all(cleanup[key] is True for key in ['serverStopped', 'ownedPostmasterExited', 'containerExited',
               'containerRemoved', 'volumeRemoved', 'portClosed', 'ownedProviderStopped', 'privateCredentialFilesRemoved'])
    assert cleanup['pgCtlStopReturncode'] == cleanup['containerExitCode'] == 0 and not cleanup.get('failures')
    assert re.fullmatch(r'[0-9a-f]{64}', receipt['containerId'])
    assert re.fullmatch(r'gdal4k-pg-arm64-[0-9a-f]{24}', receipt['volumeName'])
    commands, logs, by_label = receipt['commands'], {}, {}
    assert commands
    for command in commands:
        label = command['label']
        by_label.setdefault(label, []).append(command)
        assert command['argv'][0] == 'docker' and all(isinstance(v,str) for v in command['argv'])
        assert Path(command['cwd']).is_absolute() and command['startedAt'] and command['completedAt']
        assert not command.get('timedOut') and not command.get('executionFailureType')
        code = command['returncode']
        assert type(code) is int
        if label == 'wait-owned-postgres': assert code in [0,1,2]
        elif label == 'owned-postmaster-exited': assert code != 0
        else: assert code == 0, ('Unexpected failed provider command',label)
        for stream in ['stdout','stderr']:
            path = log_path(folder, command[stream])
            assert sha(path) == command[stream+'Sha256'], 'ARM provider public log changed'
            assert command[stream] not in logs, 'Duplicate ARM provider log'
            logs[command[stream]] = sha(path)
        assert not re.search(r'(?i)(?:PGPASSWORD|password)\s*=(?!\[redacted\])', ' '.join(command['argv']))
    required = ['docker-server','inspect-official-base','build-offline-locked-provider','inspect-built-provider',
                'create-owned-container','create-owned-volume','launch-owned-postgres','actual-server-identity',
                'point-write-commit','point-new-connection-read','drop-exact-owned-table','owned-table-absent-after',
                'actual-package-inventory','graceful-owned-postgres-stop','owned-postmaster-exited',
                'remove-owned-container','confirm-owned-container-absent','remove-owned-volume','confirm-owned-volume-absent']
    assert all(label in by_label for label in required), 'Missing actual owned provider lifecycle commands'
    def stdout(label):
        assert len(by_label[label]) == 1
        return log_path(folder, by_label[label][0]['stdout']).read_bytes()
    assert json.loads(stdout('docker-server')) == provider['dockerServer'], 'Actual Docker server identity differs'
    base = json.loads(stdout('inspect-official-base'))
    assert len(base) == 1 and base[0]['Id'] == lock['baseConfigDigest']
    assert base[0]['Os'] == 'linux' and base[0]['Architecture'] == 'arm64'
    built = json.loads(stdout('inspect-built-provider'))
    assert len(built) == 1 and built[0]['Id'] == provider['imageId']
    assert built[0]['Os'] == 'linux' and built[0]['Architecture'] == 'arm64'
    token = receipt['volumeName'].removeprefix('gdal4k-pg-arm64-')
    assert built[0]['Config']['Labels'][manager.OWNER] == token
    assert stdout('create-owned-container').decode().strip() == receipt['containerId']
    assert stdout('create-owned-volume').decode().strip() == receipt['volumeName']
    assert by_label['remove-owned-container'][0]['argv'] == ['docker','rm',receipt['containerId']]
    assert by_label['remove-owned-volume'][0]['argv'] == ['docker','volume','rm',receipt['volumeName']]
    assert by_label['confirm-owned-container-absent'][0]['argv'] == [
        'docker','ps','--all','--no-trunc','--filter','id='+receipt['containerId'],'--format','{{.ID}}']
    for command in by_label.get('inspect-owned-container', []):
        assert command['argv'] == ['docker','inspect',receipt['containerId']]
        values = json.loads(log_path(folder, command['stdout']).read_bytes())
        assert len(values) == 1
        manager.owned_container(values[0], receipt['containerId'], provider['imageId'], token, receipt['volumeName'])
    assert by_label.get('inspect-owned-container'), 'Missing actual container ownership observations'
    terminal = json.loads(log_path(folder, by_label['inspect-owned-container'][-1]['stdout']).read_bytes())[0]
    assert terminal['State']['Running'] is False and terminal['State']['ExitCode'] == cleanup['containerExitCode'] == 0
    for command in by_label.get('inspect-owned-volume', []):
        assert command['argv'] == ['docker','volume','inspect',receipt['volumeName']]
        values = json.loads(log_path(folder, command['stdout']).read_bytes())
        assert len(values) == 1
        manager.owned_volume(values[0], receipt['volumeName'], token)
    assert by_label.get('inspect-owned-volume'), 'Missing actual volume ownership observations'
    pid = server['postmasterPid']
    assert by_label.get('owned-postmaster-pid') and by_label.get('owned-postmaster-command')
    for command in by_label['owned-postmaster-pid']:
        assert command['argv'] == ['docker','exec','--user','postgres',receipt['containerId'],
                                   '/bin/cat','/var/lib/postgresql/data/postmaster.pid']
        lines = log_path(folder, command['stdout']).read_text().splitlines()
        assert len(lines) >= 4 and lines[0] == str(pid) and lines[1] == server['dataDirectory'] and lines[3] == '5432'
    for command in by_label['owned-postmaster-command']:
        assert command['argv'] == ['docker','exec','--user','postgres',receipt['containerId'],'/bin/cat','/proc/'+str(pid)+'/cmdline']
        argv = log_path(folder, command['stdout']).read_bytes().split(b'\0')
        assert argv and Path(argv[0].decode()).name == 'postgres'
        if b'-D' in argv:
            index = argv.index(b'-D')+1
            assert index < len(argv) and argv[index] == server['dataDirectory'].encode()
    assert by_label['owned-postmaster-exited'][0]['argv'] == [
        'docker','exec','--user','postgres',receipt['containerId'],'/bin/sh','-c','kill -0 '+str(pid)]
    assert by_label['graceful-owned-postgres-stop'][0]['argv'] == [
        'docker','exec','--user','postgres',receipt['containerId'],'/usr/lib/postgresql/17/bin/pg_ctl',
        '-D',server['dataDirectory'],'-m','fast','-w','-t','30','stop']
    assert hashlib.sha256(stdout('actual-package-inventory')).hexdigest() == inventory_sha
    assert stdout('owned-table-absent-after').strip() == b't'
    assert not stdout('confirm-owned-container-absent').strip()
    assert receipt['volumeName'] not in stdout('confirm-owned-volume-absent').decode().splitlines()
    assert json.loads(stdout('point-new-connection-read')) == round_trip['values']
    identity = json.loads(stdout('actual-server-identity'))
    assert all(server[key] == value for key,value in identity.items())
    binaries = provider['binaries']
    assert set(binaries) == set(lock['providerBinaryInputs'])
    for name, expected in lock['providerBinaryInputs'].items():
        actual = binaries[name]
        assert {key:actual[key] for key in ['bytes','sha256']} == expected
        assert actual['architecture'] == 'AArch64' and actual['eMachine'] == 183
        assert actual['path'] == '/'+lock['providerBinaryPaths'][name]
        data = stdout('actual-binary-'+name)
        assert len(data) == expected['bytes'] and hashlib.sha256(data).hexdigest() == expected['sha256']
        assert data[:6] == b'\x7fELF\x02\x01' and int.from_bytes(data[18:20],'little') == 183
    actual_files = {path.relative_to(folder).as_posix() for path in folder.rglob('*') if path.is_file()}
    assert actual_files == {'fixture.json'} | logs.keys(), 'Unexpected private or unbound provider evidence'
    return dict(fixtureSha256=sha(receipt_path), providerLockSha256=sha(lock_path), sourceSha256=receipt['sourceSha256'],
                provider=provider, connection=connection, server=server, startup=receipt['startup'], cleanup=cleanup,
                roundTrip=round_trip, commandLogSha256=logs, commandCount=len(commands),
                scope='Actual owned Linux ARM64 PostGIS server with locked offline provider, full installed inventory, '
                      'actual four ELF provider binaries, new-connection POINT readback and terminal owned-resource cleanup. '
                      'The host loopback mapping and container listen addresses are distinct; credentials and PGDATA are excluded.')
