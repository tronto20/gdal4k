"""Exercise real stage selection through both CI transport codecs."""
import contextlib
import gzip
import hashlib
import io
import json
import lzma
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import ci


class CiStageTest(unittest.TestCase):
    def test_legacy_and_xz_preserve_full_tar_and_portable_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / 'gdal4k-engine/build'
            payloads = {
                'engine/linux-arm64/libgdal4k.a': b'archived static bytes\x00\xff',
                'engine/linux-arm64/licenses/gdal.txt': 'Copyright 한글\n'.encode(),
                'jvm-resources/gdal4k/linux-arm64/libgdal4k.so': b'JNI bytes\x00\xff',
            }
            manifest = {'artifacts': {p: hashlib.sha256(b).hexdigest() for p, b in payloads.items()}}
            for name, value in payloads.items():
                path = build / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            manifest_path = build / 'engine/linux-arm64/manifest.json'
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            metadata = [manifest_path.parent / name for name in
                        ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json')]
            for path in metadata:
                path.write_text(json.dumps({'unitTransportOnly': path.name}), encoding='utf-8')
            # Neither SDK/cache inputs nor a non-debug test executable are portable outputs.
            excluded = build / 'engine/linux-arm64/private-sdk.bin'
            excluded.write_bytes(b'not in manifest')
            debug = root / 'gdal4k-runtime/build/bin/linuxArm64/debugTest/test.kexe'
            debug.parent.mkdir(parents=True)
            debug.write_bytes(b'transported source test executable')
            debug.chmod(0o755)
            release = root / 'gdal4k-runtime/build/bin/linuxArm64/releaseTest/test.kexe'
            release.parent.mkdir(parents=True)
            release.write_bytes(b'not requested')
            published = root / 'verification/results/published-consumer-linuxArm64/bin/linuxArm64/debugTest/test.kexe'
            published.parent.mkdir(parents=True)
            published.write_bytes(b'independent consumer executable')
            stdout = io.StringIO()
            with patch.object(ci, 'ROOT', root), patch.object(ci, 'BUILD', build), \
                 contextlib.redirect_stdout(stdout):
                ci.stage('linuxArm64', platforms=['linux-arm64'], include_tests=True)
                ci.stage('linuxArm64', platforms=['linux-arm64'], include_tests=True, archive_format='xz')
            staged = root / 'verification/build/staged'
            legacy = gzip.decompress((staged / 'linuxArm64.tar.gz').read_bytes())
            current = lzma.decompress((staged / 'linuxArm64.tar.xz').read_bytes())
            self.assertEqual(legacy, current)  # Full TAR headers, PAX records, order and padding.
            expected = {str((build / name).relative_to(root)).replace('\\', '/') for name in payloads}
            expected.update(str(p.relative_to(root)).replace('\\', '/') for p in [manifest_path, debug, published, *metadata])
            # The previous stage() wrote this TAR directly into gzip. Keep its
            # full serialization as a reference, including modes and PAX data.
            old_stream = io.BytesIO()
            with tarfile.open(fileobj=old_stream, mode='w') as previous:
                for path in sorted(root / name for name in expected):
                    previous.add(path, arcname=path.relative_to(root), recursive=False)
            self.assertEqual(old_stream.getvalue(), current)
            with tarfile.open(fileobj=io.BytesIO(current)) as archive:
                self.assertEqual(expected, set(archive.getnames()))
                for member in archive:
                    self.assertEqual((root / member.name).read_bytes(), archive.extractfile(member).read())
            self.assertEqual(manifest, json.loads(manifest_path.read_text()))
            self.assertTrue(excluded.exists())
            self.assertEqual(b'not requested', release.read_bytes())
            summaries = [json.loads(line)['stagedArchive'] for line in stdout.getvalue().splitlines() if line.startswith('{')]
            self.assertEqual(2, len(summaries))

    def test_missing_or_nonregular_sdk_metadata_rejects_success_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / 'gdal4k-engine/build'
            engine = build / 'engine/linux-arm64'
            engine.mkdir(parents=True)
            (engine / 'manifest.json').write_text(json.dumps({'artifacts': {}}))
            names = ('sdk-toolchain-inputs.json', 'sdk-source-inputs.json')
            for name in names:
                (engine / name).write_text('{}')
            for name in names:
                metadata = engine / name
                metadata.unlink()
                for directory in [False, True]:
                    if directory: metadata.mkdir()
                    with self.subTest(name=name, directory=directory), \
                         patch.object(ci, 'ROOT', root), patch.object(ci, 'BUILD', build), \
                         self.assertRaisesRegex(RuntimeError, 'regular SDK input metadata'):
                        ci.stage('linuxArm64', platforms=['linux-arm64'])
                    if directory: metadata.rmdir()
                metadata.write_text('{}')
            self.assertFalse((root / 'verification/build/staged').exists())


if __name__ == '__main__':
    unittest.main()
