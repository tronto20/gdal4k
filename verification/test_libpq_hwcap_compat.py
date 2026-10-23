"""Compile-only macro regression for the pinned libpq SVE runtime selector.

The host compiler checks a small extracted C function; it never builds or runs
libpq/GDAL and does not establish an ARM producer or runtime result.
"""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'verification/fixtures/libpq-18.4/pg_popcount_aarch64.c'
PATCH = ROOT / 'gdal4k-engine/ports/libpq/popcount-sve-hwcap-compat.patch'


def selector(text):
    match = re.search(r'static inline bool\npg_popcount_sve_available\(void\)\n\{.*?\n\}', text, re.S)
    assert match, 'Pinned source selector missing'
    return match.group()


class LibpqHwcapCompatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which('clang') or shutil.which('cc') or shutil.which('gcc')
        if cls.compiler is None:
            raise RuntimeError('A C compiler is required for the libpq compatibility regression')
        original = FIXTURE.read_bytes()
        assert hashlib.sha256(original).hexdigest() == '1021630474a30bef8097ba01623abd47ad6dd7b19bc5f86d0318c6296a67aac2'
        cls.original = original.decode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root/'src/port/pg_popcount_aarch64.c'
            file.parent.mkdir(parents=True)
            file.write_bytes(original)
            subprocess.run(['git','apply','--check',str(PATCH)],cwd=root,check=True,capture_output=True)
            subprocess.run(['git','apply',str(PATCH)],cwd=root,check=True,capture_output=True)
            cls.patched = file.read_text()

    def compile(self, source, defines, preprocess=False):
        # The capability value is a unit-test token, not a replacement kernel ABI
        # constant. No system headers or producer flags are altered.
        prefix = '#include <stdbool.h>\n#include <stddef.h>\n#define AT_HWCAP 0\n'
        prefix += 'extern unsigned long getauxval(unsigned long);\n'
        prefix += 'extern int elf_aux_info(int, void *, int);\n'
        prefix += ''.join('#define '+name+' '+value+'\n' for name,value in defines.items())
        program = prefix + selector(source) + '\nbool (*unit_selector)(void) = pg_popcount_sve_available;\n'
        flags = ['-E','-P'] if preprocess else ['-fsyntax-only','-Werror']
        return subprocess.run([self.compiler,'-std=c11',*flags,'-x','c','-'],input=program,
                              text=True,capture_output=True,check=False)

    def body(self, defines):
        result=self.compile(self.patched,defines,preprocess=True)
        self.assertEqual(0,result.returncode,result.stderr)
        return result.stdout.split('pg_popcount_sve_available(void)',1)[1].split('}',1)[0]

    def test_old_headers_reproduce_original_error_and_patched_selector_uses_fallback(self):
        for api in ['HAVE_GETAUXVAL','HAVE_ELF_AUX_INFO']:
            with self.subTest(api=api):
                original=self.compile(self.original,{api:'1'})
                self.assertNotEqual(0,original.returncode)
                self.assertIn('HWCAP_SVE',original.stderr)
                result=self.compile(self.patched,{api:'1'})
                self.assertEqual(0,result.returncode,result.stderr)
                body=self.body({api:'1'})
                self.assertNotIn('getauxval(',body)
                self.assertNotIn('elf_aux_info(',body)
                self.assertNotIn('HWCAP_SVE',body)
                self.assertRegex(body,r'return\s+0\s*;')

    def test_known_capability_headers_preserve_both_auxiliary_vector_checks(self):
        for api,call in [('HAVE_GETAUXVAL','getauxval('),('HAVE_ELF_AUX_INFO','elf_aux_info(')]:
            defines={api:'1','HWCAP_SVE':'0UL'}
            with self.subTest(api=api):
                result=self.compile(self.patched,defines)
                self.assertEqual(0,result.returncode,result.stderr)
                self.assertIn(call,self.body(defines))

    def test_no_auxiliary_api_keeps_existing_false_fallback(self):
        for defines in [{},{'HWCAP_SVE':'0UL'}]:
            result=self.compile(self.patched,defines)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertRegex(self.body(defines),r'return\s+0\s*;')

    def test_patch_changes_only_detection_guards_and_retains_original_neon_selection(self):
        expected=self.original.replace('#ifdef HAVE_ELF_AUX_INFO\n',
            '\t/* Older target headers can lack HWCAP_SVE even when SVE intrinsics compile. */\n#if defined(HAVE_ELF_AUX_INFO) && defined(HWCAP_SVE)\n')
        expected=expected.replace('#elif defined(HAVE_GETAUXVAL)\n',
                                  '#elif defined(HAVE_GETAUXVAL) && defined(HWCAP_SVE)\n')
        self.assertEqual(expected,self.patched)
        self.assertIn('pg_popcount_optimized = pg_popcount_neon;',self.patched)
        self.assertIn('pg_popcount_masked_optimized = pg_popcount_masked_neon;',self.patched)


if __name__=='__main__': unittest.main()
