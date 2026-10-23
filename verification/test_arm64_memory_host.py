"""Pure reporter host/class/environment checks; no Gradle, JVM or native execution."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


combined = load('arm64_combined_memory_report', 'combined-driver-memory-report.py')
http = load('arm64_http_memory_report', 'http-memory-report.py')
rss = load('arm64_memory_rss', 'window-memory-report.py')


class MemoryHostTest(unittest.TestCase):
    def test_three_supported_hosts_bind_target_classifier_library_and_rss_units(self):
        examples = [
            ('Linux', 'x86_64', 'linux-amd64', 'LinuxX64', 'linux-x64-gnu', 'libgdal4k.so', '-v', 'KiB', 1024),
            ('Linux', 'aarch64', 'linux-arm64', 'LinuxArm64', 'linux-arm64-gnu', 'libgdal4k.so', '-v', 'KiB', 1024),
            ('Darwin', 'arm64', 'macos-arm64', 'MacosArm64', 'macos-arm64', 'libgdal4k.dylib', '-l', 'bytes', 1),
        ]
        for reporter in (combined, http):
            for system, machine, target, gradle, classifier, library, flag, unit, multiplier in examples:
                with self.subTest(reporter=reporter.__name__, host=(system, machine)):
                    settings = reporter.host_settings({'target': target}, system, machine)
                    self.assertEqual((target, gradle, classifier, library, flag, unit, multiplier),
                                     tuple(settings[key] for key in ('target', 'gradleTarget', 'classifier',
                                           'library', 'flag', 'reportedRssUnit', 'rssMultiplierToBytes')))
                    raw = ('Maximum resident set size (kbytes): 2048\n' if system == 'Linux' else
                           '    2097152  maximum resident set size\n')
                    self.assertEqual(2097152, rss.peak_rss_bytes(raw, system))

    def test_unknown_architectures_and_mismatched_manifest_never_fall_back_to_x64(self):
        invalid = [('Linux', 'armv7l', 'linux-amd64'), ('Linux', 'arm64', 'linux-arm64'),
                   ('Linux', 'ppc64le', 'linux-amd64'), ('Darwin', 'x86_64', 'macos-arm64'),
                   ('Windows', 'AMD64', 'windows-amd64'), ('FreeBSD', 'aarch64', 'linux-arm64'),
                   ('Linux', 'aarch64', 'linux-amd64'), ('Linux', 'x86_64', 'linux-arm64'),
                   ('Darwin', 'arm64', 'linux-arm64')]
        for reporter in (combined, http):
            for system, machine, target in invalid:
                with self.subTest(reporter=reporter.__name__, host=(system, machine), target=target):
                    with self.assertRaises(ValueError):
                        reporter.host_settings({'target': target}, system, machine)


class ClasspathEnvironmentTest(unittest.TestCase):
    def test_normal_gradle_classpath_tasks_inherit_verified_staged_engine_property(self):
        property_name = 'ORG_GRADLE_PROJECT_gdal4kVerifiedStagedEngine'
        child_environment = {property_name: 'linux-arm64', 'GDAL4K_TEST_DRIVER_FIXTURE': '{"test":"public"}',
                             'PATH': '/owned/jdk/bin:/usr/bin', 'JAVA_HOME': '/owned/jdk'}
        for reporter, task, init in (
            (combined, ':gdal4k-runtime:writeCombinedDriverMemoryClasspath', 'combined-driver-memory.init.gradle'),
            (http, ':gdal4k-runtime:writeHttpMemoryClasspath', 'http-memory.init.gradle'),
        ):
            with self.subTest(reporter=reporter.__name__), \
                    patch.dict(os.environ, child_environment, clear=True), \
                    patch.object(reporter.subprocess, 'run') as run:
                reporter.prepare_classpath()
                run.assert_called_once_with(
                    [str(reporter.ROOT / 'gradlew'), '--console=plain', task, '-I',
                     str(reporter.ROOT / 'verification' / init), '--no-configuration-cache'],
                    cwd=reporter.ROOT, env=child_environment, check=True)
                self.assertIsNot(os.environ, run.call_args.kwargs['env'])
                self.assertEqual(child_environment, dict(os.environ))


class CombinedClassInventoryTest(unittest.TestCase):
    required = ['CombinedDriverMemoryProbe.class', 'DriverProfileApiTestKt.class',
                'DriverProfileFixtures.class', 'DriverProfileRuntimeFixture.class',
                'DriverProfileRuntimeFixtureKt.class', 'DriverProfileRuntimeFixtureJvmKt.class']

    def test_runtime_fixture_actual_common_and_lazy_nested_classes_are_bound(self):
        names = self.required + ['DriverProfileRuntimeFixtureKt$driverProfileRuntimeFixture$2.class',
                                 'DriverProfileApiTestKt$combinedRoundTrip$1.class',
                                 'DatasetApiTestKt.class', 'DatasetOutputPathKt.class']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in names + ['HttpMemoryProbe.class', 'UnrelatedTest.class']:
                (root / name).write_bytes(b'unit fixture, not executable bytecode')
            (root / 'DriverProfileRuntimeFixture.kt').write_text('source is not class evidence', encoding='utf-8')
            actual = combined.compiled_probe_classes(root)
            self.assertEqual(sorted(names), [path.name for path in actual])

    def test_missing_runtime_configuration_or_actual_class_cannot_silently_pass_inventory(self):
        for missing in ('DriverProfileRuntimeFixture.class', 'DriverProfileRuntimeFixtureKt.class',
                        'DriverProfileRuntimeFixtureJvmKt.class'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in self.required:
                    if name != missing:
                        (root / name).write_bytes(b'unit fixture, not executable bytecode')
                with self.assertRaisesRegex(ValueError, missing.replace('.', r'\.')):
                    combined.compiled_probe_classes(root)


if __name__ == '__main__':
    unittest.main()
