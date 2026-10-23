"""Synthetic receipt tamper tests; no Windows provider/process is executed."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api);return api


REPORT=load(ROOT/'verification/driver-profile/postgis-windows-report.py','windows_provider_report_subject')
FIXTURE_TEST=load(ROOT/'verification/test_postgis_windows_fixture.py','windows_provider_report_unit_inputs')


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class Scenario:
    def __init__(self,root):
        self.root,self.snapshot=root/'source',root/'snapshot'
        self.folder=self.snapshot/'verification/results/native-postgis-profile'
        self.folder.mkdir(parents=True);(self.folder/'commands').mkdir();(self.folder/'binaries').mkdir()
        inputs=FIXTURE_TEST.Inputs(root);self.lock=inputs.lock
        for tree in [self.root,self.snapshot]:
            for name in REPORT.NAMES:
                path=tree/name;path.parent.mkdir(parents=True,exist_ok=True)
                if name.endswith('-lock.json'):path.write_text(json.dumps(self.lock,indent=2)+'\n')
                else:shutil.copyfile(ROOT/name,path)
        work=root/'work';work.mkdir();provider=FIXTURE_TEST.API.extract_provider(inputs.archives,work,self.lock)
        binary=provider/'bin';data=work/'pgdata';data.mkdir();private=work/'private';private.mkdir()
        inventory={name:{key:item[key] for key in ['bytes','sha256']} for name,item in self.lock['providerFiles'].items()}
        (self.folder/'provider-files.json').write_text(json.dumps(inventory))
        binaries={}
        for name in FIXTURE_TEST.API.BINARY_NAMES:
            source=binary/(name+'.exe');public=self.folder/'binaries'/(name+'.exe');public.write_bytes(source.read_bytes())
            binaries[name]=dict(path=str(source),publicPath='binaries/'+name+'.exe',bytes=source.stat().st_size,
                                sha256=sha(source),**FIXTURE_TEST.API.pe_info(source.read_bytes()))
        identity={'pid':42,'creationTime100ns':200000000000000000,'imagePath':str(binary/'postgres.exe')}
        self.server={'dataDirectory':str(data),'port':54321,'listenAddresses':['127.0.0.1'],
            'postmasterPid':42,'processIdentity':identity,'listener':dict(identity,localAddress='127.0.0.1',localPort=54321,state=2),
            'serverVersionNum':170011,'serverVersion':'17.11','postgisVersion':'3.6.2','encoding':'UTF8','collation':'C',
            'version':'synthetic unit only','postgisFullVersion':'synthetic unit only'}
        acl={}
        for name in ['work','pgdata','private','initdbPassword','pgpass']:
            flags=3 if name in ['work','pgdata','private'] else 0
            acl[name]={'ownerSid':'S-1-5-21-1000','currentUserSid':'S-1-5-21-1000','daclProtected':True,
                'entries':[{'sid':sid,'type':0,'mask':0x1f01ff,'flags':flags} for sid in ['S-1-5-21-1000','S-1-5-18']]}
        self.receipt={'schemaVersion':1,'target':'windows-amd64','result':'passed','sourceUnchanged':True,'providerUnchanged':True,
            'completedAt':'2026-10-23T00:01:00Z','sourceSha256':{name:sha(self.snapshot/name) for name in REPORT.NAMES},
            'archiveIdentitySha256':FIXTURE_TEST.API.archive_identity(self.lock),
            'inputInventoryVerified':{name:v['inventory'] for name,v in self.lock['archives'].items()},
            'archives':{name:{key:value[key] for key in ['url','bytes','sha256','sha512','provenance','md5','format','inventory'] if key in value}
                        for name,value in self.lock['archives'].items()},
            'provider':{'root':str(provider),'binaryDirectory':str(binary),'inventoryPath':'provider-files.json',
                        'inventorySha256':sha(self.folder/'provider-files.json'),'binaries':binaries},
            'privateAcl':acl,'connection':{'dsn':'PG:host=127.0.0.1 port=54321 dbname=g4k_unit user=g4k_unit','passwordIncluded':False},
            'server':self.server,'startup':dict(completed=True,healthy=True,dataDirectoryVerified=True,postgisExtensionVerified=True),
            'roundTrip':{'table':'availability_'+'a'*16,'values':{'value':42,'x':3,'y':4,'srid':4326,'count':1},
                         'newConnectionVerified':True,'tableDeletedAndAbsentNewConnection':True},
            'cleanup':dict(serverStopped=True,ownedPostmasterExited=True,portClosed=True,pgCtlStopReturncode=0,privateCredentialFilesRemoved=True,
                           ownedCommandTreesClosed=True,forcedOwnedTreeTermination=False),
            'commands':[]}
        (self.snapshot.parent/'inputs.json').write_text(json.dumps({'profile':'combined','target':'windows-amd64',
            'postgresqlFixture':dict(target='windows-amd64',dsn=self.receipt['connection']['dsn'],
                                     archiveSha256=FIXTURE_TEST.API.archive_identity(self.lock))}))
        def cmd(label,argv,stdout=b''):
            index=len(self.receipt['commands']);item=dict(label=label,argv=[str(v) for v in argv],cwd=str(work),
                returncode=0,timeoutSeconds=90,startedAt='2026-10-23T00:00:00Z',completedAt='2026-10-23T00:00:01Z')
            item['windowsProcess']={'pid':100+index,'creationTime100ns':123+index,'imagePath':str(argv[0])}
            if label=='pg-ctl-start':
                item['ownedTreeRetainedForFixtureLifetime']=True
                item['outputCapture']='owned-files-direct-process-wait'
                self.receipt['cleanup']['retainedCommandTrees']=[dict(windowsProcess=item['windowsProcess'],
                    jobClosed=True,activeProcessesAfter=0,forcedTreeTermination=False,
                    gracefulDrainCompleted=True,gracefulDrainTimeoutSeconds=10)]
            else:item['ownedTreeCleanup']=dict(jobClosed=True,activeProcessesAfter=0,forcedTreeTermination=False)
            for stream,value in [('stdout',stdout),('stderr',b'')]:
                name=f'commands/{index:03d}-{label}.{stream}';path=self.folder/name;path.write_bytes(value)
                item[stream]=name;item[stream+'Sha256']=sha(path)
            self.receipt['commands'].append(item)
        cmd('initdb',[binary/'initdb.exe','-D',data,'--username=g4k_unit','--encoding=UTF8','--locale=C',
            '--auth-local=scram-sha-256','--auth-host=scram-sha-256','--pwfile='+str(private/'initdb-password')])
        cmd('pg-ctl-start',[binary/'pg_ctl.exe','-D',data,'-l',work/'server.log','-w','-t','60','start'])
        table=self.receipt['roundTrip']['table']
        observed={key:value for key,value in self.server.items() if key in ['dataDirectory','serverVersion','serverVersionNum',
            'postgisVersion','encoding','collation','version','postgisFullVersion']}
        observed.update(port='54321',listenAddresses='127.0.0.1')
        sqls=[('create-owned-database','CREATE DATABASE g4k_unit OWNER g4k_unit;',b'CREATE DATABASE'),
              ('create-postgis-extension','CREATE EXTENSION postgis;',b'CREATE EXTENSION'),
              ('server-identity','unit server identity query',json.dumps(observed).encode()),
              ('owned-table-absent',f"SELECT to_regclass('public.{table}') IS NULL;",b't'),
              ('point-write-commit',f'BEGIN; CREATE TABLE {table}(value integer NOT NULL,geom geometry(Point,4326)); INSERT INTO {table} VALUES(42,ST_SetSRID(ST_Point(3,4),4326)); COMMIT;',b'COMMIT'),
              ('point-new-connection-read','unit point query',json.dumps(self.receipt['roundTrip']['values']).encode()),
              ('drop-exact-owned-table',f'DROP TABLE IF EXISTS {table};',b'DROP TABLE'),
              ('owned-table-absent-after',f"SELECT to_regclass('public.{table}') IS NULL;",b't')]
        for label,statement,stdout in sqls:
            cmd(label,[binary/'psql.exe','-X','-h','127.0.0.1','-p','54321','-U','g4k_unit','-d',
                       'postgres' if label=='create-owned-database' else 'g4k_unit','-v','ON_ERROR_STOP=1','-At','-c',statement],stdout)
        cmd('pg-ctl-stop',[binary/'pg_ctl.exe','-D',data,'-m','fast','-w','-t','60','stop'])
        self.save()

    def save(self):
        (self.folder/'fixture.json').write_text(json.dumps(self.receipt,indent=2)+'\n')

    def run(self):
        self.save()
        with mock.patch.object(REPORT,'ROOT',self.root):return REPORT.bindings(self.snapshot,'windows-amd64')

    def command(self,label):return next(c for c in self.receipt['commands'] if c['label']==label)


class ReportTests(unittest.TestCase):
    def test_complete_identity_lifecycle_and_actual_four_byte_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Scenario(Path(temp));result=s.run()
            self.assertEqual(set(result['actualFourProviderBinarySha256']),set(FIXTURE_TEST.API.BINARY_NAMES))
            self.assertTrue(result['cleanup']['ownedPostmasterExited']);self.assertEqual(result['commandCount'],11)

    def test_complete_input_inventory_cannot_be_replaced_by_selected_member_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Scenario(Path(temp));s.receipt['inputInventoryVerified']['postgis']['fileCount']+=1
            with self.assertRaises(AssertionError):s.run()

    def test_archive_or_current_source_tamper_is_not_a_valid_provider(self):
        for kind in ['archive','source']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp))
                if kind=='archive':s.receipt['archives']['postgis']['sha256']='a'*64
                else:(s.snapshot/REPORT.NAMES[0]).write_text('# changed provider code')
                with self.assertRaises(AssertionError):s.run()

    def test_provider_file_and_retained_actual_pe_bytes_cannot_be_changed(self):
        for kind in ['provider','public-binary']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp));entry=s.receipt['provider']['binaries']['postgres']
                path=Path(entry['path']) if kind=='provider' else s.folder/entry['publicPath']
                path.write_bytes(b'tampered executable')
                with self.assertRaises(AssertionError):s.run()

    def test_wildcard_listener_pid_reuse_and_unprotected_acl_are_rejected(self):
        for kind in ['listener','identity','acl']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp))
                if kind=='listener':s.receipt['server']['listener']['localAddress']='0.0.0.0'
                elif kind=='identity':s.receipt['server']['listener']['creationTime100ns']+=1
                else:s.receipt['privateAcl']['pgpass']['daclProtected']=False
                with self.assertRaises((AssertionError,ValueError)):s.run()

    def test_incomplete_cleanup_or_foreign_stop_command_cannot_pass(self):
        for kind in ['cleanup','foreign-stop','command-failure']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp))
                if kind=='cleanup':s.receipt['cleanup']['portClosed']=False
                elif kind=='foreign-stop':s.command('pg-ctl-stop')['argv'][2]='/foreign/data'
                else:s.command('pg-ctl-stop')['returncode']=1
                with self.assertRaises(AssertionError):s.run()

    def test_rehashed_raw_sql_tamper_cannot_replace_actual_new_connection_values(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Scenario(Path(temp));command=s.command('point-new-connection-read');path=s.folder/command['stdout']
            path.write_text('{"value":41}');command['stdoutSha256']=sha(path)
            with self.assertRaises(AssertionError):s.run()

    def test_unbound_private_or_extra_public_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Scenario(Path(temp));(s.folder/'pgpass').write_text('not allowed in public artifacts')
            with self.assertRaises(AssertionError):s.run()

    def test_intended_profile_endpoint_and_aggregate_input_identity_must_match(self):
        for key,value in [('dsn','PG:host=127.0.0.1 port=54322 dbname=g4k_unit user=g4k_unit'),
                          ('archiveSha256','b'*64),('target','linux-arm64')]:
            with self.subTest(key=key),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp));path=s.snapshot.parent/'inputs.json';data=json.loads(path.read_text())
                data['postgresqlFixture'][key]=value;path.write_text(json.dumps(data))
                with self.assertRaises(AssertionError):s.run()

    def test_postgresql_windows_path_spelling_is_canonical_but_other_cluster_is_rejected(self):
        for changed in [False,True]:
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as temp:
                s=Scenario(Path(temp));command=s.command('server-identity');path=s.folder/command['stdout']
                observed=json.loads(path.read_text());observed['dataDirectory']=observed['dataDirectory'].replace('/','\\').upper()
                if changed:observed['dataDirectory']+='-foreign'
                path.write_text(json.dumps(observed));command['stdoutSha256']=sha(path)
                if changed:
                    with self.assertRaises(AssertionError):s.run()
                else:s.run()


if __name__=='__main__':unittest.main()
