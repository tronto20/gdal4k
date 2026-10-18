"""A missing artifact must not make the ordinary producer install into shared dependencies."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'gdal4k-engine/tools'))
spec=importlib.util.spec_from_file_location('producer_shared_guard',Path(__file__).resolve().parent.parent/'gdal4k-engine/tools/build-engine.py')
producer=importlib.util.module_from_spec(spec);spec.loader.exec_module(producer)

class SharedPrefixTest(unittest.TestCase):
    def test_invalid_manifest_with_shared_prefix_stops_before_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker=Path(temporary)/'original-dependency';marker.write_bytes(b'original')
            with patch.object(producer,'BINARY',Path(temporary)),patch.object(producer,'fingerprint',return_value='changed'),patch.object(producer,'reusable',return_value=False),patch.object(Path,'is_symlink',return_value=True),patch.object(producer,'build') as build,patch.object(sys,'argv',['build-engine.py','macos-arm64']):
                with self.assertRaisesRegex(RuntimeError,'Refusing vcpkg installation'):producer.main()
                build.assert_not_called()
            self.assertEqual(marker.read_bytes(),b'original')

    def test_verified_payload_reuse_does_not_enter_installer_for_shared_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(producer,'BINARY',Path(temporary)),patch.object(producer,'fingerprint',return_value='verified'),patch.object(producer,'reusable',return_value=True),patch.object(Path,'is_symlink',return_value=True),patch.object(producer,'build') as build,patch.object(sys,'argv',['build-engine.py','macos-arm64']):
                producer.main();build.assert_not_called()
