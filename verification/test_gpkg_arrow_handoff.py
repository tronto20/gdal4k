"""Offline pinned-GDAL handoff regression using actual host std::condition_variable.

The real producer/consumer/cancel/terminal blocks are extracted after exact patch
application. SQLite execution, Arrow allocation and value conversion are stubs;
these tests exercise only handoff state, rows, errors, EOF and cancellation.
They do not reproduce the pinned Windows semaphore backend or approve GDAL/JNI
runtime behavior. The original and patched host controls must preserve semantics.
"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'verification/fixtures/gdal-3.12.4-gpkg-handoff'
PORT = ROOT / 'gdal4k-engine/ports/gdal'
HEADER = 'ogr/ogrsf_frmts/gpkg/ogr_geopackage.h'
SOURCE = 'ogr/ogrsf_frmts/gpkg/ogrgeopackagetablelayer.cpp'
ARCHIVE_SHA512 = ('3b915c38cc7c9eb139df9335a90a2f6fd123c54e19ecb1f22670400eac76e317'
                  'c5334f95dff5875c9c3fde8a0ef0f7aea86fa58ad42afaee823a46c6616e9c17')
ORIGINAL_SHA256 = {
    HEADER: '4ce1caae398820173c5ddc4b072afa1c676b6c7935e6d628b1698f298b9d6fd2',
    SOURCE: 'd23f95ea861ebd966175d9fd8a2a6626683ebcf8850c81530ab94010fda12234',
    'LICENSE.TXT': '1dae3468e81d00da56e2936f74d33b8b3ad09d726437f19ce209a5dabea41f77',
}


def fixtures():
    provenance = json.loads((FIXTURE / 'provenance.json').read_text())
    sources = {}
    for record in provenance['members']:
        if record['sha256'] != ORIGINAL_SHA256[record['path']]:
            raise AssertionError('Pinned original member SHA differs')
        stored = (FIXTURE / record['fixture']).read_bytes()
        if record['fixture'].endswith('.gz'):
            if (len(stored), hashlib.sha256(stored).hexdigest()) != (record['compressedBytes'], record['compressedSha256']):
                raise AssertionError('Compressed upstream fixture differs')
            raw = gzip.decompress(stored)
        else:
            raw = stored
        if (len(raw), hashlib.sha256(raw).hexdigest()) != (record['bytes'], record['sha256']):
            raise AssertionError('Original upstream fixture bytes differ')
        sources[record['path']] = raw
    return provenance, sources


def exact_hunks(sources, patch):
    """Require every old hunk at its declared line, with no offset or fuzz."""
    lines = patch.splitlines(keepends=True)
    expected = dict(sources)
    current = None
    offset = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith('--- a/'):
            current = line[len('--- a/'):].strip()
            if current not in sources:
                raise AssertionError('Unexpected upstream patch path')
            offset = 0
        elif line.startswith('@@ '):
            match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if not match or current is None:
                raise AssertionError('Malformed patch hunk')
            start, old_count, _, new_count = [int(value or 1) for value in match.groups()]
            before, after = [], []
            index += 1
            while index < len(lines) and not lines[index].startswith(('@@ ', '--- a/')):
                entry = lines[index]
                if entry.startswith((' ', '-')):
                    before.append(entry[1:])
                if entry.startswith((' ', '+')):
                    after.append(entry[1:])
                if not entry.startswith((' ', '-', '+')):
                    raise AssertionError('Unsupported patch line')
                index += 1
            if len(before) != old_count or len(after) != new_count:
                raise AssertionError('Incorrect patch hunk size')
            original = sources[current].decode().splitlines(keepends=True)
            if original[start - 1:start - 1 + old_count] != before:
                raise AssertionError('Patch context is not exact at its declared line')
            result = expected[current].decode().splitlines(keepends=True)
            position = start - 1 + offset
            result[position:position + old_count] = after
            expected[current] = ''.join(result).encode()
            offset += new_count - old_count
            continue
        index += 1
    return expected


def between(text, start, end):
    if text.count(start) != 1:
        raise AssertionError('Pinned upstream handoff boundary changed: ' + start)
    value = text.split(start, 1)[1]
    if end not in value:
        raise AssertionError('Pinned upstream handoff end is missing')
    return value.split(end, 1)[0]


def render(sources):
    header, source = sources[HEADER].decode(), sources[SOURCE].decode()
    state_start = 'struct OGRGPKGTableLayerFillArrowArray\n'
    state = state_start + between(header, state_start, '\n\nvoid OGR_GPKG_Intersects_Spatial_Filter')
    submit_start = '    const auto SubmitBatchToCaller =\n'
    partial_start = '    const auto DealWithTooBigArray =\n'
    submit = submit_start + between(source, submit_start, '\n' + partial_start)
    partial = partial_start + between(source, partial_start, '\n    {\n        std::unique_lock<std::mutex> oLock(psFillArrowArray->oMutex);')
    wait_start = '    // Wait for GetNextArrowArrayAsynchronousWorker() /\n'
    resume_marker = source.index('        // Resume worker thread\n')
    resume_start = source.rfind('    else\n    {\n', 0, resume_marker)
    if resume_start < 0:
        raise AssertionError('Pinned upstream resume block is missing')
    resume = source[resume_start + len('    else\n'):source.index(wait_start, resume_marker)]
    consume = wait_start + between(source, wait_start, '\n/************************************************************************/\n/*                  GetNextArrowArrayAsynchronousWorker()')
    finish_start = '    std::lock_guard oLock(m_poFillArrowArray->oMutex);\n    m_poFillArrowArray->bIsFinished = true;\n'
    finish = finish_start + between(source, finish_start, '\n/************************************************************************/\n/*                      GetNextArrowArray()')
    cancel_start = 'void OGRGeoPackageTableLayer::CancelAsyncNextArrowArray()\n{\n'
    cancel = between(source, cancel_start, '\n    while (!m_oQueueArrowArrayPrefetchTasks.empty())')
    template = (FIXTURE / 'handoff.cpp.in').read_text()
    for key, value in dict(STATE=state, SUBMIT=submit, PARTIAL=partial, RESUME=resume,
                           CONSUME=consume, FINISH=finish, CANCEL=cancel).items():
        marker = '@@' + key + '@@'
        if template.count(marker) != 1:
            raise AssertionError('Adapter insertion boundary is ambiguous')
        template = template.replace(marker, value)
    return template


class GpkgArrowHandoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = shutil.which('git')
        cls.cxx = shutil.which('c++') or shutil.which('g++') or shutil.which('clang++')
        if not cls.git or not cls.cxx:
            raise RuntimeError('Git and an ordinary host C++ compiler are required')
        cls.provenance, cls.original = fixtures()
        patch = (PORT / 'separate-gpkg-arrow-handoff.patch').read_text()
        cls.patched = exact_hunks(cls.original, patch)
        cls.temporary = tempfile.TemporaryDirectory(prefix='gdal4k-gpkg-handoff-')
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        for name in [HEADER, SOURCE]:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cls.original[name])
        for arguments in [['--check', '--verbose'], []]:
            result = subprocess.run([cls.git, 'apply', *arguments, str(PORT / 'separate-gpkg-arrow-handoff.patch')],
                                    cwd=directory, capture_output=True, text=True)
            if result.returncode or re.search(r'offset|fuzz', result.stderr, re.I):
                raise AssertionError(result.stdout + result.stderr)
        for name in [HEADER, SOURCE]:
            if (directory / name).read_bytes() != cls.patched[name]:
                raise AssertionError('Applied bytes differ from the exact declared hunks')
        cls.executables = {}
        for label, sources in [('original', cls.original), ('patched', cls.patched)]:
            code, executable = directory / (label + '.cpp'), directory / label
            code.write_text(render(sources))
            result = subprocess.run([cls.cxx, '-std=c++17', '-pthread', '-O1', '-Wall', '-Wextra', '-Werror',
                                     str(code), '-o', str(executable)], capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.executables[label] = executable

    def run_case(self, label, case, rounds=1):
        result = subprocess.run([str(self.executables[label]), case, str(rounds)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_fixtures_are_exact_pinned_archive_members_with_license(self):
        self.assertEqual('3.12.4', self.provenance['version'])
        self.assertEqual(ARCHIVE_SHA512, self.provenance['archive']['sha512'])
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, (PORT / 'portfile.cmake').read_text())
        self.assertEqual({HEADER, SOURCE, 'LICENSE.TXT'}, set(self.original))
        for member in self.provenance['members']:
            self.assertEqual('gdal-3.12.4/' + member['path'], member['archiveMember'])
        self.assertIn(b'Permission is hereby granted', self.original['LICENSE.TXT'])

    def test_patch_refuses_an_offset_or_changed_context(self):
        patch = (PORT / 'separate-gpkg-arrow-handoff.patch').read_text()
        for changed in [dict(self.original, **{HEADER: b'\n' + self.original[HEADER]}),
                        dict(self.original, **{HEADER: self.original[HEADER].replace(b'std::mutex oMutex{};', b'std::mutex changed{};', 1)})]:
            with self.subTest(changed=hashlib.sha256(changed[HEADER]).hexdigest()):
                with self.assertRaisesRegex(AssertionError, 'not exact'):
                    exact_hunks(changed, patch)

    def test_real_host_handoff_preserves_batches_eof_errors_and_cancellation(self):
        expected = {'full': (3, 7), 'partial': (3, 6), 'eof': (1, 0), 'error': (2, 0),
                    'cancel': (1, 3), 'cancel-before-first': (0, 0), 'synchronous-partial': (0, 2)}
        for case, (arrays, rows) in expected.items():
            with self.subTest(case=case):
                original = self.run_case('original', case)
                patched = self.run_case('patched', case)
                self.assertEqual(dict(case=case, rounds=1, arrays=arrays, rows=rows), original)
                self.assertEqual(original, patched)

    def test_repeated_patched_batch_handoffs_keep_data_and_terminal_states(self):
        for case, rows in [('full', 7), ('partial', 6)]:
            with self.subTest(case=case):
                self.assertEqual(dict(case=case, rounds=200, arrays=600, rows=200 * rows),
                                 self.run_case('patched', case, rounds=200))


if __name__ == '__main__':
    unittest.main()
