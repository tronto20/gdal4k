"""Transport safety, immutable fixture configuration and actual Native stdout evidence."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('arm_profile_test', ROOT/'verification/driver-profile/arm64-profile.py')
arm = importlib.util.module_from_spec(spec); spec.loader.exec_module(arm)
from test_driver_profiles import checkout


def native_stdout():
    names = ['dev.gdal4k.DriverProfileApiTest.requiredDriversAndRealDataRoundTrip']
    names += ['dev.gdal4k.NativeHttpProfileTest.case' + str(i) for i in range(10)]
    names += ['dev.gdal4k.DatasetApiTest.pixelRoundTrip']
    lines = [f'[==========] Running {len(names)} tests from 3 test cases.']
    for name in names:
        lines += ['[ RUN      ] ' + name, '[       OK ] ' + name + ' (1 ms)']
    lines += [f'[==========] {len(names)} tests from 3 test cases ran. (12 ms total)',
              f'[  PASSED  ] {len(names)} tests.']
    return '\n'.join(lines) + '\n'


def configuration():
    return dict(schemaVersion=1, profile='combined', target='linux-arm64', runId='arm64_'+'a'*32,
                postgresqlDsn='PG:host=127.0.0.1 port=16432 dbname=owned user=owned',
                httpUrls=dict(http='http://127.0.0.1:16001', https='https://127.0.0.1:16002',
                              untrusted='https://127.0.0.1:16003', **{'wrong-name':'https://127.0.0.1:16004'}),
                hdf5Path='/owned/fixture.h5')


class NativeEvidenceTest(unittest.TestCase):
    def test_actual_cases_require_complete_unique_ordered_zero_skip_results(self):
        raw = native_stdout()
        self.assertEqual(12, arm.native_cases(raw)['tests'])
        bad = [raw.replace('[  PASSED  ] 12', '[  PASSED  ] 11'),
               raw.replace('[ RUN      ] dev.gdal4k.DatasetApiTest.pixelRoundTrip\n', ''),
               raw + raw, raw + '[  SKIPPED ] one\n', raw + 'Uncaught Kotlin exception\n',
               raw.replace('[       OK ] dev.gdal4k.DatasetApiTest.pixelRoundTrip', '[   FAILED ] dev.gdal4k.DatasetApiTest.pixelRoundTrip'),
               raw.replace('case9', 'case8'), raw.replace('requiredDriversAndRealDataRoundTrip', 'unrelated')]
        for value in bad:
            with self.subTest(value=value[-100:]), self.assertRaises(ValueError):
                arm.native_cases(value)

    def test_one_public_configuration_marker_with_exact_endpoint_and_context(self):
        config = configuration()
        marker = 'GDAL4K_TEST_DRIVER_FIXTURE ' + json.dumps(dict(configuration=config, contextKey='gdal4k-deadbeef'))
        self.assertEqual(config, arm.fixture_markers(marker+'\n', config)['configuration'])
        changed = dict(config, hdf5Path='/different/fixture.h5')
        for raw, expected in [(marker+'\n'+marker+'\n', config), ('', config), (marker, changed),
                              (marker.replace('gdal4k-deadbeef', '../unexpected'), config)]:
            with self.assertRaises(ValueError):
                arm.fixture_markers(raw, expected)

    def test_phase_host_guard_rejects_compilation_host_as_runtime(self):
        with patch.object(arm.platform, 'system', return_value='Linux'), patch.object(arm.platform, 'machine', return_value='x86_64'):
            arm.host('x86_64')
            with self.assertRaises(ValueError): arm.host('aarch64')


class TransferTest(unittest.TestCase):
    def prepare_transfer(self, root):
        checkout(root)
        source_tool = root/'gdal4k-engine/tools/driver-profile.py'
        source_tool.parent.mkdir(parents=True)
        source_tool.write_bytes((ROOT/'gdal4k-engine/tools/driver-profile.py').read_bytes())
        subprocess.run(['git', 'add', '--', 'gdal4k-engine/tools/driver-profile.py'], cwd=root, check=True)
        subprocess.run(['git', '-c', 'user.email=unit@example.invalid', '-c', 'user.name=Unit', 'commit', '-qm', 'unit fixture'], cwd=root, check=True)
        api = arm.load('transfer_unit_prepare', 'gdal4k-engine/tools/driver-profile.py', root)
        snapshot, _ = api.prepare(root, 'combined', 'linux-arm64', runtime_fixtures=True)
        # These tiny bytes exercise the transport format only. They are never run
        # and never represented as GDAL producer or runtime validation.
        binary = bytearray(64); binary[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HHI', binary, 16, 2, 183, 1)
        for tree in [root, snapshot]:
            build = tree/'gdal4k-engine/build'
            paths = {'engine/linux-arm64/libgdal4k.a': b'unit transport payload only'}
            for name, data in paths.items():
                path = build/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            arm.save(build/'engine/linux-arm64/manifest.json', dict(target='linux-arm64', gdal='3.12.4', jxl=True,
                     artifacts={name:hashlib.sha256(data).hexdigest() for name,data in paths.items()}))
            for name in ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json'):
                arm.save(build / 'engine/linux-arm64' / name,
                         dict(unitTransportOnly=True, context=tree.relative_to(root).as_posix(), name=name))
        files = {'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe': bytes(binary),
                 'verification/results/published-consumer-linuxArm64/bin/linuxArm64/debugTest/test.kexe': bytes(binary),
                 'verification/build/consumer-maven/dev/tronto/gdal4k/unit/0/unit.pom': b'<unit/>',
                 'verification/build/arrow-ipc-regression/arrow-ipc-check': bytes(binary),
                 'verification/results/arrow-ipc-build-linux-arm64.json': b'{"unit":true}',
                 'gdal4k-engine/build/engine/linux-arm64/cmake/gdal4k_http_probe': bytes(binary)}
        for name, data in files.items():
            path=snapshot/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
            if name.endswith('test.kexe'): path.chmod(0o755)
        arm.save(snapshot/'verification/results/published-consumer-linuxArm64/result.json',
                 dict(result='passed',compiled_only=True,producer_paths_hidden_count=3))
        transfer = root/'verification/build/unit-transfer.tar.gz'
        with patch.object(arm.platform,'system',return_value='Linux'), patch.object(arm.platform,'machine',return_value='x86_64'):
            manifest=arm.create_transfer(root,snapshot,transfer)
        return transfer, manifest

    def clone_checkout(self, source, destination):
        subprocess.run(['git','clone','-q','--no-hardlinks',str(source),str(destination)],check=True)

    def rewrite(self, original, output, mutate):
        with tarfile.open(original,'r:gz') as archive:
            members=[(member,archive.extractfile(member).read()) for member in archive.getmembers()]
        mutate(members)
        with tarfile.open(output,'w:gz') as archive:
            for member,data in members:
                member.size=len(data); archive.addfile(member,io.BytesIO(data))

    def test_exact_transfer_relocation_preserves_modes_sources_and_compile_only_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'producer';root.mkdir()
            archive,manifest=self.prepare_transfer(root)
            runtime=Path(temporary)/'runtime';self.clone_checkout(root,runtime)
            snapshot,restored=arm.restore_transfer(runtime,archive)
            self.assertEqual(manifest,restored)
            binary=runtime/restored['consumerBinary']
            self.assertEqual(0o755,binary.stat().st_mode&0o777)
            self.assertTrue(json.loads((snapshot/'verification/results/published-consumer-linuxArm64/result.json').read_text())['compiled_only'])
            self.assertFalse((snapshot/'gdal4k-engine/build/engine/linux-arm64/installed').exists())
            for tree in [runtime, snapshot]:
                engine = tree / 'gdal4k-engine/build/engine/linux-arm64'
                payload = json.loads((engine / 'manifest.json').read_text())
                self.assertEqual(['engine/linux-arm64/libgdal4k.a'], list(payload['artifacts']))
                for name in ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json'):
                    self.assertTrue(json.loads((engine / name).read_text())['unitTransportOnly'])
                    self.assertIn((engine / name).relative_to(runtime).as_posix(), restored['files'])
            self.assertEqual(archive.stat().st_size,json.loads(archive.with_suffix('.gz.json').read_text())['bytes'])
            with self.assertRaisesRegex(ValueError,'existing destination'):
                arm.restore_transfer(runtime,archive)

    def test_missing_or_linked_sdk_metadata_rejected_by_engine_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = root / 'gdal4k-engine/build/engine/linux-arm64'
            engine.mkdir(parents=True)
            arm.save(engine / 'manifest.json', dict(target='linux-arm64', gdal='3.12.4', jxl=True, artifacts={}))
            names = ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json')
            for name in names: (engine / name).write_text('{}')
            for name in names:
                metadata = engine / name
                metadata.unlink()
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'SDK input metadata'):
                    arm.engine_files(root)
                try: metadata.symlink_to(engine / 'manifest.json')
                except OSError: self.skipTest('File symlinks are unavailable')
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    arm.engine_files(root)
                metadata.unlink(); metadata.write_text('{}')

    def test_hash_tamper_and_symlink_member_rejected_before_any_output_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'producer';root.mkdir()
            archive,_=self.prepare_transfer(root)
            for kind in ['hash','link','duplicate','traversal']:
                runtime=Path(temporary)/kind;self.clone_checkout(root,runtime)
                broken=Path(temporary)/(kind+'.tar.gz')
                def mutate(members):
                    if kind=='hash': members[-1]=(members[-1][0],b'changed')
                    elif kind=='link': members[-1][0].type=tarfile.SYMTYPE;members[-1][0].linkname='/outside'
                    elif kind=='duplicate': members.append(copy.deepcopy(members[-1]))
                    else: members[-1][0].name='../outside'
                self.rewrite(archive,broken,mutate)
                with self.subTest(kind=kind),self.assertRaises(ValueError):arm.restore_transfer(runtime,broken)
                self.assertFalse((runtime/'build').exists())

    def test_current_git_or_uncommitted_source_change_rejects_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'producer';root.mkdir()
            archive,_=self.prepare_transfer(root)
            runtime=Path(temporary)/'runtime';self.clone_checkout(root,runtime)
            (runtime/'gradle.properties').write_text('gdal4kVersion=other\n')
            with self.assertRaisesRegex(ValueError,'source differs'):
                arm.restore_transfer(runtime,archive)


if __name__=='__main__': unittest.main()
