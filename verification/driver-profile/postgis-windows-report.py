"""Verify the exact portable Windows provider and actual owned lifecycle evidence."""
import hashlib
import importlib.util
import json
import ntpath
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
NAMES = ['verification/driver-profile/postgis-windows-fixture.py',
         'verification/driver-profile/postgis-windows-fixture-lock.json',
         'verification/windows_process.py']


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(root, name, module):
    spec = importlib.util.spec_from_file_location(module, root/name)
    api = importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    return api


def public_file(folder, name):
    if not isinstance(name,str) or not name or '\\' in name or Path(name).is_absolute() or Path(name).as_posix()!=name:
        raise ValueError('Noncanonical public provider evidence path')
    path = folder/Path(name)
    if '..' in Path(name).parts or path.is_symlink() or not path.is_file():
        raise ValueError('Missing/unsafe public provider evidence')
    for parent in path.relative_to(folder).parents:
        if (folder/parent).is_symlink():
            raise ValueError('Public provider evidence traverses a symlink')
    return path


def bindings(snapshot, target):
    if target != 'windows-amd64':
        raise ValueError('Windows provider report target mismatch')
    folder = snapshot/'verification/results/native-postgis-profile'
    receipt_path = public_file(folder,'fixture.json')
    value = json.loads(receipt_path.read_text(encoding='utf-8'))
    assert (value['schemaVersion'],value['target'],value['result'])==(1,target,'passed')
    assert value['sourceUnchanged'] is True and value['providerUnchanged'] is True and value['completedAt']
    assert set(value['sourceSha256'])==set(NAMES)
    for name,digest in value['sourceSha256'].items():
        assert sha(ROOT/name)==sha(snapshot/name)==digest,'Windows provider source/lock changed'
    manager = load(snapshot,NAMES[0],'windows_provider_report_manager')
    process = load(snapshot,NAMES[2],'windows_provider_report_process')
    lock = manager.validate_lock(json.loads((snapshot/NAMES[1]).read_text(encoding='utf-8')))
    assert value['archiveIdentitySha256']==manager.archive_identity(lock)
    expected_archives = {name:{key:entry[key] for key in ['url','bytes','sha256','sha512','provenance','md5','format','inventory'] if key in entry}
                         for name,entry in lock['archives'].items()}
    assert value['archives']==expected_archives,'Portable archives differ from actual-byte lock'
    assert value['inputInventoryVerified']=={name:entry['inventory'] for name,entry in lock['archives'].items()}, 'Actual full input inventory was not verified'
    parser_files=set()
    if lock.get('extractor'):
        tool=lock['extractor']; actual=value['extractor']
        assert actual['version']==tool['version']
        assert actual['sourceInputs']=={name:tool[name] for name in ('bootstrap','bundle')}
        expected_payloads={**tool['payloads'],tool['bootstrap']['name']:tool['bootstrap']}
        assert set(actual['payloads'])==set(expected_payloads)
        for name, entry in actual['payloads'].items():
            expected=expected_payloads[name]; assert entry['publicPath']=='parser/'+name
            path=public_file(folder,entry['publicPath'])
            assert path.stat().st_size==entry['bytes']==expected['bytes'] and sha(path)==entry['sha256']==expected['sha256']
            assert manager.regular(Path(entry['path']),Path(entry['path']).parent).stat().st_size==entry['bytes']
            assert sha(Path(entry['path']))==entry['sha256']
            parser_files.add(entry['publicPath'])
    provider=value['provider'];inventory_path=public_file(folder,provider['inventoryPath'])
    assert provider['inventoryPath']=='provider-files.json' and sha(inventory_path)==provider['inventorySha256']
    inventory=json.loads(inventory_path.read_text(encoding='utf-8'))
    expected={name:{key:item[key] for key in ['bytes','sha256']} for name,item in lock['providerFiles'].items()}
    assert inventory==expected,'Final provider file bytes differ from original locked members'
    actual_provider=Path(provider['root'])
    for name,item in expected.items():
        path=manager.regular(actual_provider/name,actual_provider)
        assert path.stat().st_size==item['bytes'] and sha(path)==item['sha256'],'Owned provider changed after runtime'
    assert {p.relative_to(actual_provider).as_posix() for p in actual_provider.rglob('*') if p.is_file()}==set(expected)
    binaries=provider['binaries'];assert set(binaries)==set(manager.BINARY_NAMES)
    actual_binary_hashes={}
    for name,entry in binaries.items():
        relative=lock['binaryRelativePath']+'/'+name+'.exe'
        source=lock['providerFiles'][relative]
        assert source['archive']=='postgres' and entry['bytes']==source['bytes'] and entry['sha256']==source['sha256']
        assert entry['publicPath']=='binaries/'+name+'.exe'
        actual=public_file(folder,entry['publicPath']);data=actual.read_bytes()
        assert len(data)==entry['bytes'] and sha(actual)==entry['sha256']
        parsed=manager.pe_info(data)
        assert all(entry[key]==parsed[key] for key in parsed)
        assert Path(entry['path'])==Path(provider['binaryDirectory'])/(name+'.exe')
        assert Path(provider['binaryDirectory'])==Path(provider['root'])/lock['binaryRelativePath']
        actual_binary_hashes[name]=sha(actual)
    acl=value['privateAcl']
    assert set(acl)=={'work','pgdata','private','initdbPassword','pgpass'}
    for name,item in acl.items():process.validate_private_acl(item,name in {'work','pgdata','private'})
    connection=value['connection'];assert connection['passwordIncluded'] is False
    match=re.fullmatch(r'PG:host=127\.0\.0\.1 port=([1-9][0-9]{0,4}) dbname=([a-z][a-z0-9_]{0,62}) user=\2',connection['dsn'])
    assert match and int(match[1])<65536
    intended=json.loads((snapshot.parent/'inputs.json').read_text(encoding='utf-8'))
    assert intended['profile']=='combined' and intended['target']==target
    assert intended['postgresqlFixture']==dict(target=target,dsn=connection['dsn'],
                                              archiveSha256=manager.archive_identity(lock)), 'Owned provider differs from intended profile fixture'
    server=value['server'];identity=server['processIdentity'];listener=server['listener']
    assert server['port']==int(match[1]) and server['listenAddresses']==['127.0.0.1']
    assert type(identity['pid']) is int and identity['pid']>0 and identity['pid']==server['postmasterPid']
    assert type(identity['creationTime100ns']) is int and identity['creationTime100ns']>0
    assert Path(identity['imagePath'])==Path(provider['binaryDirectory'])/'postgres.exe'
    assert listener['localAddress']=='127.0.0.1' and listener['localPort']==server['port']
    assert all(listener[key]==identity[key] for key in ['pid','creationTime100ns','imagePath'])
    assert server['serverVersionNum']==lock['serverVersionNum'] and server['postgisVersion']==lock['postgisVersion']
    assert server['encoding']=='UTF8' and server['collation']=='C'
    assert all(value['startup'][key] is True for key in ['completed','healthy','dataDirectoryVerified','postgisExtensionVerified'])
    rt=value['roundTrip'];assert re.fullmatch('availability_[0-9a-f]{16}',rt['table'])
    assert rt['values']=={'value':42,'x':3,'y':4,'srid':4326,'count':1}
    assert rt['newConnectionVerified'] is True and rt['tableDeletedAndAbsentNewConnection'] is True
    cleanup=value['cleanup']
    assert all(cleanup[key] is True for key in ['serverStopped','ownedPostmasterExited','portClosed','privateCredentialFilesRemoved'])
    assert cleanup['ownedCommandTreesClosed'] is True and cleanup['forcedOwnedTreeTermination'] is False
    assert type(cleanup['pgCtlStopReturncode']) is int and cleanup['pgCtlStopReturncode']==0
    commands=value['commands'];logs={};labels={}
    assert commands
    for command in commands:
        assert command['returncode']==0 and type(command['returncode']) is int
        assert not command.get('timedOut') and not command.get('executionFailureType')
        assert command['startedAt'] and command['completedAt'] and command['timeoutSeconds']>0
        assert command['cwd'] and command['argv'] and all(isinstance(v,str) for v in command['argv'])
        observed=command['windowsProcess']
        assert type(observed['pid']) is int and observed['pid']>0
        assert type(observed['creationTime100ns']) is int and observed['creationTime100ns']>0
        assert Path(observed['imagePath'])==Path(command['argv'][0]), 'Provider command image mismatch'
        if command['label']=='pg-ctl-start':
            assert command['ownedTreeRetainedForFixtureLifetime'] is True
            assert command['outputCapture']=='owned-files-direct-process-wait'
        else:
            tree=command['ownedTreeCleanup']
            assert tree['jobClosed'] is True and tree['activeProcessesAfter']==0 and tree['forcedTreeTermination'] is False
        assert not re.search(r'(?i)(?:PGPASSWORD|password)\s*=(?!\[fixture-secret-redacted\])',' '.join(command['argv']))
        labels.setdefault(command['label'],[]).append(command)
        for stream in ['stdout','stderr']:
            name=command[stream];assert name.startswith('commands/') and name not in logs
            path=public_file(folder,name);assert sha(path)==command[stream+'Sha256']
            logs[name]=sha(path)
    if lock.get('extractor'):
        assert all(len(labels.get(name,[]))==1 for name in ('extract-full-nsis-parser','list-nsis-input','extract-nsis-data'))
    required=['initdb','pg-ctl-start','create-owned-database','create-postgis-extension','server-identity',
              'owned-table-absent','point-write-commit','point-new-connection-read','drop-exact-owned-table',
              'owned-table-absent-after','pg-ctl-stop']
    assert all(len(labels.get(label,[]))==1 for label in required),'Missing/duplicate actual lifecycle command'
    retained=cleanup['retainedCommandTrees']
    assert len(retained)==1 and retained[0]['windowsProcess']==labels['pg-ctl-start'][0]['windowsProcess']
    assert retained[0]['gracefulDrainCompleted'] is True and retained[0]['gracefulDrainTimeoutSeconds']==10
    assert retained[0]['jobClosed'] is True and retained[0]['activeProcessesAfter']==0 and retained[0]['forcedTreeTermination'] is False
    def command(label):return labels[label][0]
    def stdout(label):return public_file(folder,command(label)['stdout']).read_text(encoding='utf-8').strip()
    data=server['dataDirectory'];binary=Path(provider['binaryDirectory'])
    assert command('pg-ctl-stop')['argv']==[str(binary/'pg_ctl.exe'),'-D',data,'-m','fast','-w','-t','60','stop']
    assert command('pg-ctl-start')['argv']==[str(binary/'pg_ctl.exe'),'-D',data,'-l',str(Path(command('pg-ctl-start')['cwd'])/'server.log'),'-w','-t','60','start']
    init=command('initdb')['argv'];assert init[0]==str(binary/'initdb.exe') and init[1:3]==['-D',data]
    for option in ['--encoding=UTF8','--locale=C','--auth-local=scram-sha-256','--auth-host=scram-sha-256']:
        assert option in init
    assert sum(v.startswith('--pwfile=') for v in init)==1 and not any(v.startswith('--password=') for v in init)
    for label in required[2:-1]:
        argv=command(label)['argv'];assert argv[:5]==[str(binary/'psql.exe'),'-X','-h','127.0.0.1','-p']
        assert argv[5]==str(server['port']) and argv[6:8]==['-U',match[2]] and argv[-4:-1]==['ON_ERROR_STOP=1','-At','-c']
    assert stdout('owned-table-absent')==stdout('owned-table-absent-after')=='t'
    assert json.loads(stdout('point-new-connection-read'))==rt['values']
    actual_identity=json.loads(stdout('server-identity'))
    assert ntpath.normcase(ntpath.normpath(actual_identity['dataDirectory']))==ntpath.normcase(ntpath.normpath(data))
    assert actual_identity['port']==str(server['port'])
    assert actual_identity['listenAddresses']=='127.0.0.1'
    assert all(str(server[key])==str(item) for key,item in actual_identity.items() if key not in ['dataDirectory','port','listenAddresses'])
    table=rt['table'];assert command('owned-table-absent')['argv'][-1]==f"SELECT to_regclass('public.{table}') IS NULL;"
    assert command('owned-table-absent-after')['argv'][-1]==command('owned-table-absent')['argv'][-1]
    assert command('drop-exact-owned-table')['argv'][-1]==f'DROP TABLE IF EXISTS {table};'
    assert f'CREATE TABLE {table}(' in command('point-write-commit')['argv'][-1]
    assert 'COMMIT;' in command('point-write-commit')['argv'][-1]
    expected_files={'fixture.json','provider-files.json'}|parser_files|set(logs)|{v['publicPath'] for v in binaries.values()}
    if value.get('serverLogSha256'):
        assert sha(public_file(folder,'server.log'))==value['serverLogSha256'];expected_files.add('server.log')
    assert {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}==expected_files
    return dict(fixtureSha256=sha(receipt_path),sourceSha256=value['sourceSha256'],archiveIdentitySha256=value['archiveIdentitySha256'],
                archives=value['archives'],providerInventorySha256=sha(inventory_path),actualFourProviderBinarySha256=actual_binary_hashes,
                connection=connection,server=server,startup=value['startup'],roundTrip=rt,cleanup=cleanup,
                privateAcl=acl,commandLogSha256=logs,commandCount=len(commands),
                scope='Actual owned portable Windows PostgreSQL/PostGIS, exact locked official input bytes and provider files, '
                      'four retained actual PEAMD64 provider binaries, verified private DACLs, real new-connection Point42 readback '
                      'and terminal exact-process/port cleanup. First-observed strong input hashes are not publisher signatures; '
                      'provider DLLs, PGDATA and private credentials are outside the published GDAL engine.')
