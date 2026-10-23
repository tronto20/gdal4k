"""Portable-provider validation and lifecycle failures, with no database execution."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('windows_postgis_fixture_unit',
    ROOT/'verification/driver-profile/postgis-windows-fixture.py')
API = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(API)


def digest(data, algorithm='sha256'):
    return hashlib.new(algorithm, data).hexdigest()


def pe(machine=0x8664):
    # Unit-only PE header; never an executable test engine or a provider.
    data = bytearray(512);data[:2]=b'MZ';struct.pack_into('<I',data,60,64)
    data[64:68]=b'PE\0\0';struct.pack_into('<HHIIIHH',data,68,machine,0,0,0,0,112,0)
    struct.pack_into('<H',data,88,0x20b);return bytes(data)


class Inputs:
    def __init__(self, root):
        self.root=root;self.archives={};self.lock={'schemaVersion':1,'target':'windows-amd64',
            'serverVersionNum':170011,'postgisVersion':'3.6.2','binaryRelativePath':'bin',
            'archives':{},'providerFiles':{}}
        for name,files in [('postgres',{f'pgsql/bin/{v}.exe':pe() for v in API.BINARY_NAMES}),
                           ('postgis',{'lib/postgis-3.dll':pe(),'share/extension/postgis.control':b"default_version='3.6.2'\n"}),
                           ('microsoft',{'unit-notice.txt':b'synthetic unit-only runtime notice'})]:
            archive=root/(name+'.zip')
            with zipfile.ZipFile(archive,'w') as out:
                for member,data in files.items():out.writestr(member,data)
            self.archives[name]=archive;data=archive.read_bytes()
            self.lock['archives'][name]={'url':('https://get.enterprisedb.com/postgresql/unit.zip' if name=='postgres'
                else ('https://ftp.postgresql.org/pub/postgis/pg17/v3.6.2/win64/unit.zip' if name=='postgis' else 'https://download.microsoft.com/download/unit.zip')),'bytes':len(data),
                'sha256':digest(data),'sha512':digest(data,'sha512'),'format':'zip','provenance':'SYNTHETIC UNIT INPUT ONLY',
                'inventory':API.inventory_identity(API.archive_inventory(archive))}
            for member,item in API.archive_inventory(archive).items():
                relative=member.removeprefix('pgsql/')
                self.lock['providerFiles'][relative]={'archive':name,'member':member,
                    'bytes':item['bytes'],'sha256':item['sha256'],**({'pe':item['pe']} if 'pe' in item else {})}
        self.lock['dependencyRoots']=['bin/'+n+'.exe' for n in API.BINARY_NAMES]+['lib/postgis-3.dll']
        self.lock['windowsSystemImports']=[]
        self.lock['dependencyClosure']=API.dependency_closure(self.lock['providerFiles'],self.lock['dependencyRoots'],[])


class FixtureTests(unittest.TestCase):
    def test_archive_identity_requires_actual_bytes_and_complete_safe_member_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs=Inputs(Path(temp));API.validate_lock(inputs.lock)
            for name,path in inputs.archives.items():API.verify_archive(path,inputs.lock['archives'][name])
            damaged=copy.deepcopy(inputs.lock);damaged['archives']['postgis']['sha256']=None
            with self.assertRaises((TypeError,ValueError)):API.validate_lock(damaged)
            inputs.archives['postgis'].write_bytes(b'not-the-pinned-input')
            with self.assertRaises(ValueError):API.verify_archive(inputs.archives['postgis'],inputs.lock['archives']['postgis'])

    def test_windows_paths_reject_traversal_drives_reserved_devices_ads_and_case_collisions(self):
        for name in ['../out','/root','C:/out','bin\\x','a//b','a/./b','a/../b','CON','COM1.txt','trailing.','a ','a:stream']:
            with self.subTest(name=name),self.assertRaises(ValueError):API.safe_name(name)
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'bad.zip'
            with zipfile.ZipFile(archive,'w') as out:out.writestr('bin/file',b'a');out.writestr('BIN/FILE',b'b')
            with self.assertRaises(ValueError):API.archive_inventory(archive)

    def test_archive_links_parent_file_shadowing_and_crc_damage_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'link.zip'
            with zipfile.ZipFile(archive,'w') as out:
                entry=zipfile.ZipInfo('link');entry.external_attr=(stat.S_IFLNK|0o777)<<16;out.writestr(entry,'elsewhere')
            with self.assertRaises(ValueError):API.archive_inventory(archive)
            archive=Path(temp)/'shadow.zip'
            with zipfile.ZipFile(archive,'w') as out:out.writestr('bin',b'file');out.writestr('bin/child',b'x')
            with self.assertRaises(ValueError):API.archive_inventory(archive)

    def test_exact_extraction_preserves_originals_and_refuses_inventory_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);inputs=Inputs(root);work=root/'owned';work.mkdir()
            provider=API.extract_provider(inputs.archives,work,inputs.lock)
            self.assertEqual({p.relative_to(provider).as_posix() for p in provider.rglob('*') if p.is_file()},
                             set(inputs.lock['providerFiles']))
            for name,item in inputs.lock['providerFiles'].items():self.assertEqual(API.sha(provider/name),item['sha256'])
            bad=copy.deepcopy(inputs.lock);bad['archives']['postgis']['inventory']['fileCount']+=1
            other=root/'other';other.mkdir()
            with self.assertRaises(ValueError):API.extract_provider(inputs.archives,other,bad)
            self.assertEqual(list((other/'provider').iterdir()),[])

    def test_nsis_listing_rejects_escape_links_shadowing_and_case_collision(self):
        self.assertEqual(API.nsis_names('Path = bin\\file.dll\nAttributes = \n'), ['bin/file.dll'])
        for value in ['Path = ../escape\nAttributes = \n',
                      'Path = bin/file\nSymbolic Link = other\n',
                      'Path = bin\n\nPath = bin/child\n',
                      'Path = bin/file\n\nPath = BIN/FILE\n']:
            with self.subTest(value=value),self.assertRaises(ValueError):API.nsis_names(value)

    def test_non_os_dll_dependency_must_be_exact_private_selected_image(self):
        files={'bin/root.exe':{'pe':{'machine':'AMD64','imports':['msvcp140.dll']}}}
        with self.assertRaises(ValueError):API.dependency_closure(files,['bin/root.exe'],['kernel32.dll'])
        files['bin/msvcp140.dll']={'pe':{'machine':'AMD64','imports':['kernel32.dll']}}
        closed=API.dependency_closure(files,['bin/root.exe'],['kernel32.dll'])
        self.assertEqual(closed['moduleEdges']['bin/root.exe']['msvcp140.dll'],'bin/msvcp140.dll')

    def test_full_nsis_parser_dll_tamper_is_rejected_before_provider_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)/'work';work.mkdir();output=Path(temp)/'output';output.mkdir()
            files={'7z.exe':pe(),'7z.dll':pe(),'License.txt':b'synthetic parser license'}
            tool={'version':'26.05','bootstrap':{'name':'7zr.exe'},'bundle':{'name':'7z2605-x64.exe'},
                  'payloads':{n:dict(bytes=len(d),sha256=digest(d),**({'pe':API.pe_info(d)} if n.endswith(('.exe','.dll')) else {})) for n,d in files.items()}}
            commands=mock.Mock();commands.output=output;commands.receipt={}
            def downloaded(path,entry):path.write_bytes(b'unit archive input')
            def extracted(*args,**kwargs):
                destination=work/'archive-parser/full'
                for name,data in files.items():(destination/name).write_bytes(data)
                (destination/'7z.dll').write_bytes(b'damaged DLL')
            commands.run.side_effect=extracted
            with mock.patch.object(API,'download_locked',side_effect=downloaded),self.assertRaises(ValueError):
                API.parser_tool(work,{'extractor':tool},commands)
            self.assertNotIn('extractor',commands.receipt)
            self.assertEqual(commands.run.call_args.args[1][0],work/'archive-parser/7zr.exe')
            self.assertNotEqual(commands.run.call_args.args[1][0],work/'archive-parser/7z2605-x64.exe')

    def test_provider_core_binaries_cannot_be_replaced_by_overlay_members(self):
        with tempfile.TemporaryDirectory() as temp:
            lock=Inputs(Path(temp)).lock;lock['providerFiles']['bin/postgres.exe']['archive']='postgis'
            with self.assertRaises((KeyError,ValueError)):API.validate_lock(lock)

    def test_pe_architecture_validation_distinguishes_payload_from_32bit_bootstrap(self):
        self.assertEqual(API.pe_info(pe())['machine'],'AMD64')
        with self.assertRaises(ValueError):API.pe_info(pe(0x14c))
        self.assertEqual(API.pe_info(pe(0x14c),require_amd64=False)['machine'],'I386')
        with self.assertRaises(ValueError):API.pe_info(b'not a PE image')

    def test_safe_dsn_and_actual_windows_host_are_required_before_download_or_output(self):
        self.assertEqual(API.safe_dsn('g4k_unit','g4k_unit',54321),'PG:host=127.0.0.1 port=54321 dbname=g4k_unit user=g4k_unit')
        for user,db,port in [('evil password=x','evil',1),('good','different',1),('good','good',True),('good','good',65536)]:
            with self.assertRaises(ValueError):API.safe_dsn(user,db,port)
        with mock.patch.object(API.platform,'system',return_value='Darwin'),mock.patch.object(API,'claim_output') as output:
            with self.assertRaises(ValueError):
                with API.fixture(Path('/unused')):pass
            output.assert_not_called()

    def test_public_output_and_regular_paths_cannot_escape_owned_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);outside=root/'outside';outside.mkdir();inside=root/'verification/results';inside.mkdir(parents=True)
            with mock.patch.object(API,'ROOT',root):
                with self.assertRaises(ValueError):API.claim_output(outside/'output')
                link=inside/'linked';link.symlink_to(outside,target_is_directory=True)
                with self.assertRaises(ValueError):API.claim_output(link/'output')
                with self.assertRaises(ValueError):API.claim_output(inside/'..'/'escape')
            target=outside/'data';target.write_text('x')
            with self.assertRaises(ValueError):API.regular(target,inside)
            with self.assertRaises(ValueError):API.regular(inside/'..'/'outside'/'data',inside)

    def test_server_dll_environment_does_not_leak_into_safe_child_descriptor(self):
        with mock.patch.dict(os.environ,{'SystemRoot':'C:/Windows','PGPASSWORD':'other-secret','PATH':'foreign-provider',
                'GDAL_DATA':'foreign-gdal','PROJ_DATA':'foreign-proj'},clear=True):
            env=API.server_environment(Path('owned/bin'),Path('owned'),Path('work'))
            self.assertNotIn('PGPASSWORD',env);self.assertNotIn('foreign',str(env))
            self.assertTrue(env['PATH'].startswith('owned'))
            self.assertEqual(env['GDAL_DATA'],str(Path('owned/gdal-data')))

    def test_command_failures_retain_sanitized_public_bytes_and_private_raw_echo(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);output=root/'output';work=root/'work';output.mkdir();work.mkdir()
            receipt={'commands':[]};api=mock.Mock();commands=API.Commands(output,work,receipt,api);commands.private_values=['unit-confidential-token-42']
            owned=api.OwnedProcess.return_value
            owned.identity={'pid':42,'creationTime100ns':123,'imagePath':'unit.exe'}
            owned.close.return_value={'activeProcessesAfter':0,'jobClosed':True,'forcedTreeTermination':False}
            owned.process.returncode=1;owned.process.communicate.return_value=(b'unit-confidential-token-42 echoed',b'failure')
            with self.assertRaises(RuntimeError):commands.run('failed',['unit','--pwfile=private'])
            item=receipt['commands'][0];self.assertEqual(item['returncode'],1)
            self.assertNotIn(b'unit-confidential-token-42',(output/item['stdout']).read_bytes())
            self.assertEqual((work/'private-command-output'/Path(item['stdout']).name).read_bytes(),b'unit-confidential-token-42 echoed')
            api.private_file.assert_called()
            owned.process.communicate.side_effect=[subprocess.TimeoutExpired(['unit'],1), (b'partial',b'')]
            with self.assertRaises(RuntimeError):commands.run('timeout',['unit'])
            self.assertTrue(receipt['commands'][-1]['timedOut'])
            owned.terminate_tree.assert_called_once()
            self.assertEqual(owned.close.call_count,2)

    def test_server_start_job_is_retained_until_fixture_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);output=root/'output';work=root/'work';output.mkdir();work.mkdir()
            receipt={'commands':[]};api=mock.Mock();commands=API.Commands(output,work,receipt,api)
            owned=api.OwnedProcess.return_value;owned.identity={'pid':45,'creationTime100ns':123,'imagePath':'pg_ctl.exe'}
            owned.process.returncode=0;owned.process.wait.return_value=0
            owned.close.return_value={'activeProcessesAfter':0,'jobClosed':True,'forcedTreeTermination':False}
            commands.run('pg-ctl-start',['pg_ctl.exe'],retain_tree=True)
            owned.close.assert_not_called();owned.process.communicate.assert_not_called()
            owned.process.wait.assert_called_once_with(timeout=60)
            self.assertEqual(receipt['commands'][0]['outputCapture'],'owned-files-direct-process-wait')
            self.assertNotEqual(api.OwnedProcess.call_args.kwargs['stdout'],subprocess.PIPE)
            self.assertEqual(len(commands.persistent_jobs),1)
            owned.api.active_processes.side_effect=[1,0,0]
            with mock.patch.object(API.time,'sleep'):
                result=commands.close_persistent_trees()
            owned.close.assert_called_once();self.assertTrue(result[0]['gracefulDrainCompleted'])
            self.assertFalse(result[0]['forcedTreeTermination']);self.assertEqual(commands.persistent_jobs,[])

    def test_retained_job_that_never_drains_retains_forced_cleanup_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);output=root/'output';work=root/'work';output.mkdir();work.mkdir()
            commands=API.Commands(output,work,{'commands':[]},mock.Mock())
            owned=mock.Mock();owned.identity={'pid':42};owned.api.active_processes.return_value=1
            owned.close.return_value=dict(activeProcessesAfter=0,jobClosed=True,forcedTreeTermination=True)
            commands.persistent_jobs=[owned]
            with mock.patch.object(API.time,'monotonic',side_effect=[0,11]):
                result=commands.close_persistent_trees()
            self.assertFalse(result[0]['gracefulDrainCompleted']);self.assertTrue(result[0]['forcedTreeTermination'])
            owned.close.assert_called_once()

    def test_owned_tree_cleanup_failure_keeps_actual_output_and_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);output=root/'output';work=root/'work';output.mkdir();work.mkdir()
            receipt={'commands':[]};api=mock.Mock();commands=API.Commands(output,work,receipt,api)
            owned=api.OwnedProcess.return_value;owned.identity={'pid':47,'creationTime100ns':123,'imagePath':'initdb.exe'}
            owned.process.returncode=0;owned.process.communicate.return_value=(b'actual partial stdout',b'actual stderr')
            owned.close.side_effect=RuntimeError('owned job did not drain')
            with self.assertRaises(RuntimeError):commands.run('initdb',['initdb.exe'])
            item=receipt['commands'][0];self.assertTrue(item['failed'])
            self.assertEqual(item['ownedTreeCleanupFailureType'],'RuntimeError')
            self.assertEqual((output/item['stdout']).read_bytes(),b'actual partial stdout')
            self.assertNotIn('ownedTreeCleanup',item)

    def server(self, root):
        work=root/'work';work.mkdir();provider=work/'provider';binary=provider/'bin';binary.mkdir(parents=True)
        receipt={'commands':[]};api=mock.Mock();api.private_directory.return_value={'verified':True};api.private_file.return_value={'verified':True}
        commands=mock.Mock();commands.private_values=[]
        commands.close_persistent_trees.return_value=[]
        with mock.patch.dict(os.environ,{'SystemRoot':'C:/Windows'}):
            server=API.Server(work,provider,{'binaryRelativePath':'bin','serverVersionNum':170011,'postgisVersion':'3.6.2'},commands,receipt,api)
        identity={'pid':42,'creationTime100ns':200000000000000000,'imagePath':str(binary/'postgres.exe')}
        api.process_identity.return_value=identity;api.same_process.return_value=True;api.port_owner.return_value=dict(identity)
        api.wait_process_exit.return_value=True;api.port_closed.return_value=True
        return server,api,commands,receipt,identity

    def test_pid_reuse_or_foreign_image_is_never_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            server,api,commands,receipt,identity=self.server(Path(temp));server.started_after=1;server.identity=identity
            (server.pgdata/'postmaster.pid').write_text(f'42\n{server.pgdata}\n0\n{server.port}\n')
            api.process_identity.return_value=dict(identity,creationTime100ns=identity['creationTime100ns']+1)
            with self.assertRaises(ValueError):server.stop()
            commands.run.assert_not_called()
            api.process_identity.return_value=dict(identity,imagePath='/foreign/postgres.exe')
            with self.assertRaises(ValueError):server.stop()
            commands.run.assert_not_called()

    def test_graceful_exact_owned_stop_requires_process_exit_and_closed_port(self):
        with tempfile.TemporaryDirectory() as temp:
            server,api,commands,receipt,identity=self.server(Path(temp));server.identity=identity;server.started_after=1
            pidfile=server.pgdata/'postmaster.pid';pidfile.write_text(f'42\n{server.pgdata}\n0\n{server.port}\n')
            def stop(*args,**kwargs):pidfile.unlink();return subprocess.CompletedProcess(args,0,b'',b'')
            commands.run.side_effect=stop;server.stop()
            self.assertTrue(receipt['cleanup']['serverStopped']);self.assertFalse(server.passfile.exists())
            argv=commands.run.call_args.args[1];self.assertEqual(argv[0],server.binary/'pg_ctl.exe')
            self.assertEqual(argv[1:3],['-D',server.pgdata]);self.assertEqual(argv[-1],'stop')
            api.wait_process_exit.assert_called_once_with(identity,10)

    def test_startup_or_caller_failure_preserves_original_and_runs_exact_cleanup(self):
        server=mock.Mock();server.start.side_effect=RuntimeError('original failure');server.stop.side_effect=RuntimeError('cleanup failure')
        with self.assertRaisesRegex(RuntimeError,'original failure') as raised:
            with API.server_scope(server):self.fail('must not yield')
        server.stop.assert_called_once();self.assertIn('cleanup failure',''.join(raised.exception.__notes__))
        server=mock.Mock()
        with self.assertRaises(KeyboardInterrupt):
            with API.server_scope(server):raise KeyboardInterrupt()
        server.stop.assert_called_once()


if __name__ == '__main__':unittest.main()
