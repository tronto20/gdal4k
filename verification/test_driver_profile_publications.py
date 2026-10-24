"""Publication byte-identity tests; synthetic payloads are never runtime evidence."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('profile_publication_subject', ROOT / 'verification/driver-profile/report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as output:
        for name, data in members.items():
            output.writestr(name, data)


class Publications:
    def __init__(self, root, target, native_engine=None):
        self.snapshot, self.repo = root / 'source', root / 'maven'
        self.target, self.version = target, '0.1.0-combined-SNAPSHOT'
        native = {'windows-amd64':'mingwx64', 'linux-amd64':'linuxx64',
                  'linux-arm64':'linuxarm64', 'macos-arm64':'macosarm64'}[target]
        suffix = {'windows-amd64':'mingw-x64', 'linux-amd64':'linux-x64-gnu',
                  'linux-arm64':'linux-arm64-gnu', 'macos-arm64':'macos-arm64'}[target]
        native_engine = native_engine or 'gdal4k-engine-native-' + suffix
        classifier = {'windows-amd64':'windows-x64', 'linux-amd64':'linux-x64-gnu',
                      'linux-arm64':'linux-arm64-gnu', 'macos-arm64':'macos-arm64'}[target]
        library = ('gdal4k.dll' if target.startswith('windows') else
                   'libgdal4k.dylib' if target.startswith('macos') else 'libgdal4k.so')
        self.base = self.repo / 'dev/tronto/gdal4k'
        coordinates = {module + suffix for module in ['gdal4k', 'gdal4k-runtime']
                       for suffix in ['', '-jvm', '-' + native]} | {'gdal4k-engine', native_engine}
        ns = '{http://maven.apache.org/POM/4.0.0}'
        for coordinate in coordinates:
            pom = self.base / coordinate / self.version / f'{coordinate}-{self.version}.pom'
            pom.parent.mkdir(parents=True, exist_ok=True)
            xml = ET.Element(ns + 'project')
            for key, value in [('groupId','dev.tronto.gdal4k'), ('artifactId',coordinate), ('version',self.version)]:
                ET.SubElement(xml, ns + key).text = value
            dependencies = ET.SubElement(xml, ns + 'dependencies')
            dependency = ET.SubElement(dependencies, ns + 'dependency')
            for key, value in [('groupId','dev.tronto.gdal4k'), ('artifactId','gdal4k'), ('version',self.version)]:
                ET.SubElement(dependency, ns + key).text = value
            ET.ElementTree(xml).write(pom)
        self.jars = {}
        for module in ['gdal4k', 'gdal4k-runtime']:
            data = b'unit-only class identity: ' + module.encode()
            member = 'dev/gdal4k/Unit.class'
            compiled = self.snapshot / module / 'build/classes/kotlin/jvm/main' / member
            compiled.parent.mkdir(parents=True, exist_ok=True); compiled.write_bytes(data)
            self.jars[module] = self.base / (module + '-jvm') / self.version / f'{module}-jvm-{self.version}.jar'
            archive(self.jars[module], {member:data})
        self.resource = f'gdal4k/{target}/{library}'
        self.jni = b'unit-only JNI payload identity'
        self.native = b'unit-only static archive identity'
        self.engine_jar = self.base / 'gdal4k-engine' / self.version / f'gdal4k-engine-{self.version}-{classifier}.jar'
        archive(self.engine_jar, {self.resource:self.jni})
        self.klib = self.base / native_engine / self.version / f'{native_engine}-{self.version}-cinterop-engine.klib'
        self.included = 'default/targets/unit/included/libgdal4k.a'
        archive(self.klib, {self.included:self.native, 'default/manifest':b'unique_name=unit-only\n'})
        self.artifacts = {'jvm-resources/' + self.resource:digest(self.jni),
                          f'engine/{target}/libgdal4k.a':digest(self.native)}

    def verify(self):
        return REPORT.publication_bindings(self.snapshot, self.repo, self.version, self.target, self.artifacts)


class PublicationBindingsTests(unittest.TestCase):
    def test_four_hosts_bind_actual_container_members_to_current_producer_bytes(self):
        for target in ['windows-amd64', 'linux-amd64', 'linux-arm64', 'macos-arm64']:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                fixture = Publications(Path(temporary), target)
                result = fixture.verify()
                self.assertEqual(8, len(result['coordinates']))
                self.assertEqual(digest(fixture.jni), result['jniSha256'])
                self.assertEqual(digest(fixture.native), result['nativeSha256'])
                self.assertEqual(2, len(result['compiledProductionSha256']))
                if target == 'windows-amd64':
                    self.assertIn('gdal4k-engine-native-mingw-x64', result['coordinates'])
                    self.assertIn('windows-x64.jar', fixture.engine_jar.name)

    def test_jvm_windows_classifier_is_rejected_as_a_native_engine_coordinate(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Publications(Path(temporary), 'windows-amd64', 'gdal4k-engine-native-windows-x64')
            with self.assertRaises(AssertionError):
                fixture.verify()

    def test_changed_packaged_classes_jni_and_native_archive_are_rejected(self):
        for changed in ['classes', 'jni', 'native']:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                fixture = Publications(Path(temporary), 'windows-amd64')
                if changed == 'classes':
                    archive(fixture.jars['gdal4k'], {'dev/gdal4k/Unit.class':b'changed packaged class'})
                elif changed == 'jni':
                    archive(fixture.engine_jar, {fixture.resource:b'changed packaged JNI'})
                else:
                    archive(fixture.klib, {fixture.included:b'changed static archive', 'default/manifest':b'unique_name=unit-only\n'})
                with self.assertRaises(AssertionError):
                    fixture.verify()


if __name__ == '__main__':
    unittest.main()
