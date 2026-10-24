"""Pinned-source regression for libpq's Windows Perl generator paths.

These tests need Git and Perl, use no network, and never execute Windows Perl,
build libpq/GDAL, or establish Windows producer/runtime acceptance.
"""
import functools
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
PORT = ROOT / 'gdal4k-engine/ports/libpq'
FIXTURE = ROOT / 'verification/fixtures/libpq-windows-paths'
PATCH_NAME = 'windows/meson-canonical-generator-paths.patch'
PATCH = PORT / PATCH_NAME
MESON_FILE = 'src/include/storage/meson.build'
SOURCE_SUBDIR = 'src/include/storage'
SCRIPT = 'src/backend/storage/lmgr/generate-lwlocknames.pl'
INPUTS = ['src/include/storage/lwlocklist.h',
          'src/backend/utils/activity/wait_event_names.txt']
WAIT_SCRIPT = 'src/backend/utils/activity/generate-wait_event_types.pl'
FILE_CHANGES = {
    MESON_FILE: {
        '../../include/storage/lwlocklist.h': INPUTS[0],
        '../../backend/utils/activity/wait_event_names.txt': INPUTS[1],
        '../../backend/storage/lmgr/generate-lwlocknames.pl': SCRIPT,
    },
    'src/include/utils/meson.build': {
        '../../backend/utils/activity/wait_event_names.txt': INPUTS[1],
        '../../backend/utils/activity/generate-wait_event_types.pl': WAIT_SCRIPT,
        '../../backend/utils/errcodes.txt': 'src/backend/utils/errcodes.txt',
        '../../backend/utils/generate-errcodes.pl': 'src/backend/utils/generate-errcodes.pl',
        '../../backend/utils/probes.d': 'src/backend/utils/probes.d',
        '../../backend/utils/Gen_dummy_probes.pl': 'src/backend/utils/Gen_dummy_probes.pl',
        '../catalog/pg_proc.dat': 'src/include/catalog/pg_proc.dat',
        '../../backend/utils/Gen_fmgrtab.pl': 'src/backend/utils/Gen_fmgrtab.pl',
    },
    'src/include/catalog/meson.build': {
        '../../backend/catalog/genbki.pl': 'src/backend/catalog/genbki.pl',
    },
    'src/include/nodes/meson.build': {
        '../../backend/nodes/gen_node_support.pl': 'src/backend/nodes/gen_node_support.pl',
    },
}
ARCHIVE_SHA512 = ('1a7606b5b2460a4fc817d491e1e03262df17ece6869fa3d2b80647baffcfb790'
                  'fbcfa019c3b10fab9f9a14218651b6a358864e40a5d8c5ddc796db2d6e5b469c')
MESON_EXCERPT_SHA256 = '4064630fa1e1673da4bf0f07e1ba84c7049b75e358856653f887fed82e1f73b2'


def patched_source(directory, git):
    shutil.copytree(FIXTURE / 'src', directory / 'src')
    subprocess.run([git, 'apply', '--check', str(PATCH)], cwd=directory,
                   check=True, capture_output=True)
    subprocess.run([git, 'apply', str(PATCH)], cwd=directory,
                   check=True, capture_output=True)
    patched = {name: (directory / name).read_text() for name in FILE_CHANGES}
    if sum(text.count('meson.project_source_root()') for text in patched.values()) != 14:
        raise AssertionError('Git did not apply the canonical-path patch to the fixture')
    return patched


class LibpqWindowsPathsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = shutil.which('git')
        cls.perl = shutil.which('perl')
        if cls.git is None or cls.perl is None:
            raise RuntimeError('Git and Perl are required for the libpq path regression')
        cls.original = (FIXTURE / MESON_FILE).read_text()
        cls.original_files = {name: (FIXTURE / name).read_text() for name in FILE_CHANGES}
        with tempfile.TemporaryDirectory() as temporary:
            cls.patched_files = patched_source(Path(temporary), cls.git)
        cls.patched = cls.patched_files[MESON_FILE]

    def test_fixture_members_match_the_pinned_archive_and_meson_source(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual('18.4', provenance['postgresqlVersion'])
        self.assertEqual(ARCHIVE_SHA512, provenance['archive']['sha512'])
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, (PORT / 'portfile.cmake').read_text())
        self.assertEqual('18.4', json.loads((PORT / 'vcpkg.json').read_text())['version'])
        self.assertEqual({'COPYRIGHT', *FILE_CHANGES, SCRIPT, WAIT_SCRIPT, *INPUTS},
                         {member['path'] for member in provenance['members']})
        for member in provenance['members']:
            with self.subTest(member=member['path']):
                data = (FIXTURE / member['path']).read_bytes()
                self.assertEqual(member['bytes'], len(data))
                self.assertEqual(member['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual('postgresql-18.4/' + member['path'], member['archiveMember'])
        meson = provenance['mesonFileClass']
        self.assertEqual(MESON_EXCERPT_SHA256, meson['excerptSha256'])
        self.assertEqual(MESON_EXCERPT_SHA256,
                         hashlib.sha256((FIXTURE / meson['excerptPath']).read_bytes()).hexdigest())
        self.assertEqual(meson['license']['sha256'],
                         hashlib.sha256((FIXTURE / meson['license']['path']).read_bytes()).hexdigest())

    def test_patch_is_selected_only_inside_the_windows_target_condition(self):
        port = (PORT / 'portfile.cmake').read_text()
        prefix, extraction = port.split('vcpkg_extract_source_archive(', 1)
        # Require the patch list to start empty and be populated solely inside
        # the Windows branch, before the same extraction used by other targets.
        selected = re.search(r'set\((\w+)\)\s*'
                             r'if\(VCPKG_TARGET_IS_WINDOWS\)\s*'
                             r'list\(APPEND \1 ([^\s)]+)\)\s*endif\(\)\s*$', prefix)
        self.assertIsNotNone(selected, 'Windows-only source patch selection missing')
        variable, patch = selected.groups()
        self.assertEqual(PATCH_NAME, patch)
        self.assertEqual(1, port.count(PATCH_NAME))
        extraction = extraction.split('file(GLOB _py3_include_path', 1)[0]
        self.assertIn('PATCHES', extraction)
        self.assertEqual(1, extraction.count('${' + variable + '}'))
        self.assertIn('popcount-sve-hwcap-compat.patch', extraction)
        # The original Meson source is not rewritten for POSIX targets: their
        # empty conditional patch list leaves the upstream File paths intact.
        self.assertIn("files('../../backend/storage/lmgr/generate-lwlocknames.pl')", self.original)

    def test_patch_preserves_shared_header_file_dependencies_and_existing_targets(self):
        count = 0
        for name, replacements in FILE_CHANGES.items():
            with self.subTest(meson=name):
                original, patched = self.original_files[name], self.patched_files[name]
                expected = [replacements[value] for value in re.findall(r"'([^']+)'", original)
                            if value in replacements]
                canonical = re.findall(r"meson\.project_source_root\(\) / '([^']+)'", patched)
                self.assertEqual(expected, canonical)
                count += len(canonical)
                restored = patched
                for relative, source in replacements.items():
                    restored = restored.replace("meson.project_source_root() / '" + source + "'",
                                                "'" + relative + "'")
                # Only path spelling changes; file tracking, target dependencies,
                # output, installation, Perl selection and scripts remain identical.
                self.assertEqual(original, restored)
        self.assertEqual(14, count)
        self.assertIn("perl, files(meson.project_source_root() / '" + WAIT_SCRIPT + "')",
                      self.patched_files['src/include/utils/meson.build'])

    def test_pinned_meson_file_paths_remove_the_logged_parent_segments(self):
        # Execute the exact pinned Meson File class with Windows lexical path
        # operations. The existence hook records checks; it is not a Windows
        # filesystem probe or an emulation of Strawberry Perl's Win32 calls.
        checked = []
        windows_path = SimpleNamespace(join=ntpath.join, normpath=ntpath.normpath,
                                       splitext=ntpath.splitext,
                                       isfile=lambda value: checked.append(value) or True)
        namespace = {'HoldableObject': object, 'lru_cache': functools.lru_cache,
                     'os': SimpleNamespace(path=windows_path), 'MesonException': RuntimeError}
        exec(compile((FIXTURE / 'meson-1.9.0-file-class.py').read_text(),
                     'pinned-meson-1.9.0-file-class.py', 'exec'), namespace)
        meson_file = namespace['File']
        cwd = ('D:/a/gdal4k/gdal4k/build/driver-profiles/w/cedf2533174b82dcc9d595bd/'
               'source/gdal4k-engine/build/engine/windows-amd64/vcpkg-buildtrees/libpq/'
               'gdal4k-windows-amd64-rel')
        build_to_source = '../src/tgresql-18-5510452789.clean'
        source_root = ntpath.normpath(ntpath.join(cwd, build_to_source))
        path_count = 0
        for name, replacements in FILE_CHANGES.items():
            subdir = str(Path(name).parent).replace('\\', '/')
            for relative, source in replacements.items():
                path_count += 1
                before = meson_file.from_source_file(source_root, subdir, relative)
                after = meson_file.from_source_file(source_root, subdir,
                                                    ntpath.join(source_root, source))
                self.assertIsInstance(before, meson_file)
                self.assertIsInstance(after, meson_file)
                old_argument = before.rel_to_builddir(build_to_source)
                new_argument = after.rel_to_builddir(build_to_source)
                self.assertTrue(ntpath.isabs(new_argument))
                self.assertNotIn('..', Path(new_argument.replace('\\', '/')).parts)
                self.assertEqual(ntpath.normcase(ntpath.normpath(ntpath.join(cwd, old_argument))),
                                 ntpath.normcase(new_argument))
                self.assertLess(len(new_argument), 260)
                self.assertEqual(new_argument, checked[-1])
                if source == SCRIPT:
                    self.assertEqual(269, len(cwd + '/' + old_argument))
                    self.assertEqual(219, len(new_argument))
                if source == WAIT_SCRIPT:
                    self.assertGreaterEqual(len(cwd + '/' + old_argument), 260)
        self.assertEqual(13, path_count)  # probes.d occurs in both DTrace branches.
        self.assertEqual(26, len(checked))

    def test_real_perl_generator_output_is_byte_identical_for_original_and_canonical_paths(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-pg-') as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            patched_source(source, self.git)
            build = root / 'build'
            build.mkdir()
            original_output, canonical_output = root / 'original', root / 'canonical'
            original_output.mkdir()
            canonical_output.mkdir()
            build_to_source = os.path.relpath(source, build)
            old_paths = [os.path.join(build_to_source, SOURCE_SUBDIR,
                                     '../../' + name.removeprefix('src/'))
                         for name in [SCRIPT, *INPUTS]]
            new_paths = [str(source / name) for name in [SCRIPT, *INPUTS]]
            for paths, output in [(old_paths, original_output), (new_paths, canonical_output)]:
                subprocess.run([self.perl, paths[0], '-o', str(output), *paths[1:]], cwd=build,
                               check=True, capture_output=True)
            original = (original_output / 'lwlocknames.h').read_bytes()
            self.assertEqual(original, (canonical_output / 'lwlocknames.h').read_bytes())
            self.assertTrue(original.startswith(b'/* autogenerated from src/include/storage/lwlocklist.h'))
            self.assertIn(b'(&MainLWLockArray[53].lock)', original)
            self.assertIn(b'#define NUM_INDIVIDUAL_LWLOCKS\t\t54', original)
            # The pinned list has 43 named locks and gaps in its index space.
            self.assertEqual(43, original.count(b'(&MainLWLockArray['))

    def test_real_wait_event_generator_preserves_all_three_outputs(self):
        with tempfile.TemporaryDirectory(prefix='gdal4k-pg-wait-') as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            patched_source(source, self.git)
            build = root / 'build'
            build.mkdir()
            original_output, canonical_output = root / 'original', root / 'canonical'
            original_output.mkdir()
            canonical_output.mkdir()
            prefix = os.path.join(os.path.relpath(source, build), 'src/include/utils', '../..')
            old_paths = [os.path.join(prefix, name.removeprefix('src/'))
                         for name in [WAIT_SCRIPT, INPUTS[1]]]
            new_paths = [str(source / name) for name in [WAIT_SCRIPT, INPUTS[1]]]
            for paths, output in [(old_paths, original_output), (new_paths, canonical_output)]:
                subprocess.run([self.perl, paths[0], '--outdir', str(output), '--code', paths[1]],
                               cwd=build, check=True, capture_output=True)
            expected = {'wait_event_types.h', 'pgstat_wait_event.c', 'wait_event_funcs_data.c'}
            self.assertEqual(expected, {path.name for path in original_output.iterdir()})
            self.assertEqual(expected, {path.name for path in canonical_output.iterdir()})
            for name in expected:
                data = (original_output / name).read_bytes()
                self.assertEqual(data, (canonical_output / name).read_bytes())
                self.assertGreater(len(data), 1000)
            self.assertIn(b'WAIT_EVENT_ARCHIVER_MAIN', (canonical_output / 'wait_event_types.h').read_bytes())


if __name__ == '__main__':
    unittest.main()
