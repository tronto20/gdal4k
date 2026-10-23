"""Pure public-schema and generated-source transfer regressions; no GDAL/processes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('runtime_fixture', ROOT / 'verification/driver-profile/runtime-fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def public_config():
    return dict(schemaVersion=1, profile='combined', target='linux-arm64', runId='arm64_fixture_0123456789',
                postgresqlDsn='PG:host=127.0.0.1 port=15432 dbname=g4k_012345 user=g4k_012345',
                httpUrls={'http': 'http://127.0.0.1:18080', 'https': 'https://127.0.0.1:18443',
                          'untrusted': 'https://127.0.0.1:18444', 'wrong-name': 'https://127.0.0.1:18445'},
                hdf5Path='/owned/fixture 영상.h5')


class RuntimeFixtureContractTest(unittest.TestCase):
    def test_public_configuration_is_canonical_and_independently_copied(self):
        value = public_config()
        canonical = fixture.canonical_json(value)
        self.assertEqual(fixture.parse(canonical), value)
        self.assertEqual(canonical, fixture.canonical_json(dict(reversed(list(value.items())))))
        self.assertTrue(canonical.isascii())
        validated = fixture.validate(value)
        value['httpUrls']['http'] = 'http://127.0.0.1:9999'
        self.assertEqual(validated['httpUrls']['http'], 'http://127.0.0.1:18080')

    def test_configuration_is_complete_and_private_fields_are_rejected(self):
        for name in fixture.KEYS:
            value = public_config(); del value[name]
            with self.subTest(missing=name), self.assertRaises(ValueError): fixture.validate(value)
        for name in ['password', 'PGPASSFILE', 'privateKey', 'caPrivateKey']:
            value = dict(public_config(), **{name: 'never-serialize-secret'})
            with self.subTest(extra=name), self.assertRaises(ValueError) as error: fixture.validate(value)
            self.assertNotIn('never-serialize-secret', str(error.exception))

    def test_wrong_target_schema_profile_and_run_identity_are_rejected(self):
        for key, values in {'schemaVersion': [True, '1', 1.0, 2], 'profile': ['http', 'scientific'],
                            'target': ['linux-amd64', 'macos-arm64'],
                            'runId': ['', 'short', 'A' * 16, '../' + 'a' * 20, 'a' * 81]}.items():
            for invalid in values:
                value = public_config(); value[key] = invalid
                with self.subTest(field=key, value=invalid), self.assertRaises(ValueError): fixture.validate(value)

    def test_postgresql_is_only_canonical_password_free_owned_loopback(self):
        for invalid in ['PG:host=example.com port=15432 dbname=g4k_012345 user=g4k_012345',
                        'PG:host=127.0.0.1 port=65536 dbname=g4k_012345 user=g4k_012345',
                        'PG:host=127.0.0.1 port=0 dbname=g4k_012345 user=g4k_012345',
                        'PG:host=127.0.0.1 port=15432 dbname=g4k_012345 user=another',
                        public_config()['postgresqlDsn'] + ' password=never-serialize-secret']:
            value = public_config(); value['postgresqlDsn'] = invalid
            with self.subTest(endpoint=invalid), self.assertRaises(ValueError) as error: fixture.validate(value)
            self.assertNotIn('never-serialize-secret', str(error.exception))

    def test_http_servers_require_four_distinct_exact_loopback_origins(self):
        for invalid in ['http://example.com:18080', 'http://localhost:18080', 'http://127.0.0.1:65536',
                        'http://127.0.0.1:0', 'http://127.0.0.1:18080/path',
                        'http://127.0.0.1:18080?token=secret', 'https://127.0.0.1:18080']:
            value = public_config(); value['httpUrls']['http'] = invalid
            with self.subTest(origin=invalid), self.assertRaises(ValueError): fixture.validate(value)
        value = public_config(); value['httpUrls']['untrusted'] = value['httpUrls']['https']
        with self.assertRaises(ValueError): fixture.validate(value)
        value = public_config(); del value['httpUrls']['wrong-name']
        with self.assertRaises(ValueError): fixture.validate(value)

    def test_hdf_path_and_malformed_json_fail_without_printing_input(self):
        for invalid in ['relative.h5', 'https://host/fixture.h5', '/tmp/a\0.h5', '/tmp/a\n.h5', '/tmp/a".h5']:
            value = public_config(); value['hdf5Path'] = invalid
            with self.subTest(path=invalid), self.assertRaises(ValueError): fixture.validate(value)
        for invalid in ['{"password":"never-serialize-secret"', '[]', 'null']:
            with self.subTest(json=invalid), self.assertRaises(ValueError) as error: fixture.parse(invalid)
            self.assertNotIn('never-serialize-secret', str(error.exception))


class RuntimeFixtureSourceTransferTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'snapshot'
        self.consumer = Path(self.temporary.name) / 'consumer'
        self.output = Path(self.temporary.name) / 'evidence'
        self.root.mkdir(); self.consumer.mkdir()

    def generate(self, required):
        paths = [('DriverProfileRuntimeFixture.kt', 'commonTest'),
                 ('DriverProfileRuntimeFixtureJvm.kt', 'jvmTest'),
                 ('DriverProfileRuntimeFixtureNative.kt', 'nativeTest')]
        for filename, source_set in paths:
            destination = self.root / f'gdal4k-runtime/src/{source_set}/kotlin/dev/gdal4k' / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            text = (ROOT / 'verification/driver-profile' / filename).read_text(encoding='utf-8')
            text = text.replace('@RUNTIME_FIXTURES_REQUIRED@', 'true' if required else 'false')
            if not required:
                replacements = {'@HDF5_PATH@': '/fixed/fixture.h5', '@POSTGRESQL_DSN@': 'fixed-host-dsn',
                                '@HTTP_BASE@': 'http://127.0.0.1:18080', '@HTTPS_BASE@': 'https://127.0.0.1:18443',
                                '@UNTRUSTED_BASE@': 'https://127.0.0.1:18444', '@WRONG_NAME_BASE@': 'https://127.0.0.1:18445'}
                for key, value in replacements.items(): text = text.replace(key, value)
            destination.write_text(text, encoding='utf-8')

    def test_default_nonprofile_consumers_copy_nothing(self):
        self.assertEqual(fixture.copy_to_consumer(self.root, self.consumer, 'jvm'), [])
        self.assertEqual(list(self.consumer.iterdir()), [])

    def test_cross_native_keeps_runtime_reader_and_exact_source_receipts(self):
        self.generate(required=True)
        receipt = fixture.copy_to_consumer(self.root, self.consumer, 'linuxArm64')
        self.assertEqual(len(receipt), 2)
        self.assertEqual({x['consumer'].split('/')[1] for x in receipt}, {'commonTest', 'linuxArm64Test'})
        for item in receipt:
            original = (self.root / item['source']).read_bytes()
            actual = (self.consumer / item['consumer']).read_bytes()
            self.assertEqual(actual, original.replace(b'package dev.gdal4k\n', b'package consumer\n'))
            self.assertEqual(hashlib.sha256(original).hexdigest(), item['sourceSha256'])
            self.assertEqual(hashlib.sha256(actual).hexdigest(), item['consumerSha256'])
        common = next(x for x in receipt if '/commonTest/' in x['source'])
        text = (self.consumer / common['consumer']).read_text(encoding='utf-8')
        self.assertIn('if (!true)', text)
        self.assertIn('GDAL4K_TEST_DRIVER_FIXTURE', text)
        actual = next(x for x in receipt if '/nativeTest/' in x['source'])
        self.assertIn('platform.posix.getenv(name)', (self.consumer / actual['consumer']).read_text(encoding='utf-8'))
        fixture.preserve_consumer_sources(self.consumer, self.output, receipt)
        for item in receipt:
            self.assertEqual((self.output / item['preservedSource']).read_bytes(), (self.consumer / item['consumer']).read_bytes())

    def test_fixed_host_jvm_copies_only_jvm_actual_and_compiled_defaults(self):
        self.generate(required=False)
        receipt = fixture.copy_to_consumer(self.root, self.consumer, 'jvm')
        self.assertEqual({x['consumer'].split('/')[1] for x in receipt}, {'commonTest', 'jvmTest'})
        common = next(x for x in receipt if '/commonTest/' in x['source'])
        text = (self.consumer / common['consumer']).read_text(encoding='utf-8')
        self.assertIn('if (!false)', text)
        self.assertIn('"/fixed/fixture.h5", "fixed-host-dsn"', text)
        actual = next(x for x in receipt if '/jvmTest/' in x['source'])
        self.assertIn('System.getenv(name)', (self.consumer / actual['consumer']).read_text(encoding='utf-8'))

    def test_missing_actual_or_wrong_package_fails_before_partial_copy(self):
        self.generate(required=True)
        actual = self.root / 'gdal4k-runtime/src/nativeTest/kotlin/dev/gdal4k/DriverProfileRuntimeFixtureNative.kt'
        actual.unlink()
        with self.assertRaises(ValueError): fixture.copy_to_consumer(self.root, self.consumer, 'linuxArm64')
        self.assertEqual(list(self.consumer.iterdir()), [])
        self.generate(required=True)
        actual.write_text('package wrong\n', encoding='utf-8')
        with self.assertRaises(ValueError): fixture.copy_to_consumer(self.root, self.consumer, 'linuxArm64')
        self.assertEqual(list(self.consumer.iterdir()), [])

    def test_mutated_consumer_helper_cannot_be_preserved_as_original(self):
        self.generate(required=True)
        receipt = fixture.copy_to_consumer(self.root, self.consumer, 'jvm')
        (self.consumer / receipt[0]['consumer']).write_text('changed', encoding='utf-8')
        with self.assertRaises(ValueError): fixture.preserve_consumer_sources(self.consumer, self.output, receipt)


if __name__ == '__main__':
    unittest.main()
