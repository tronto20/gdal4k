"""Use the exact engine compiler when a Linux toolchain omits its CMake cache entry."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('arrow_ipc',Path(__file__).parent/'driver-profile/arrow-ipc-check.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class ArrowIpcToolchainTest(unittest.TestCase):
    def test_linux_normal_variable_uses_compiled_metadata_with_matching_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);compiler=root/'compiler with spaces';compiler.touch()
            metadata=root/'CMakeFiles/4.4.4/CMakeCXXCompiler.cmake';metadata.parent.mkdir(parents=True)
            metadata.write_text('set(CMAKE_CXX_COMPILER "'+str(compiler).replace('\\','/')+'")\n')
            other=root/'CMakeFiles/3.31.0/CMakeCXXCompiler.cmake';other.parent.mkdir(parents=True);other.write_text('old metadata must not be selected')
            cache={'CMAKE_CACHE_MAJOR_VERSION':'4','CMAKE_CACHE_MINOR_VERSION':'4','CMAKE_CACHE_PATCH_VERSION':'4'}
            self.assertEqual(Path(module.compiler_from_cmake(cache,root)),compiler)

    def test_cached_compiler_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);compiler=root/'clang++';compiler.touch()
            self.assertEqual(Path(module.compiler_from_cmake({'CMAKE_CXX_COMPILER':str(compiler)},root)),compiler)

    def test_missing_or_conflicting_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with self.assertRaisesRegex(AssertionError,'Missing exact'):module.compiler_from_cmake({},root)
            compiler=root/'clang++';compiler.touch();other=root/'other';other.touch()
            metadata=root/'CMakeFiles/4.4.4/CMakeCXXCompiler.cmake';metadata.parent.mkdir(parents=True)
            metadata.write_text('set(CMAKE_CXX_COMPILER "'+str(other).replace('\\','/')+'")\n')
            with self.assertRaisesRegex(AssertionError,'mismatch'):module.compiler_from_cmake({'CMAKE_CXX_COMPILER':str(compiler)},root)
