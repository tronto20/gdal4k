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
ARCHIVE_SHA512 = ('1a7606b5b2460a4fc817d491e1e03262df17ece6869fa3d2b80647baffcfb790'
                  'fbcfa019c3b10fab9f9a14218651b6a358864e40a5d8c5ddc796db2d6e5b469c')
MESON_EXCERPT_SHA256 = '4064630fa1e1673da4bf0f07e1ba84c7049b75e358856653f887fed82e1f73b2'


def patched_source(directory, git):
    shutil.copytree(FIXTURE / 'src', directory / 'src')
    subprocess.run([git, 'apply', '--check', str(PATCH)], cwd=directory,
                   check=True, capture_output=True)
    subprocess.run([git, 'apply', str(PATCH)], cwd=directory,
                   check=True, capture_output=True)
    patched = (directory / MESON_FILE).read_text()
    if 'meson.project_source_root()' not in patched:
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
        with tempfile.TemporaryDirectory() as temporary:
            cls.patched = patched_source(Path(temporary), cls.git)

    def test_fixture_members_match_the_pinned_archive_and_meson_source(self):
        provenance = json.loads((FIXTURE / 'provenance.json').read_text())
        self.assertEqual('18.4', provenance['postgresqlVersion'])
        self.assertEqual(ARCHIVE_SHA512, provenance['archive']['sha512'])
        self.assertIn('SHA512 ' + ARCHIVE_SHA512, (PORT / 'portfile.cmake').read_text())
        self.assertEqual('18.4', json.loads((PORT / 'vcpkg.json').read_text())['version'])
        self.assertEqual({'COPYRIGHT', MESON_FILE, SCRIPT, *INPUTS},
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

    def test_patch_preserves_three_file_dependencies_and_the_existing_target(self):
        canonical = re.findall(r"meson\.project_source_root\(\) / '([^']+)'", self.patched)
        self.assertEqual([*INPUTS, SCRIPT], canonical)
        self.assertRegex(self.patched, r'input:\s*files\(\s*meson\.project_source_root\(\)')
        self.assertIn("perl, files(meson.project_source_root() / '" + SCRIPT + "')", self.patched)
        self.assertIn("'@INPUT@'", self.patched)
        # Only the three File names change: target dependencies, output,
        # installation, Perl selection and generator code remain identical.
        restored = self.patched
        for source in [*INPUTS, SCRIPT]:
            relative = ntpath.relpath(source, SOURCE_SUBDIR).replace('\\', '/')
            # Upstream intentionally spells the first input as ../../include/...
            if source == INPUTS[0]:
                relative = '../../include/storage/lwlocklist.h'
            restored = restored.replace("meson.project_source_root() / '" + source + "'",
                                        "'" + relative + "'")
        self.assertEqual(self.original, restored)

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
        for source in [*INPUTS, SCRIPT]:
            with self.subTest(source=source):
                relative = '../../' + source.removeprefix('src/')
                before = meson_file.from_source_file(source_root, SOURCE_SUBDIR, relative)
                after = meson_file.from_source_file(source_root, SOURCE_SUBDIR,
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
        self.assertEqual(6, len(checked))

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


if __name__ == '__main__':
    unittest.main()
