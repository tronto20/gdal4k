"""COFF/SDK boundary checks; actual Windows scheduling is a separate CI probe."""
import struct
import unittest

import winpthread_handoff_probe as probe


def object_bytes(extra=None, defined=False):
    names = b'\0\0\0\0' + probe.OLD_SYMBOL + b'\0'
    symbols = [struct.pack('<IIIhHBB', 0, 4, 0, 1 if defined else 0, 0x20, 2, 0)]
    if extra is not None:
        symbols.append(struct.pack('<IIIhHBB', 0, extra, 0, 0, 0, 2, 0))
    count = len(symbols)
    names = struct.pack('<I', len(names)) + names[4:]
    text = b'\xe8\0\0\0\0'
    relocation = struct.pack('<IIH', 1, 0, 4)
    raw_at, reloc_at, symbols_at = 60, 65, 75
    header = struct.pack('<HHIIIHH', 0x8664, 1, 0, symbols_at, count, 0, 0)
    section = struct.pack('<8sIIIIIIHHI', b'.text\0\0\0', 0, 0, len(text), raw_at,
                          reloc_at, 0, 1, 0, 0x60000020)
    return header + section + text + relocation + b''.join(symbols) + names


class WinpthreadHandoffProbeTest(unittest.TestCase):
    def test_only_undefined_name_changes_and_instruction_relocation_bytes_stay_exact(self):
        original = object_bytes()
        changed, receipt = probe.rewrite_unlock(original)
        at, size = receipt['changedStringOffset'], receipt['changedStringBytes']
        self.assertEqual(original[:at], changed[:at])
        self.assertEqual(original[at + size:], changed[at + size:])
        self.assertEqual(probe.NEW_SYMBOL, changed[at:at + size].rstrip(b'\0'))
        self.assertEqual(original[60:75], changed[60:75])
        self.assertEqual(1, receipt['hookRelocations'])
        self.assertTrue(receipt['instructionsAndDataAndRelocationsUnchanged'])

    def test_defined_duplicate_and_suffix_aliased_symbols_are_refused(self):
        for data in [object_bytes(defined=True), object_bytes(extra=4), object_bytes(extra=5)]:
            with self.subTest(sha=probe.sha(data)), self.assertRaises(ValueError):
                probe.rewrite_unlock(data)

    def test_malformed_tables_and_out_of_bounds_relocations_are_refused(self):
        original = object_bytes()
        bad_reloc = bytearray(original)
        struct.pack_into('<I', bad_reloc, 65, 5)
        for data in [original[:-1], b'\0' * len(original), bytes(bad_reloc)]:
            with self.subTest(sha=probe.sha(data)), self.assertRaises(ValueError):
                probe.rewrite_unlock(data)

    def test_gnu_long_archive_member_and_exact_sdk_pin_are_checked(self):
        def member(name, value):
            header = (name.ljust(16) + '0'.ljust(12) + '0'.ljust(6) + '0'.ljust(6)
                      + '100644'.ljust(8) + str(len(value)).ljust(10) + '`\n').encode('ascii')
            return header + value + (b'\n' if len(value) & 1 else b'')
        archive = b'!<arch>\n' + member('//', (probe.MEMBER + '/\n').encode('ascii')) + member('/0', object_bytes())
        self.assertEqual(object_bytes(), probe.ar_member(archive, probe.MEMBER))
        with self.assertRaisesRegex(ValueError, 'pinned archive SHA256'):
            probe.pinned_object(archive)
        with self.assertRaises(ValueError):
            probe.ar_member(archive[:-1], probe.MEMBER)

    def test_control_receipt_rejects_wrong_queue_window_and_untyped_claims(self):
        marker = dict(mode='shared', result='expected-stall-observed-and-cancelled', pid=12,
                      producerTid=13, consumerTid=14, consumerUnlocks=3, queueBeforeSecondWait=1,
                      queueBeforeThirdWait=0, observedBlockedQueueCount=-2, oldStallObserved=True,
                      hookError=False, threadsJoined=True, rowsObservedAfterJoin=True, rows=[3])
        record = dict(pid=12, status='passed', outputComplete=True, returncode=0,
                      cleanup=dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False))
        probe.validate_control('shared', marker, record)
        for changes in [dict(queueBeforeSecondWait=0), dict(oldStallObserved=False),
                        dict(threadsJoined='false'), dict(consumerTid=13), dict(rows=[3, 1])]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                probe.validate_control('shared', dict(marker, **changes), record)


if __name__ == '__main__':
    unittest.main()
