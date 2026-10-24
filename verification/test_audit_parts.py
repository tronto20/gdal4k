"""Actual file/ZIP transport tests, never real GDAL/runtime acceptance evidence."""
from contextlib import ExitStack
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('audit_parts_unit', ROOT / 'verification/package-audit-parts.py')
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)
REAL_DOWNLOAD_LIMIT, REAL_PART_BYTES, REAL_MAX_ARCHIVE_BYTES = API.DOWNLOAD_LIMIT, API.PART_BYTES, API.MAX_ARCHIVE_BYTES


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Audit Unit', '-c', 'user.email=audit-unit@example.invalid',
                                    '-C', str(root), *args], text=True, stderr=subprocess.PIPE).strip()


def parents(root):
    raw = git(root, 'cat-file', '-p', 'HEAD')
    return [line[7:] for line in raw.split('\n\n', 1)[0].splitlines() if line.startswith('parent ')]


class AuditPartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        git(self.workspace, 'init', '--initial-branch=proof')
        git(self.workspace, 'commit', '--allow-empty', '-m', 'synthetic audit fixture')
        self.checkout = git(self.workspace, 'rev-parse', 'HEAD')
        self.metadata = {'schemaVersion': 1, 'headSha': self.checkout, 'checkoutSha': self.checkout,
                         'checkoutParents': [],
                         'source': {'artifactName': 'synthetic-package', 'artifactId': 123, 'artifactDigest': 'a' * 64},
                         'run': {'id': '456', 'attempt': '1', 'job': 'synthetic', 'repository': 'unit/fixture', 'githubSha': self.checkout},
                         'allowlist': ['verification/build/staged/*.tar.gz', 'verification/results/public/']}
        self.names = ['verification/build/staged/consumer-maven.tar.gz',
                      'verification/results/public/passfile-receipt.json',
                      'verification/results/public/credential-acl.json']
        self.write(self.names[0], os.urandom(14000))
        self.write(self.names[1], b'{"privateCredentialFilesRemoved":true}\n')
        self.write(self.names[2], b'{"privateAcl":"verified metadata, no secret value"}\n')
        self.output = self.base / 'parts'
        self.restored = self.base / 'restored.zip'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.multiple(API, DOWNLOAD_LIMIT=4096, PART_BYTES=3072, MAX_ARCHIVE_BYTES=307200))

    def write(self, relative, data):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def collect(self, **kwargs):
        return API.collect(self.workspace, kwargs.get('names', self.names), self.output, kwargs.get('metadata', self.metadata))

    def update_manifest(self, callback):
        path = self.output / 'manifest.json'
        value = json.loads(path.read_text())
        callback(value)
        path.write_text(json.dumps(value))
        return API.digest(path)

    def rejected(self, expected_sha=None, exception=ValueError):
        with self.assertRaises(exception):
            API.reassemble(self.output, self.restored, expected_sha or API.digest(self.output / 'manifest.json'))
        self.assertFalse(self.restored.exists())
        self.assertEqual(list(self.base.glob('gdal4k-audit-*')), [])

    def test_real_default_bounds_preserve_actual_tar_gz_member_bytes_and_zip_crc(self):
        with mock.patch.multiple(API, DOWNLOAD_LIMIT=REAL_DOWNLOAD_LIMIT, PART_BYTES=REAL_PART_BYTES,
                                 MAX_ARCHIVE_BYTES=REAL_MAX_ARCHIVE_BYTES):
            original = self.workspace / self.names[0]
            payload = os.urandom(REAL_DOWNLOAD_LIMIT + 12345)
            with tarfile.open(original, 'w:gz') as archive:
                info = tarfile.TarInfo('maven/unit/fixture/1.0/synthetic-data.bin')
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            original_hash = API.digest(original)
            result = self.collect()
            self.assertTrue(result['exported'])
            self.assertEqual(len(result['parts']), 2)
            self.assertLessEqual(max(item['bytes'] for item in result['parts']), REAL_DOWNLOAD_LIMIT - 65536)
            receipt = API.reassemble(self.output, self.restored, result['manifestSha256'])
            self.assertTrue(receipt['zipCrcAndMemberBytesVerified'])
            self.assertFalse(receipt['originalArtifactZipBytesPreserved'])
            with zipfile.ZipFile(self.restored) as archive:
                self.assertIsNone(archive.testzip())
                restored_tar = archive.read(self.names[0])
                self.assertEqual(hashlib.sha256(restored_tar).hexdigest(), original_hash)
                with tarfile.open(fileobj=io.BytesIO(restored_tar), mode='r:gz') as tar:
                    self.assertEqual(tar.extractfile('maven/unit/fixture/1.0/synthetic-data.bin').read(), payload)
            cli_zip = self.base / 'cli-restored.zip'
            cli = subprocess.run([sys.executable, API.__file__, 'reassemble', '--directory', str(self.output),
                                  '--output', str(cli_zip), '--manifest-sha256', result['manifestSha256']],
                                 check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(cli.stdout)['result'], 'passed')
            self.assertEqual(API.digest(cli_zip), result['whole']['sha256'])

    def test_duplicate_input_paths_are_removed_and_public_secret_metadata_is_preserved(self):
        result = self.collect(names=self.names + self.names)
        self.assertEqual(len(result['zipMembers']), 3)
        API.reassemble(self.output, self.restored, result['manifestSha256'])
        with zipfile.ZipFile(self.restored) as archive:
            self.assertEqual(set(archive.namelist()), set(self.names))
            for name in self.names:
                self.assertEqual(archive.read(name), (self.workspace / name).read_bytes())

    def test_small_archive_skips_export_with_no_parts_and_keeps_provenance(self):
        self.write(self.names[0], b'small synthetic package')
        result = self.collect()
        self.assertFalse(result['exported'])
        self.assertLess(result['whole']['bytes'], API.DOWNLOAD_LIMIT)
        self.assertEqual(result['parts'], [])
        self.assertEqual(result['metadata'], self.metadata)
        self.assertEqual({p.name for p in self.output.iterdir()}, {'manifest.json'})
        self.rejected()

    def test_collect_cli_accepts_the_explicit_json_file_list_and_provenance(self):
        paths, metadata = self.base / 'files.json', self.base / 'metadata.json'
        paths.write_text(json.dumps(self.names))
        metadata.write_text(json.dumps(self.metadata))
        cli = subprocess.run([sys.executable, API.__file__, 'collect', '--workspace', str(self.workspace),
                              '--file-list', str(paths), '--metadata', str(metadata), '--output', str(self.output)],
                             check=True, capture_output=True, text=True)
        plan = json.loads(cli.stdout)
        self.assertFalse(plan['exported'])
        self.assertEqual(plan['metadata'], self.metadata)
        self.assertEqual(len(plan['zipMembers']), len(self.names))
        self.assertEqual(plan['manifestSha256'], API.digest(self.output / 'manifest.json'))

    def test_collect_checks_actual_checkout_and_raw_parent_headers_in_a_shallow_merge(self):
        first = self.checkout
        git(self.workspace, 'checkout', '-b', 'feature')
        git(self.workspace, 'commit', '--allow-empty', '-m', 'synthetic PR head')
        head = git(self.workspace, 'rev-parse', 'HEAD')
        git(self.workspace, 'checkout', 'proof')
        git(self.workspace, 'commit', '--allow-empty', '-m', 'synthetic base change')
        git(self.workspace, 'merge', '--no-ff', 'feature', '-m', 'synthetic merge checkout')
        merge = git(self.workspace, 'rev-parse', 'HEAD')
        clone = self.base / 'shallow'
        subprocess.run(['git', 'clone', '--quiet', '--depth=1', '--branch=proof', self.workspace.as_uri(), str(clone)],
                       check=True, capture_output=True)
        self.assertEqual(git(clone, 'rev-parse', '--is-shallow-repository'), 'true')
        self.assertEqual(git(clone, 'show', '-s', '--format=%P', 'HEAD'), '')
        self.assertIn(head, parents(clone))
        self.assertNotEqual(head, merge)
        for name in self.names:
            path = clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.workspace / name, path)
        meta = copy.deepcopy(self.metadata)
        meta.update(headSha=head, checkoutSha=merge, checkoutParents=parents(clone))
        meta['run']['githubSha'] = merge
        result = API.collect(clone, self.names, self.output, meta)
        self.assertEqual(result['metadata']['headSha'], head)
        self.assertEqual(result['metadata']['checkoutSha'], merge)
        self.assertNotEqual(first, head)

    def test_fabricated_checkout_or_parent_provenance_is_rejected_before_output(self):
        for key, value in [('checkoutSha', 'f' * 40), ('checkoutParents', ['e' * 40])]:
            meta = copy.deepcopy(self.metadata)
            meta[key] = value
            if key == 'checkoutSha':
                meta['headSha'] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.collect(metadata=meta)
            self.assertFalse(self.output.exists())

    def test_actual_existing_directory_globs_and_file_depth_rules(self):
        cases = [('verification/results/ci-commands-mingwX64/nested/command.json', 'verification/results/ci-commands-*/'),
                 ('build/driver-profiles/id/source/gdal4k-runtime/build/test-results/jvmTest/TEST-fixture.xml',
                  'build/driver-profiles/**/source/gdal4k-runtime/build/test-results/'),
                 ('build/driver-profiles/source/report.json', 'build/driver-profiles/**/report.json'),
                 ('verification/results/public/passfile-receipt.json', 'verification/results/public/')]
        for name, pattern in cases:
            with self.subTest(name=name):
                self.assertTrue(API.matches_pattern(name, pattern))
        self.assertFalse(API.matches_pattern('logs/nested/file.log', 'logs/*.log'))
        self.assertFalse(API.allowed('logs/private.log', ['logs/*.log', '!logs/private.log']))

    def test_allowlist_escape_and_nonselected_file_are_rejected(self):
        for pattern in ['../outside/**', '/outside/**']:
            meta = copy.deepcopy(self.metadata)
            meta['allowlist'] = [pattern]
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                self.collect(metadata=meta)
        name = 'verification/results/other/public.json'
        self.write(name, b'not selected by original upload')
        with self.assertRaisesRegex(ValueError, 'outside.*allowlist'):
            self.collect(names=[name])

    def test_private_credentials_pgdata_git_tools_and_key_files_are_rejected(self):
        for name in ['.git/config', 'source/v/config', 'verification/results/public/PGDATA/state',
                     'verification/results/public/private/pgpass', 'verification/results/public/passfile',
                     'verification/results/public/server.key', 'verification/results/public/initdb-password']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.collect(names=[name])
        self.assertFalse(self.output.exists())

    def test_symlink_and_parent_escape_paths_are_rejected(self):
        outside = self.base / 'canary'
        outside.write_bytes(b'outside-canary')
        link = self.workspace / 'verification/results/public/link'
        link.symlink_to(outside)
        for name in ['verification/results/public/link', '../canary', '/canary', 'C:/canary']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.collect(names=[name])
        self.assertEqual(outside.read_bytes(), b'outside-canary')

    def test_absolute_workspace_allowlist_preserves_the_same_relative_file_set(self):
        meta = copy.deepcopy(self.metadata)
        meta['allowlist'] = [str(self.workspace / 'verification/results/public') + '/']
        result = self.collect(names=self.names[1:], metadata=meta)
        self.assertEqual(result['publicAllowlist'], ['verification/results/public/'])

    def test_part_corruption_truncation_missing_and_extra_are_rejected(self):
        result = self.collect()
        part = self.output / result['parts'][0]['name']
        original = part.read_bytes()
        for altered in [bytes([original[0] ^ 1]) + original[1:], original[:-1]]:
            part.write_bytes(altered)
            self.rejected(result['manifestSha256'])
        part.unlink()
        self.rejected(result['manifestSha256'])
        part.write_bytes(original)
        extra = self.output / 'part-999.bin'
        extra.write_bytes(b'extra')
        self.rejected(result['manifestSha256'])

    def test_manifest_anchor_order_offset_and_path_tampering_are_rejected(self):
        self.collect()
        manifest_path = self.output / 'manifest.json'
        original = manifest_path.read_bytes()
        self.rejected('0' * 64)
        mutations = [lambda m: m['parts'].reverse(), lambda m: m['parts'][0].update(offset=1),
                     lambda m: m['parts'][0].update(name='../canary'),
                     lambda m: m['zipMembers'][0].update(path='../canary')]
        for mutation in mutations:
            manifest_path.write_bytes(original)
            sha = self.update_manifest(mutation)
            self.rejected(sha)

    def test_whole_and_member_digest_tampering_are_rejected(self):
        self.collect()
        path = self.output / 'manifest.json'
        original = path.read_bytes()
        sha = self.update_manifest(lambda m: m['whole'].update(sha256='0' * 64))
        self.rejected(sha)
        path.write_bytes(original)
        sha = self.update_manifest(lambda m: m['zipMembers'][0].update(sha256='0' * 64))
        self.rejected(sha)

    def test_corrupt_zip_crc_is_rejected_even_with_consistent_transport_hashes(self):
        self.collect()
        value = json.loads((self.output / 'manifest.json').read_text())
        data = bytearray(b''.join((self.output / p['name']).read_bytes() for p in value['parts']))
        central = data.index(b'PK\x01\x02')
        data[central + 16] ^= 1
        for item in value['parts']:
            block = data[item['offset']:item['offset'] + item['bytes']]
            (self.output / item['name']).write_bytes(block)
            item['sha256'] = hashlib.sha256(block).hexdigest()
        value['whole']['sha256'] = hashlib.sha256(data).hexdigest()
        path = self.output / 'manifest.json'
        path.write_text(json.dumps(value))
        self.rejected(API.digest(path), exception=zipfile.BadZipFile)

    def test_actual_zip_size_bound_rejects_central_directory_claim_before_decompression(self):
        path = self.base / 'declared-too-large.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('verification/results/public/data', b'x')
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        struct.pack_into('<I', data, central + 24, 1000)
        path.write_bytes(data)
        with mock.patch.object(API, 'MAX_SOURCE_BYTES', 100), self.assertRaisesRegex(ValueError, 'Actual ZIP inventory.*bound'):
            API.zip_inventory(path)

    def test_source_and_part_count_bounds_fail_without_publishing_transport(self):
        with mock.patch.object(API, 'MAX_SOURCE_BYTES', 100), self.assertRaises(ValueError):
            self.collect()
        self.assertFalse(self.output.exists())
        with mock.patch.object(API, 'MAX_ARCHIVE_BYTES', 100), self.assertRaises(ValueError):
            self.collect()
        self.assertFalse((self.output / 'manifest.json').exists())
        self.assertFalse((self.output / 'audit-content.zip').exists())

    def test_existing_output_and_symlinked_part_are_not_followed_or_overwritten(self):
        result = self.collect()
        first = self.output / result['parts'][0]['name']
        outside = self.base / 'outside'
        outside.write_bytes(first.read_bytes())
        first.unlink()
        first.symlink_to(outside)
        self.rejected(result['manifestSha256'])
        first.unlink()
        first.write_bytes(outside.read_bytes())
        self.restored.write_bytes(b'existing-output-canary')
        with self.assertRaises(ValueError):
            API.reassemble(self.output, self.restored, result['manifestSha256'])
        self.assertEqual(self.restored.read_bytes(), b'existing-output-canary')


if __name__ == '__main__':
    unittest.main()
