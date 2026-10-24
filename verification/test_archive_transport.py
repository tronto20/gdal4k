"""Exercise actual codec/restore boundaries using small, fixed TAR fixtures."""
import contextlib
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import archive_transport as transport


def tar_bytes(entries, *, mtime=1234567890):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, content, mode, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.mtime = mode, mtime
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = '../escape'
            archive.addfile(member, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


def encode(path, decoded):
    # A fixture is serialized once, then the very same headers, payload and
    # end padding are fed to either real compressor in deliberately odd chunks.
    with path.open('wb') as output:
        writer = transport._Encoder(output, transport._format(path))
        for offset in range(0, len(decoded), 17):
            writer.write(decoded[offset:offset + 17])
        return writer.finish()


class ArchiveTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.downloads = self.base / 'downloads'
        self.downloads.mkdir()
        self.destination = self.base / 'restored'

    def fixture(self, name='sample', codec='gzip', entries=None, decoded=None):
        if decoded is None:
            decoded = tar_bytes(entries or [('engine/payload', b'payload\x00\xff', 0o644, tarfile.REGTYPE)])
        path = self.downloads / (name + transport.SUFFIXES[codec])
        summary = encode(path, decoded)
        return path, summary

    def restore(self, expected=('sample',), **kwargs):
        return transport.restore_archives(self.downloads, self.destination, expected, **kwargs)

    def assert_unrestored(self):
        self.assertFalse(self.destination.exists(), 'Validation must finish before destination writes')

    def test_same_serialized_tar_stream_gzip_xz_full_identity(self):
        decoded = tar_bytes([
            ('engine/a', b'zero\x00\xff', 0o644, tarfile.REGTYPE),
            ('engine/' + 'long-' * 30 + '한글', b'executable', 0o755, tarfile.REGTYPE),
        ])
        identities = []
        for codec in ['gzip', 'xz']:
            with self.subTest(codec=codec):
                path, writer = self.fixture(codec=codec, decoded=decoded)
                result = self.restore()
                reader = result['archives'][0]
                self.assertEqual(writer['decodedTarBytes'], len(decoded))
                self.assertEqual(writer['decodedTarSha256'], hashlib.sha256(decoded).hexdigest())
                self.assertEqual({k: reader[k] for k in writer}, writer)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), reader['archiveSha256'])
                self.assertEqual(b'zero\x00\xff', (self.destination / 'engine/a').read_bytes())
                identities.append((reader['decodedTarBytes'], reader['decodedTarSha256'], reader['members']))
                path.unlink()
        self.assertEqual(identities[0], identities[1])

    def test_real_writer_both_codecs_keep_full_tar_and_input_bytes(self):
        root = self.base / 'producer'
        root.mkdir()
        payload = root / 'data'
        payload.write_bytes(b'actual source\x00\xff')
        os.utime(payload, (1234567890, 1234567890))
        legacy, current = self.base / 'out.tar.gz', self.base / 'out.tar.xz'
        summaries = [transport.write_archive(path, [payload], root) for path in [legacy, current]]
        self.assertEqual(gzip.decompress(legacy.read_bytes()), lzma.decompress(current.read_bytes()))
        self.assertEqual(summaries[0]['decodedTarSha256'], summaries[1]['decodedTarSha256'])
        self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), summaries[0]['members'][0]['sha256'])
        self.assertEqual(b'actual source\x00\xff', payload.read_bytes())

    def test_corrupt_and_truncated_codecs_write_nothing(self):
        for codec in ['gzip', 'xz']:
            for kind in ['corrupt', 'truncated']:
                with self.subTest(codec=codec, kind=kind):
                    path, _ = self.fixture(codec=codec)
                    data = bytearray(path.read_bytes())
                    if kind == 'corrupt':
                        data[len(data) // 2] ^= 0x7f
                    else:
                        del data[-8:]
                    path.write_bytes(data)
                    with self.assertRaises(transport.ArchiveError):
                        self.restore()
                    self.assert_unrestored()
                    path.unlink()

    def test_suffix_magic_mismatch(self):
        path, _ = self.fixture()
        path.rename(self.downloads / 'sample.tar.xz')
        with self.assertRaisesRegex(transport.ArchiveError, 'magic'):
            self.restore()
        self.assert_unrestored()

    def test_exact_archive_selection_rejects_duplicates_unknown_and_missing(self):
        first, _ = self.fixture()
        other, _ = self.fixture(codec='xz')
        with self.assertRaisesRegex(transport.ArchiveError, 'Ambiguous'):
            self.restore()
        other.unlink()
        first.rename(self.downloads / 'unexpected.tar.gz')
        with self.assertRaisesRegex(transport.ArchiveError, 'Unknown'):
            self.restore()
        (self.downloads / 'unexpected.tar.gz').unlink()
        with self.assertRaisesRegex(transport.ArchiveError, 'missing'):
            self.restore()
        self.assert_unrestored()

    def test_unsupported_archive_and_duplicate_expected_names(self):
        (self.downloads / 'sample.tar.zst').write_bytes(b'other codec')
        with self.assertRaises(transport.ArchiveError):
            self.restore()
        (self.downloads / 'sample.tar.zst').unlink()
        with self.assertRaises(transport.ArchiveError):
            self.restore(('sample', 'sample'))
        self.assert_unrestored()

    def test_unsafe_paths_and_nonregular_members(self):
        unsafe = ['.', '../outside', '/absolute', 'C:/drive', 'back\\slash', 'dir/../outside',
                  'folder/trailing.', 'folder/NUL.txt']
        cases = [(name, tarfile.REGTYPE) for name in unsafe]
        cases.extend([('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)])
        for name, kind in cases:
            with self.subTest(name=name, kind=kind):
                path, _ = self.fixture(entries=[(name, b'data', 0o644, kind)])
                with self.assertRaises(transport.ArchiveError):
                    self.restore()
                self.assert_unrestored()
                path.unlink()

    def test_duplicate_and_case_ambiguous_members(self):
        for names in [('same', 'same'), ('folder/Name', 'folder/name')]:
            path, _ = self.fixture(entries=[(name, b'data', 0o644, tarfile.REGTYPE) for name in names])
            with self.assertRaisesRegex(transport.ArchiveError, 'Duplicate'):
                self.restore()
            self.assert_unrestored()
            path.unlink()

    def test_identical_cross_archive_files_allow_different_mtime_and_normalized_mode(self):
        self.fixture('engine', entries=[('maven/shared.jar', b'same bytes', 0o666, tarfile.REGTYPE)])
        self.fixture('complete-maven', 'xz', decoded=tar_bytes([
            ('maven/shared.jar', b'same bytes', 0o644, tarfile.REGTYPE),
            ('tests/test.kexe', b'executable', 0o755, tarfile.REGTYPE),
        ], mtime=1234567999))
        result = self.restore(('engine', 'complete-maven'))
        self.assertEqual(2, result['restoredMembers'])
        self.assertEqual(1, result['identicalDuplicateMembers'])
        member = result['archives'][0]['members'][0]
        self.assertEqual(0o666, member['archiveMode'])
        self.assertEqual(0o644, member['restoredMode'])
        self.assertEqual(b'same bytes', (self.destination / 'maven/shared.jar').read_bytes())
        if os.name != 'nt':
            self.assertEqual(0o755, (self.destination / 'tests/test.kexe').stat().st_mode & 0o777)

    def test_conflicting_cross_archive_bytes_or_permissions_write_nothing(self):
        for second, mode in [(b'different bytes', 0o644), (b'first bytes', 0o755)]:
            a, _ = self.fixture('engine', entries=[('same', b'first bytes', 0o644, tarfile.REGTYPE)])
            b, _ = self.fixture('complete-maven', 'xz', entries=[('same', second, mode, tarfile.REGTYPE)])
            with self.assertRaisesRegex(transport.ArchiveError, 'Conflicting member'):
                self.restore(('engine', 'complete-maven'))
            self.assert_unrestored()
            a.unlink()
            b.unlink()

    def test_file_directory_conflict_is_rejected_before_writes(self):
        self.fixture('engine', entries=[('parent', b'file', 0o644, tarfile.REGTYPE)])
        self.fixture('complete-maven', 'xz', entries=[('parent/child', b'data', 0o644, tarfile.REGTYPE)])
        with self.assertRaisesRegex(transport.ArchiveError, 'file/directory'):
            self.restore(('engine', 'complete-maven'))
        self.assert_unrestored()

    def test_casefold_parent_prefix_conflict_is_rejected_before_writes(self):
        self.fixture('engine', entries=[('Parent', b'file', 0o644, tarfile.REGTYPE)])
        self.fixture('complete-maven', 'xz', entries=[('parent/child', b'data', 0o644, tarfile.REGTYPE)])
        with self.assertRaisesRegex(transport.ArchiveError, 'file/directory'):
            self.restore(('engine', 'complete-maven'))
        self.assert_unrestored()

    def test_restore_cannot_overwrite_an_input_archive(self):
        path, _ = self.fixture(entries=[('would-be-first', b'data', 0o644, tarfile.REGTYPE),
                                        ('downloads/sample.tar.gz', b'overwrite', 0o644, tarfile.REGTYPE)])
        before = path.read_bytes()
        before_stat = transport._stat(path)
        with self.assertRaisesRegex(transport.ArchiveError, 'input archive'):
            transport.restore_archives(self.downloads, self.base, ['sample'])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(before_stat, transport._stat(path))
        self.assertFalse((self.base / 'would-be-first').exists())

    def test_restore_cannot_replace_a_hardlink_to_an_input_archive(self):
        path, _ = self.fixture(entries=[('alias', b'overwrite', 0o644, tarfile.REGTYPE)])
        self.destination.mkdir()
        try:
            os.link(path, self.destination / 'alias')
        except OSError:
            self.skipTest('This filesystem cannot create hardlinks')
        before = transport._stat(path)
        with self.assertRaisesRegex(transport.ArchiveError, 'aliases'):
            self.restore()
        self.assertEqual(before, transport._stat(path))
        self.assertTrue(path.samefile(self.destination / 'alias'))

    def test_existing_symlink_destination_is_not_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        self.destination.mkdir()
        try:
            (self.destination / 'engine').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('This Windows runner cannot create symlinks')
        self.fixture()
        with self.assertRaisesRegex(transport.ArchiveError, 'symlink'):
            self.restore()
        self.assertEqual([], list(outside.iterdir()))

    def test_existing_parent_file_fails_before_any_other_member_is_installed(self):
        self.destination.mkdir()
        (self.destination / 'engine').write_bytes(b'existing parent file')
        self.fixture(entries=[('would-be-first', b'data', 0o644, tarfile.REGTYPE),
                              ('engine/payload', b'data', 0o644, tarfile.REGTYPE)])
        with self.assertRaisesRegex(transport.ArchiveError, 'parent'):
            self.restore()
        self.assertFalse((self.destination / 'would-be-first').exists())
        self.assertEqual(b'existing parent file', (self.destination / 'engine').read_bytes())

    def test_decode_memory_byte_and_member_limits(self):
        path, _ = self.fixture(codec='xz', entries=[('a', b'a', 0o644, tarfile.REGTYPE), ('b', b'b', 0o644, tarfile.REGTYPE)])
        with self.assertRaisesRegex(transport.ArchiveError, 'memory'):
            self.restore(memory_limit=1024 * 1024)
        with self.assertRaisesRegex(transport.ArchiveError, 'byte limit'):
            self.restore(max_decoded_bytes=100)
        with self.assertRaisesRegex(transport.ArchiveError, 'member limit'):
            self.restore(max_members=1)
        self.assert_unrestored()
        self.assertEqual(2, self.restore(memory_limit=128 * 1024**2)['restoredMembers'])

    def test_combined_member_budget_includes_identical_duplicates(self):
        self.fixture('engine')
        self.fixture('complete-maven', 'xz')
        with self.assertRaisesRegex(transport.ArchiveError, 'Combined TAR member'):
            self.restore(('engine', 'complete-maven'), max_members=1)
        self.assert_unrestored()

    def test_oversized_pax_and_gnu_metadata_rejected_before_allocation(self):
        for kind in [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME]:
            header = tarfile.TarInfo('metadata')
            header.type, header.size = kind, transport.MAX_METADATA_BYTES + 1
            path, _ = self.fixture(decoded=header.tobuf(format=tarfile.GNU_FORMAT))
            with self.assertRaisesRegex(transport.ArchiveError, 'metadata header'):
                self.restore()
            self.assert_unrestored()
            path.unlink()

    def test_legacy_and_pax_sparse_files_are_rejected_without_expansion(self):
        legacy = tarfile.TarInfo('sparse')
        legacy.type = tarfile.GNUTYPE_SPARSE
        path, _ = self.fixture(decoded=legacy.tobuf(format=tarfile.GNU_FORMAT))
        with self.assertRaisesRegex(transport.ArchiveError, 'Sparse'):
            self.restore()
        self.assert_unrestored()
        path.unlink()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo('sparse')
            member.size = 1
            member.pax_headers = {'GNU.sparse.map': '0,1', 'GNU.sparse.size': '1000000000000'}
            archive.addfile(member, io.BytesIO(b'x'))
        self.fixture(decoded=output.getvalue())
        with self.assertRaisesRegex(transport.ArchiveError, 'Sparse'):
            self.restore()
        self.assert_unrestored()

    def test_full_tar_padding_is_hashed_and_nonzero_trailing_data_rejected(self):
        decoded = tar_bytes([('file', b'data', 0o644, tarfile.REGTYPE)])
        path, _ = self.fixture(decoded=decoded + b'\x00' * 4096)
        result = self.restore()['archives'][0]
        self.assertEqual(hashlib.sha256(decoded + b'\x00' * 4096).hexdigest(), result['decodedTarSha256'])
        self.assertEqual(len(decoded) + 4096, result['decodedTarBytes'])
        path.unlink()
        self.destination.rename(self.base / 'first-restored')
        self.fixture(decoded=decoded + b'hidden second TAR')
        with self.assertRaisesRegex(transport.ArchiveError, 'Nonzero'):
            self.restore()
        self.assert_unrestored()

    def test_trailing_compressed_stream_is_rejected(self):
        for codec in ['gzip', 'xz']:
            path, _ = self.fixture(codec=codec)
            path.write_bytes(path.read_bytes() + path.read_bytes())
            with self.assertRaisesRegex(transport.ArchiveError, 'Trailing'):
                self.restore()
            self.assert_unrestored()
            path.unlink()

    def test_gzip_works_without_optional_lzma(self):
        root = self.base / 'producer'
        root.mkdir()
        (root / 'data').write_bytes(b'legacy gzip stays usable')
        with patch.dict(sys.modules, {'lzma': None, '_lzma': None}):
            transport.write_archive(self.downloads / 'sample.tar.gz', [root / 'data'], root)
            self.assertEqual(1, self.restore()['restoredMembers'])
            with self.assertRaisesRegex(transport.ArchiveError, 'optional'):
                transport.write_archive(self.base / 'new.tar.xz', [root / 'data'], root)
            with self.assertRaisesRegex(transport.ArchiveError, 'optional'):
                transport.check_codecs()
        self.assertFalse((self.base / 'new.tar.xz').exists())

    def test_writer_failure_preserves_existing_archive_and_removes_candidate(self):
        root = self.base / 'producer'
        root.mkdir()
        outside = self.base / 'outside'
        outside.write_bytes(b'not an owned member')
        output = self.base / 'selected.tar.gz'
        output.write_bytes(b'previous immutable bytes')
        with self.assertRaises(transport.ArchiveError):
            transport.write_archive(output, [outside], root)
        self.assertEqual(b'previous immutable bytes', output.read_bytes())
        self.assertEqual([], list(self.base.glob('.selected.tar.gz-*.tmp')))

    def test_check_cli_uses_native_preset6_with_128mib_and_writes_no_files(self):
        before = set(self.base.iterdir())
        completed = subprocess.run([sys.executable, str(Path(transport.__file__).resolve()), 'check'],
                                   cwd=self.base, check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual('passed', result['result'])
        self.assertEqual(6, result['xzPreset'])
        self.assertEqual(128 * 1024**2, result['xzMemoryLimitBytes'])
        self.assertEqual({'gzip', 'xz'}, {x['format'] for x in result['checks']})
        self.assertEqual(before, set(self.base.iterdir()))


if __name__ == '__main__':
    unittest.main()
