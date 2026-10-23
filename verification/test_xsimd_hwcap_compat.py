"""Host compile-only regression for xsimd's pinned ARM SVE runtime selector.

The complete upstream header is retained, but only its real SVE selector is
compiled against declared unit dependencies. No xsimd/GDAL program is linked or
executed and these checks do not establish ARM hardware or producer support.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'verification/fixtures/xsimd-14.3.0'
HEADER = Path('include/xsimd/config/xsimd_cpu_features_arm.hpp')
OVERLAY = ROOT / 'gdal4k-engine/ports/xsimd'
PATCH = OVERLAY / 'sve-hwcap-compat.patch'


def selector(source):
    match = re.search(r'    inline bool arm_cpu_features::sve\(\) const noexcept\n    \{.*?\n    \}',
                      source, re.S)
    if match is None:
        raise AssertionError('Pinned xsimd SVE selector is missing')
    return match.group()


class XsimdHwcapCompatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which('clang++') or shutil.which('c++') or shutil.which('g++')
        if cls.compiler is None:
            raise RuntimeError('A C++ compiler is required for the xsimd compatibility regression')
        original = (FIXTURES / HEADER).read_bytes()
        if hashlib.sha256(original).hexdigest() != '1647ea22ff51e69c1b9466398a62b3a3daaa25e6d361dfac1e8dc1e6d697e5a5':
            raise AssertionError('Pinned xsimd header identity differs')
        cls.original = original.decode('utf-8')
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            file = temporary / HEADER
            file.parent.mkdir(parents=True)
            file.write_bytes(original)
            subprocess.run(['git', 'apply', '--check', str(PATCH)], cwd=temporary,
                           check=True, capture_output=True)
            subprocess.run(['git', 'apply', str(PATCH)], cwd=temporary,
                           check=True, capture_output=True)
            cls.patched = file.read_text(encoding='utf-8')

    def compile(self, source, defines, *, preprocess=False):
        # This token only lets the host compiler type-check the real expression;
        # it is not a replacement Linux capability bit or a runtime CPU result.
        prefix = ''.join('#define ' + name + ' ' + value + '\n'
                         for name, value in defines.items())
        prefix += '''struct unit_hwcap { bool has_feature(unsigned long) const; };
struct arm_cpu_features {
    unit_hwcap hwcap() const;
    bool sve() const noexcept;
};
'''
        program = prefix + selector(source) + '\nauto unit_selector = &arm_cpu_features::sve;\n'
        flags = ['-E', '-P'] if preprocess else ['-fsyntax-only', '-Werror']
        return subprocess.run([self.compiler, '-std=c++17', *flags, '-x', 'c++', '-'],
                              input=program, text=True, capture_output=True, check=False)

    def body(self, source, defines):
        result = self.compile(source, defines, preprocess=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.split('arm_cpu_features::sve() const noexcept', 1)[1].split('}', 1)[0]

    def test_missing_capability_reproduces_original_error_and_selects_existing_false_branch(self):
        defines = {'XSIMD_TARGET_ARM64': '1', 'XSIMD_HAVE_LINUX_GETAUXVAL': '1'}
        before = self.compile(self.original, defines)
        self.assertNotEqual(0, before.returncode)
        self.assertIn('HWCAP_SVE', before.stderr)
        after = self.compile(self.patched, defines)
        self.assertEqual(0, after.returncode, after.stderr)
        body = self.body(self.patched, defines)
        self.assertRegex(body, r'return\s+false\s*;')
        self.assertNotIn('hwcap()', body)

    def test_known_capability_preserves_original_auxiliary_vector_check(self):
        defines = {'XSIMD_TARGET_ARM64': '1', 'XSIMD_HAVE_LINUX_GETAUXVAL': '1',
                   'HWCAP_SVE': '0UL'}
        for source in [self.original, self.patched]:
            result = self.compile(source, defines)
            self.assertEqual(0, result.returncode, result.stderr)
        body = self.body(self.patched, defines)
        self.assertEqual(self.body(self.original, defines), body)
        self.assertIn('hwcap().has_feature(0UL)', body)
        self.assertNotIn('return false', body)

    def test_non_arm64_and_non_linux_paths_retain_original_false_result(self):
        for target_arm64, linux_auxv in [('0', '1'), ('1', '0'), ('0', '0')]:
            defines = {'XSIMD_TARGET_ARM64': target_arm64,
                       'XSIMD_HAVE_LINUX_GETAUXVAL': linux_auxv}
            with self.subTest(defines=defines):
                result = self.compile(self.patched, defines)
                self.assertEqual(0, result.returncode, result.stderr)
                body = self.body(self.patched, defines)
                self.assertEqual(self.body(self.original, defines), body)
                self.assertRegex(body, r'return\s+false\s*;')

    def test_patch_preserves_all_other_detection_and_pinned_port_inputs(self):
        original_selector = selector(self.original)
        expected_selector = original_selector.replace(
            '#if XSIMD_TARGET_ARM64 && XSIMD_HAVE_LINUX_GETAUXVAL\n',
            '#if XSIMD_TARGET_ARM64 && XSIMD_HAVE_LINUX_GETAUXVAL && defined(HWCAP_SVE)\n')
        self.assertEqual(self.original.replace(original_selector, expected_selector), self.patched)
        provenance = json.loads((FIXTURES / 'provenance.json').read_text(encoding='utf-8'))
        for name, expected in provenance['selectedFiles'].items():
            self.assertEqual(expected['sha256'], hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest())
        expected = provenance['originalPortFiles']['fix-pkgconfig-xcomplex.patch']['sha256']
        self.assertEqual(expected, hashlib.sha256((OVERLAY / 'fix-pkgconfig-xcomplex.patch').read_bytes()).hexdigest())
        port = (OVERLAY / 'portfile.cmake').read_text(encoding='utf-8')
        self.assertIn('SHA512 ' + provenance['archive']['sha512'], port)
        spec = json.loads((OVERLAY / 'vcpkg.json').read_text(encoding='utf-8'))
        self.assertEqual('14.3.0', spec['version'])
        self.assertEqual(1, spec['port-version'])
        self.assertEqual(['vcpkg-cmake', 'vcpkg-cmake-config'], [value['name'] for value in spec['dependencies']])
        self.assertEqual({'xcomplex'}, set(spec['features']))


if __name__ == '__main__':
    unittest.main()
