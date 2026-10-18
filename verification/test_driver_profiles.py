"""Exercise profile isolation against a real temporary Git checkout, without installing dependencies."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('driver_profile', Path(__file__).resolve().parents[1] / 'gdal4k-engine/tools/driver-profile.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ProfileIsolationTest(unittest.TestCase):
    def test_isolated_sources_version_and_tamper_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {'gradle.properties': 'gdal4kVersion=0.1.0-SNAPSHOT\n',
                     'build.gradle.kts': '// default build\n',
                     'gdal4k-engine/engine/vcpkg.json': json.dumps({'dependencies': [{'name': 'gdal', 'default-features': False, 'features': ['geos']}]}),
                     'verification/consumer.py': "for test_name in ['DatasetApiTest']: pass\n",
                     'verification/driver-profile/DriverProfileApiTest.kt': '@PROFILE@ @HDF5_PATH@',
                     'gdal4k-runtime/src/commonTest/kotlin/dev/gdal4k/Existing.kt': '// API test\n',
                     '.gitignore': 'build/\n'}
            for name, text in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
            subprocess.run(['git', 'init', '-q', root], check=True)
            subprocess.run(['git', 'add', '--', *files], cwd=root, check=True)
            before = {name: (root / name).read_bytes() for name in files}
            scientific, data = module.prepare(root, 'scientific', 'linux-amd64')
            parquet, other = module.prepare(root, 'parquet', 'linux-amd64')
            self.assertNotEqual(scientific, parquet)
            self.assertEqual('0.1.0-scientific-SNAPSHOT', data['version'])
            self.assertNotEqual(data['version'], other['version'])
            features = json.loads((scientific / 'gdal4k-engine/engine/vcpkg.json').read_text())['dependencies'][0]['features']
            self.assertEqual(['geos', 'hdf5', 'netcdf'], features)
            dependencies = json.loads((scientific / 'gdal4k-engine/engine/vcpkg.json').read_text())['dependencies']
            self.assertIn({'name': 'netcdf-c', 'default-features': False, 'features': ['netcdf-4']}, dependencies)
            self.assertTrue((scientific / 'verification/consumer.py').read_text().startswith("for test_name in ['DriverProfileApiTest'"))
            self.assertEqual(before, {name: (root / name).read_bytes() for name in files})
            self.assertEqual(scientific, module.prepare(root, 'scientific', 'linux-amd64')[0])
            (scientific / 'build.gradle.kts').write_text('// unexpected edit')
            with self.assertRaisesRegex(ValueError, 'source changed'):
                module.prepare(root, 'scientific', 'linux-amd64')

    def test_unsupported_target_fails_before_creating_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'no verified build matrix'):
                module.prepare(root, 'postgresql', 'macos-arm64')
            self.assertFalse((root / 'build').exists())

    def test_release_versions_remain_separate(self):
        self.assertEqual('1.2.3-parquet', module.profile_version('1.2.3', 'parquet'))

if __name__ == '__main__': unittest.main()
