"""Pure staged-file validation tests; synthetic headers are never GDAL engines."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'gdal4k-engine/tools'
sys.path.insert(0, str(TOOLS))
import staged_engine

SPEC = importlib.util.spec_from_file_location('staged_test_producer', TOOLS / 'build-engine.py')
producer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(producer)


def elf(kind, machine=183):
    value = bytearray(64)
    value[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', value, 16, kind, machine, 1)
    struct.pack_into('<H', value, 52, 64)
    return bytes(value)


def archive(*members):
    result = bytearray(b'!<arch>\n')
    for name, value in members:
        result.extend(f'{name:<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(value):<10}`\n'.encode('ascii'))
        result.extend(value)
        if len(value) % 2:
            result.extend(b'\n')
    return bytes(result)


class StagedEngineTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / 'gdal4k-engine'
        self.build = self.binary / 'build'
        self.out = self.build / 'engine/linux-arm64'
        self.version = 'test-combined-SNAPSHOT'
        self.source = self.root / 'gdal4k/native/runtime.cpp'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'current producer input\n')
        for name in ['ports', 'engine', 'tools', 'web']:
            (self.binary / name).mkdir(parents=True)
        self.static = 'engine/linux-arm64/libgdal4k.a'
        self.linker = 'engine/linux-arm64/native-linker-options.json'
        self.jni = 'jvm-resources/gdal4k/linux-arm64/libgdal4k.so'
        self.license = 'engine/linux-arm64/licenses/gdal.txt'
        self.payloads = {self.static: archive(('object.o/', elf(1))), self.linker: b'["-lm", "-ldl"]\n',
                         self.jni: elf(3), self.license: 'Copyright © 한글\n'.encode('utf-8')}
        for name, value in self.payloads.items():
            path = self.build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.manifest = dict(target='linux-arm64', source_fingerprint=self.fingerprint(),
                             vcpkg=producer.VCPKG_REV, gdal='3.12.4', jxl=True,
                             artifacts={n: hashlib.sha256(v).hexdigest() for n, v in self.payloads.items()})
        self.save_manifest()
        self.receipt = self.build / 'verified-staged/linux-arm64.json'
        self.addCleanup(patch.stopall)
        patch.object(staged_engine.platform, 'system', return_value='Linux').start()
        patch.object(staged_engine.platform, 'machine', return_value='aarch64').start()

    def fingerprint(self):
        with patch.object(producer, 'ROOT', self.root), patch.object(producer, 'BINARY', self.binary), \
             patch.object(producer, 'configured_binary_version', return_value=self.version):
            return producer.fingerprint()

    def save_manifest(self):
        (self.out / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def replace_payload(self, name, value):
        (self.build / name).write_bytes(value)
        self.manifest['artifacts'][name] = hashlib.sha256(value).hexdigest()
        self.save_manifest()

    def verify(self, fingerprint=None):
        return staged_engine.verify_staged_engine(self.binary, 'linux-arm64', fingerprint or self.fingerprint,
                                                  producer.VCPKG_REV, self.version)

    def test_valid_current_files_record_exact_receipt_without_execution(self):
        with patch.object(staged_engine, '_digest', wraps=staged_engine._digest) as digest:
            receipt = self.verify()
        self.assertEqual(receipt, json.loads(self.receipt.read_text(encoding='utf-8')))
        self.assertEqual(4, receipt['artifactCount'])
        self.assertEqual('AArch64', receipt['jni']['machine'])
        self.assertEqual(1, receipt['staticArchive']['objectMembers'])
        self.assertFalse(receipt['producerExecuted'])
        self.assertFalse(receipt['toolBootstrapExecuted'])
        self.assertEqual(self.fingerprint(), receipt['sourceFingerprint'])
        self.assertEqual(4, digest.call_count)
        for name, value in self.payloads.items():
            self.assertEqual({'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}, receipt['artifacts'][name])

    def test_canonical_source_and_binary_version_changes_invalidate_stage(self):
        original = self.fingerprint()
        self.source.write_bytes(b'current producer input\r\n')
        self.assertEqual(original, self.fingerprint())  # Same canonical producer newline policy.
        self.source.write_bytes(b'changed producer input\n')
        with self.assertRaisesRegex(ValueError, 'source fingerprint'):
            self.verify()
        self.source.write_bytes(b'current producer input\n')
        self.version = 'different-combined-SNAPSHOT'
        with self.assertRaisesRegex(ValueError, 'source fingerprint'):
            self.verify()
        self.assertFalse(self.receipt.exists())

    def test_tampered_missing_and_wrong_architecture_payloads_fail(self):
        (self.build / self.jni).write_bytes(elf(3) + b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            self.verify()
        (self.build / self.jni).unlink()
        with self.assertRaisesRegex(ValueError, 'Missing regular'):
            self.verify()
        self.replace_payload(self.jni, elf(3, 62))
        with self.assertRaisesRegex(ValueError, 'AArch64 JNI'):
            self.verify()
        self.assertFalse(self.receipt.exists())

    def test_target_pins_required_inventory_and_duplicate_keys_fail(self):
        for field, value in [('target', 'linux-amd64'), ('gdal', '3.13.0'), ('jxl', False), ('vcpkg', '0' * 40)]:
            with self.subTest(field=field):
                before = self.manifest[field]
                self.manifest[field] = value
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'target, pinned versions'):
                    self.verify()
                self.manifest[field] = before
        self.manifest['artifacts'].pop(self.license)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'omits a required'):
            self.verify()
        self.manifest['artifacts'][self.license] = hashlib.sha256(self.payloads[self.license]).hexdigest()
        self.save_manifest()
        path = self.out / 'manifest.json'
        path.write_text(path.read_text()[:-1] + ',"target":"linux-arm64"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate staged JSON key'):
            self.verify()
        self.assertFalse(self.receipt.exists())

    def test_paths_and_license_inventory_cannot_escape_or_hide_files(self):
        for name in ['../outside.txt', '/absolute.txt', 'engine/linux-arm64/../outside.txt',
                     'engine/linux-amd64/licenses/other.txt', 'engine/linux-arm64/licenses//other.txt']:
            with self.subTest(path=name):
                self.manifest['artifacts'][name] = '0' * 64
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'Unsafe staged artifact|Unexpected staged artifact'):
                    self.verify()
                self.manifest['artifacts'].pop(name)
        self.save_manifest()
        extra = self.out / 'licenses/not-in-manifest.txt'
        extra.write_bytes(b'not inventoried')
        with self.assertRaisesRegex(ValueError, 'license directory inventory'):
            self.verify()
        extra.unlink()
        marker = self.root / 'unowned-license'
        marker.write_bytes(self.payloads[self.license])
        license_file = self.build / self.license
        license_file.unlink()
        license_file.symlink_to(marker)
        with self.assertRaisesRegex(ValueError, 'must not use symlinks'):
            self.verify()
        self.assertEqual(self.payloads[self.license], marker.read_bytes())
        self.assertFalse(self.receipt.exists())

    def test_all_static_objects_and_linker_structure_are_checked(self):
        value = archive(('/', b'index'), ('//', b'very-long-object-name.o/\n'), ('/0', elf(1)), ('second.o/', elf(1)))
        self.replace_payload(self.static, value)
        result = self.verify()
        self.assertEqual(2, result['staticArchive']['objectMembers'])
        self.assertEqual(2, result['staticArchive']['metadataMembers'])
        self.receipt.unlink()
        self.replace_payload(self.static, archive(('first.o/', elf(1)), ('second.o/', elf(1, 62))))
        with self.assertRaisesRegex(ValueError, 'AArch64 static object'):
            self.verify()
        self.replace_payload(self.static, value[:-1])
        with self.assertRaisesRegex(ValueError, 'Truncated staged ar member'):
            self.verify()
        self.replace_payload(self.static, value)
        self.replace_payload(self.linker, b'{"unexpected":"-lm"}')
        with self.assertRaisesRegex(ValueError, 'linker option list'):
            self.verify()
        self.assertFalse(self.receipt.exists())

    def test_actual_runtime_host_is_required_before_files_or_receipt(self):
        for system, arch in [('Linux', 'x86_64'), ('Darwin', 'arm64'), ('Windows', 'aarch64')]:
            with self.subTest(system=system, arch=arch), \
                 patch.object(staged_engine.platform, 'system', return_value=system), \
                 patch.object(staged_engine.platform, 'machine', return_value=arch):
                with self.assertRaisesRegex(ValueError, 'Linux ARM64 host'):
                    self.verify()
        with self.assertRaisesRegex(ValueError, 'Linux ARM64 host'):
            staged_engine.verify_staged_engine(self.binary, 'linux-amd64', self.fingerprint, producer.VCPKG_REV, self.version)
        self.assertFalse(self.receipt.exists())

    def test_changed_sources_manifest_or_payload_during_verification_fail(self):
        for change in ['source', 'manifest', 'artifact']:
            with self.subTest(change=change):
                self.source.write_bytes(b'current producer input\n')
                (self.build / self.jni).write_bytes(self.payloads[self.jni])
                self.save_manifest()
                calls = 0
                def fingerprint():
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if change == 'source':
                            self.source.write_bytes(b'changed during verification')
                        elif change == 'manifest':
                            (self.out / 'manifest.json').write_bytes(b'{}')
                        else:
                            (self.build / self.jni).write_bytes(self.payloads[self.jni] + b'changed')
                    return self.fingerprint()
                with self.assertRaisesRegex(ValueError, 'changed during verification'):
                    self.verify(fingerprint)
                self.assertFalse(self.receipt.exists())

    def test_producer_cli_verifies_without_reuse_tools_or_build_fallback(self):
        with patch.object(producer, 'ROOT', self.root), patch.object(producer, 'BINARY', self.binary), \
             patch.object(producer, 'configured_binary_version', return_value=self.version), \
             patch.object(producer, 'build') as build, patch.object(producer, 'reusable') as reuse, \
             patch.object(producer, 'require_private_install_prefix') as install, \
             patch.object(producer, 'native_toolchain') as toolchain, \
             patch.object(producer, 'build_lock') as lock, \
             patch.object(sys, 'argv', ['build-engine.py', 'linux-arm64', '--verify-staged-only']):
            producer.main()
            self.assertTrue(self.receipt.exists())
            self.receipt.unlink()
            (self.build / self.jni).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                producer.main()
            for function in [build, reuse, install, toolchain, lock]:
                function.assert_not_called()
        self.assertFalse(self.receipt.exists())


if __name__ == '__main__':
    unittest.main()
