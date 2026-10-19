"""Check producer integration without invoking a dependency build or fabricating an engine."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'gdal4k-engine/tools'
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location('engine_builder', TOOLS / 'build-engine.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ConfiguredToolsTest(unittest.TestCase):
    def test_native_and_web_configure_use_explicit_tools(self):
        class Configured(Exception):
            pass
        for target, base, http in [('macos-arm64', 'arm64-osx', False), ('web', 'wasm32-emscripten', False), ('macos-arm64', 'arm64-osx', True)]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                binary = root / 'gdal4k-engine'
                (binary / 'engine').mkdir(parents=True)
                (binary / 'engine/vcpkg.json').write_text(json.dumps({'dependencies': [{'name': 'gdal', 'features': ['curl'] if http else []}]}))
                vcpkg = root / 'shared vcpkg'
                (vcpkg / '.git').mkdir(parents=True)
                (vcpkg / 'triplets').mkdir()
                (vcpkg / 'triplets' / (base + '.cmake')).touch()
                (vcpkg / ('vcpkg.exe' if os.name == 'nt' else 'vcpkg')).touch()
                out = binary / 'build/engine' / target
                out.mkdir(parents=True)
                cmake, ninja = str(root / 'custom cmake'), str(root / 'custom ninja')
                commands = []
                def run(*command, **kwargs):
                    commands.append(command)
                    if str(command[0]) == cmake:
                        raise Configured()
                args = argparse.Namespace(target=target, jobs=2)
                with patch.object(builder, 'ROOT', root), patch.object(builder, 'BINARY', binary), \
                     patch.object(builder, 'run', run), \
                     patch.object(builder.subprocess, 'check_output', return_value=builder.VCPKG_REV), \
                     patch.dict(os.environ, {'EMSDK': str(root / 'custom sdk')}):
                    with self.assertRaises(Configured):
                        builder.build(args, vcpkg, out, 'source fingerprint', cmake, ninja)
                configure = commands[-1]
                self.assertEqual(cmake, configure[0])
                self.assertIn('-DCMAKE_MAKE_PROGRAM=' + ninja, configure)
                self.assertIn('-G', configure)
                self.assertIn('Ninja', configure)
                self.assertIn('-DGDAL4K_ENABLE_HTTP=' + ('ON' if http else 'OFF'), configure)
                options = [f'--x-buildtrees-root={out/"vcpkg-buildtrees"}', f'--x-packages-root={out/"vcpkg-packages"}']
                self.assertTrue(all(option in commands[0] for option in options))
                self.assertIn('-DVCPKG_INSTALL_OPTIONS=' + ';'.join(options), configure)
                if target == 'web':
                    self.assertIn('-DCMAKE_SYSTEM_NAME=Emscripten', configure)
                    self.assertIn('-DVCPKG_CHAINLOAD_TOOLCHAIN_FILE=' + str(root / 'custom sdk/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake'), configure)

    @unittest.skipIf(os.name == 'nt', 'Creating directory symlinks needs Windows privileges')
    def test_workspace_symlinks_are_rejected_without_modifying_shared_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root/'producer/build/engine/web'
            out.mkdir(parents=True)
            shared = root/'shared'
            shared.mkdir()
            marker = shared/'keep'
            marker.write_text('untouched')
            with patch.object(builder, 'ROOT', root/'producer'):
                for name in ['installed','vcpkg-buildtrees','vcpkg-packages']:
                    link = out/name
                    link.symlink_to(shared, target_is_directory=True)
                    with self.assertRaisesRegex(RuntimeError, 'shared or symlinked'):
                        builder.vcpkg_work_options(out)
                    link.unlink()
                with self.assertRaisesRegex(RuntimeError, 'outside the producer'):
                    builder.vcpkg_work_options(shared)
                local_link = root/'producer/linked-target'
                local_link.symlink_to(out, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, 'shared or symlinked'):
                    builder.vcpkg_work_options(local_link)
                with self.assertRaisesRegex(RuntimeError, 'shared or symlinked'):
                    builder.vcpkg_work_options(local_link/'nested')
            self.assertEqual('untouched', marker.read_text())


if __name__ == '__main__':
    unittest.main()
