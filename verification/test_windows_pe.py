"""Pure synthetic PE tests; no compiler, Windows image or provider is executed."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_pe_test_api', ROOT/'verification/windows_pe.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def pe_image(regular=('kernel32.dll',), delay=(), *, machine=0x8664, delay_va=False):
    """A minimal byte fixture containing actual normal and delay descriptors."""
    data = bytearray(2048)
    data[:2] = b'MZ'; struct.pack_into('<I', data, 60, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, machine, 1, 0, 0, 0, 240, 0)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b)
    image_base = 0x400000 if delay_va else 0x140000000
    struct.pack_into('<Q', data, optional+24, image_base)
    struct.pack_into('<I', data, optional+108, 16)
    struct.pack_into('<IIII', data, optional+240+8, 1536, 0x1000, 1536, 512)
    cursor = 0x1200
    for names, index, width, base in [(regular, 1, 20, 0x1000), (delay, 13, 32, 0x1100)]:
        if not names: continue
        struct.pack_into('<II', data, optional+112+index*8, base, width*(len(names)+1))
        for number, name in enumerate(names):
            raw = name.encode('ascii') + b'\0'
            offset = cursor-0x1000+512; data[offset:offset+len(raw)] = raw
            descriptor = base-0x1000+512+number*width
            if index == 1: struct.pack_into('<I', data, descriptor+12, cursor)
            else: struct.pack_into('<II', data, descriptor, 0 if delay_va else 1, cursor+image_base if delay_va else cursor)
            cursor += len(raw)
    return bytes(data)


class WindowsPeTest(unittest.TestCase):
    def test_regular_and_delay_imports_use_actual_separate_tables(self):
        data = module.inspect(pe_image(['KERNEL32.dll'], ['api-ms-win-crt-runtime-l1-1-0.dll']))
        self.assertEqual(['kernel32.dll'], data['imports']['regular'])
        self.assertEqual(['api-ms-win-crt-runtime-l1-1-0.dll'], data['imports']['delay'])
        self.assertEqual(64, data['directories']['delay']['descriptorBytes'])
        self.assertEqual(data['imports']['all'], module.validate_system_dependencies(data['imports']['all']))
        va = module.inspect(pe_image(delay=['ws2_32.dll'], delay_va=True))
        self.assertEqual(['ws2_32.dll'], va['imports']['delay'])

    def test_dynamic_compiler_runtime_and_delay_libraries_are_rejected(self):
        for name in ['libwinpthread-1.dll', 'libstdc++-6.dll', 'libgcc_s_seh-1.dll', 'libpq.dll']:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unbundled dependencies'):
                module.validate_system_dependencies(module.inspect(pe_image(delay=[name]))['imports']['all'])
        with self.assertRaisesRegex(ValueError, 'Could not inspect'):
            module.validate_system_dependencies([])

    def test_wrong_machine_truncated_and_outside_section_references_reject(self):
        for value in [b'', pe_image(machine=0x14c), pe_image()[:550]]:
            with self.subTest(length=len(value)), self.assertRaises(ValueError): module.inspect(value)
        invalid = bytearray(pe_image()); struct.pack_into('<I', invalid, 512+12, 0x9000)
        with self.assertRaisesRegex(ValueError, 'section bytes'): module.inspect(invalid)
        invalid = bytearray(pe_image()); struct.pack_into('<II', invalid, 0x98+112+8, 0x1000, 20)
        with self.assertRaisesRegex(ValueError, 'Unterminated'): module.inspect(invalid)

    def test_actual_byte_receipt_binds_source_hash_and_rejects_preserved_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'verification/windows_pe.py'; source.parent.mkdir()
            source.write_bytes((ROOT/'verification/windows_pe.py').read_bytes())
            program = root/'owned probe.exe'; program.write_bytes(pe_image(delay=['ws2_32.dll']))
            output = root/'verification/results/probe-pe.json'
            with patch.object(module, '__file__', str(source)):
                receipt = module.audit(program, root=root, output=output)
                self.assertEqual('passed', receipt['result'])
                self.assertEqual(hashlib.sha256(program.read_bytes()).hexdigest(), receipt['executableSha256'])
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), receipt['inspectorSourceSha256'])
                self.assertEqual(receipt, json.loads(output.read_text(encoding='utf-8')))
                with self.assertRaisesRegex(ValueError, 'Preserve existing'):
                    module.audit(program, root=root, output=output)

    def test_failed_import_audit_retains_receipt_without_executing_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'verification/windows_pe.py'; source.parent.mkdir()
            source.write_bytes((ROOT/'verification/windows_pe.py').read_bytes())
            program = root/'owned probe.exe'; program.write_bytes(pe_image(delay=['libwinpthread-1.dll']))
            output = root/'verification/results/probe-pe.json'
            with patch.object(module, '__file__', str(source)), self.assertRaisesRegex(ValueError, 'Unbundled'):
                module.audit(program, root=root, output=output)
            receipt = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual('failed', receipt['result'])
            self.assertEqual(['libwinpthread-1.dll'], receipt['pe']['imports']['delay'])

    def test_owned_boundary_rejects_parent_traversal_and_windows_reparse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError): module._owned(root, root/'..'/'escape.exe')
            with self.assertRaises(ValueError): module._owned(root/'..', root/'probe.exe')
            with patch.object(Path, 'is_junction', return_value=True):
                with self.assertRaisesRegex(ValueError, 'reparse'): module._owned(root, root/'probe.exe')


if __name__ == '__main__':
    unittest.main()
