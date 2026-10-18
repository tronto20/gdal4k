import concurrent.futures
import hashlib
import importlib.util
import io
import json
import os
from unittest.mock import patch
from pathlib import Path
import platform
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

PROJECT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('prepare_tool', PROJECT / 'src/main/resources/dev/tronto/buildtools/prepare_tool.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='build tools ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self, version='1.13.2'):
        archive = self.root / 'ninja.zip'
        # A tiny version-reporting executable tests installer behavior, not a real toolchain.
        script = '#!/bin/sh\nprintf "%s\\n" "' + version + '"\n'
        with zipfile.ZipFile(archive, 'w') as out:
            out.writestr('ninja', script)
        return dict(tool='ninja', version='1.13.2', host='linux-x64',
                    directory=str(self.root / 'store'), installation=str(self.root / 'store/1.13.2/linux-x64'),
                    url=archive.as_uri(), sha256=installer.digest(archive), revision='',
                    executable='ninja', git='git', offline=False, local=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX fixture executable; native Windows is tested with local tool configuration')
    def test_install_cache_offline_and_corruption(self):
        config = self.fixture()
        installer.install(config)
        dest = Path(config['installation'])
        before = (dest / installer.RECEIPT).stat().st_mtime_ns
        (self.root / 'ninja.zip').unlink()
        config['offline'] = True
        installer.install(config)
        self.assertEqual(before, (dest / installer.RECEIPT).stat().st_mtime_ns)
        (dest / 'ninja').write_text('damaged')
        with self.assertRaisesRegex(RuntimeError, 'Unverified tool directory'):
            installer.install(config)
        self.assertEqual((dest / 'ninja').read_text(), 'damaged')

    @unittest.skipIf(os.name == 'nt', 'POSIX fixture executable')
    def test_custom_existing_directory_is_read_only(self):
        config = self.fixture()
        installer.install(config)
        source = Path(config['installation'])
        (source / installer.RECEIPT).unlink()
        before = {p.name: installer.digest(p) for p in source.iterdir() if p.is_file()}
        config.update(local=True, offline=True)
        installer.install(config)
        self.assertEqual(before, {p.name: installer.digest(p) for p in source.iterdir() if p.is_file()})
        self.assertFalse((source / installer.RECEIPT).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX fixture executable')
    def test_wrong_local_version_rejected(self):
        config = self.fixture('1.13.1')
        existing = self.root / 'existing'
        existing.mkdir()
        with zipfile.ZipFile(self.root / 'ninja.zip') as z:
            z.extractall(existing)
        (existing / 'ninja').chmod(0o755)
        config.update(local=True, installation=str(existing))
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            installer.install(config)

    def test_sha_failure_is_atomic(self):
        config = self.fixture()
        config['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            installer.install(config)
        self.assertFalse(Path(config['installation']).exists())
        self.assertEqual([], list(Path(config['directory']).rglob('*.partial')))
        self.assertFalse(any(p.name.startswith('.linux-x64-install-') for p in Path(config['installation']).parent.iterdir()))

    def test_missing_sha_and_offline_fail_without_installation(self):
        config = self.fixture()
        config['sha256'] = ''
        with self.assertRaisesRegex(ValueError, 'SHA-256 is required'):
            installer.install(config)
        config['sha256'] = 'a' * 64
        config['offline'] = True
        with self.assertRaisesRegex(RuntimeError, 'Offline'):
            installer.install(config)
        self.assertFalse(Path(config['installation']).exists())

    def test_prerelease_is_not_an_exact_version_match(self):
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            installer.require_version({'tool': 'cmake', 'version': '4.4.4'}, 'cmake version 4.4.4-rc1')
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            installer.require_version({'tool': 'ninja', 'version': '1.13.2'}, '1.13.2.git')

    def test_windows_arm_sdk_is_explicitly_unsupported(self):
        config = self.fixture()
        config.update(tool='emscripten', host='windows-arm64')
        with self.assertRaisesRegex(RuntimeError, 'no native Windows ARM64 payload'):
            installer.install(config)
        self.assertFalse(Path(config['installation']).exists())

    def test_release_pins_cover_all_six_hosts(self):
        props = dict(line.split('=', 1) for line in (PROJECT / 'src/main/resources/dev/tronto/buildtools/versions.properties').read_text().splitlines() if line and not line.startswith('#'))
        for host in ('macos-x64', 'macos-arm64', 'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64'):
            for tool in ('cmake', 'ninja'):
                self.assertTrue(props[tool + '.' + host + '.archive'])
                self.assertRegex(props[tool + '.' + host + '.sha256'], '^[a-f0-9]{64}$')

    def test_vcpkg_direct_binary_all_hosts_and_no_bootstrap(self):
        for host in ('macos-x64', 'macos-arm64', 'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64'):
            with self.subTest(host=host):
                config = self.fixture()
                payload = self.root / 'direct-binary'
                payload.write_bytes(b'fixture executable')
                config.update(tool='vcpkg', host=host, executable='vcpkg.exe' if host.startswith('windows') else 'vcpkg',
                              installation=str(self.root / host), revision='a' * 40,
                              binary_version='2026-09-26', binary_url=payload.as_uri(), binary_sha256=installer.digest(payload))
                commands = []
                def checkout(config, stage):
                    (stage / 'scripts').mkdir()
                    (stage / 'scripts/vcpkg-tool-metadata.txt').write_text('VCPKG_TOOL_RELEASE_TAG=2026-09-26\n')
                def run(*args, **kwargs):
                    commands.append(args)
                    return 'vcpkg package management program version 2026-09-26'
                with patch.object(installer, 'git_checkout', checkout), patch.object(installer, 'run', run):
                    installer.install(config)
                self.assertEqual(1, len(commands))
                self.assertEqual('version', commands[0][1])
                destination = Path(config['installation'])
                self.assertEqual(payload.read_bytes(), (destination / config['executable']).read_bytes())
                with patch.object(installer, 'run', side_effect=[config['revision'], '']):
                    self.assertTrue(installer.valid_install(config, destination))
                receipt = json.loads((destination / installer.RECEIPT).read_text())
                for key in ('binary_version', 'binary_url', 'binary_sha256'):
                    receipt['identity'].pop(key)
                (destination / installer.RECEIPT).write_text(json.dumps(receipt))
                with patch.object(installer, 'run', side_effect=[config['revision'], '']):
                    self.assertTrue(installer.valid_install(config, destination), 'Verified legacy install must be reusable')
                (destination / config['executable']).write_bytes(b'corrupted')
                self.assertFalse(installer.valid_install(config, destination))

    def test_vcpkg_checksum_or_release_mismatch_is_atomic(self):
        config = self.fixture()
        config.update(tool='vcpkg', executable='vcpkg', binary_version='2026-09-26',
                      binary_url=(self.root / 'ninja.zip').as_uri(), binary_sha256='0' * 64)
        with patch.object(installer, 'git_checkout') as checkout:
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                installer.install(config)
            checkout.assert_not_called()
        config['binary_sha256'] = installer.digest(self.root / 'ninja.zip')
        def checkout(config, stage):
            (stage / 'scripts').mkdir()
            (stage / 'scripts/vcpkg-tool-metadata.txt').write_text('VCPKG_TOOL_RELEASE_TAG=wrong\n')
        with patch.object(installer, 'git_checkout', checkout), patch.object(installer, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'release mismatch'):
                installer.install(config)
            run.assert_not_called()
        self.assertFalse(Path(config['installation']).exists())

    def test_vcpkg_pins_cover_all_hosts(self):
        props = dict(line.split('=', 1) for line in (PROJECT / 'src/main/resources/dev/tronto/buildtools/versions.properties').read_text().splitlines() if line and not line.startswith('#'))
        for host in ('macos-x64', 'macos-arm64', 'linux-x64', 'linux-arm64', 'windows-x64', 'windows-arm64'):
            self.assertTrue(props['vcpkg.' + host + '.binaryAsset'])
            self.assertRegex(props['vcpkg.' + host + '.binarySha256'], '^[a-f0-9]{64}$')

    def test_archive_traversal_is_rejected(self):
        for name in ('../escape', '/escape', 'C:/escape', '..\\escape'):
            archive = self.root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr(name, 'no')
            with self.assertRaises(ValueError):
                installer.extract(archive, self.root / 'extract', True)
        archive = self.root / 'bad.tar.gz'
        with tarfile.open(archive, 'w:gz') as z:
            info = tarfile.TarInfo('../escape')
            info.size = 2
            z.addfile(info, io.BytesIO(b'no'))
        with self.assertRaises(ValueError):
            installer.extract(archive, self.root / 'extract', False)
        self.assertFalse((self.root / 'escape').exists())

    def test_escaping_symlink_rejected(self):
        archive = self.root / 'bad.tar.gz'
        with tarfile.open(archive, 'w:gz') as z:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = '../../escape'
            z.addfile(info)
        with self.assertRaisesRegex(ValueError, 'symlink escapes'):
            installer.extract(archive, self.root / 'extract', False)

    def test_git_revision_mismatch_does_not_replace_existing(self):
        existing = self.root / 'existing'
        existing.mkdir()
        subprocess.run(['git', 'init', str(existing)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['git', '-C', str(existing), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-m', 'fixture'], check=True, stdout=subprocess.DEVNULL)
        (existing / 'vcpkg').write_text('existing')
        config = self.fixture()
        config.update(tool='vcpkg', executable='vcpkg', installation=str(existing), revision='0' * 40, local=True)
        with self.assertRaisesRegex(ValueError, 'revision mismatch'):
            installer.install(config)
        self.assertEqual('existing', (existing / 'vcpkg').read_text())

    @unittest.skipIf(os.name == 'nt', 'POSIX fixture executable')
    def test_two_processes_share_one_installation(self):
        config = self.fixture()
        command = [os.sys.executable, str(PROJECT / 'src/main/resources/dev/tronto/buildtools/prepare_tool.py')]
        for key, value in config.items():
            if key not in ('local', 'offline'):
                command += ['--' + key, str(value)]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True), range(2)))
        self.assertTrue(all(result.returncode == 0 for result in results), results)
        self.assertTrue(installer.valid_install(config, Path(config['installation'])))


class GradlePluginTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('GRADLE_EXECUTABLE'), 'Run through build-logic check for Gradle functional verification')
    def test_default_store_is_shared_by_projects_in_selected_gradle_user_home(self):
        # Use the real Gradle User Home selected for this test run, avoiding any
        # assumptions about HOME or the location of the checked-out repository.
        with tempfile.TemporaryDirectory(prefix='shared tool projects ') as tmp:
            roots = [Path(tmp)/name for name in ['first project','second project']]
            locations = []
            for root in roots:
                root.mkdir()
                (root/'settings.gradle.kts').write_text('pluginManagement { includeBuild(' + json.dumps(str(PROJECT)) + ') }\nrootProject.name="shared-store-fixture"\n')
                (root/'build.gradle.kts').write_text('''
plugins { id("dev.tronto.buildtools.ninja") }
val home = gradle.gradleUserHomeDir.absolutePath
val store = ninjaTool.directory.get().asFile.absolutePath
val installation = ninjaTool.installationDirectory.get().asFile.absolutePath
tasks.register("locations") {
    val expectedHome = home
    val sharedStore = store
    val sharedInstallation = installation
    doLast {
        println("USER_HOME=" + expectedHome)
        println("TOOL_STORE=" + sharedStore)
        println("TOOL_LOCATION=" + sharedInstallation)
    }
}
''')
                command = (['cmd.exe','/d','/c'] if os.name == 'nt' else []) + [os.environ['GRADLE_EXECUTABLE'], '--gradle-user-home', os.environ['TEST_GRADLE_USER_HOME'], '-p', str(root), 'locations', '--offline', '--configuration-cache', '--max-workers=1', '--console=plain', '-Dorg.gradle.jvmargs=-Xmx256m']
                for attempt in range(2):
                    result = subprocess.run(command, text=True, capture_output=True)
                    self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                    values = dict(line.split('=',1) for line in result.stdout.splitlines() if line.startswith(('USER_HOME=','TOOL_STORE=','TOOL_LOCATION=')))
                    self.assertEqual((Path(values['USER_HOME'])/'build-tools/ninja').resolve(), Path(values['TOOL_STORE']).resolve())
                    if attempt:
                        self.assertIn('Reusing configuration cache', result.stdout)
                locations.append(values['TOOL_LOCATION'])
                self.assertFalse((root/'.gradle/ninja').exists())
            self.assertEqual(locations[0], locations[1])

    @unittest.skipUnless(os.environ.get('GRADLE_EXECUTABLE'), 'Run through build-logic check for Gradle functional verification')
    def test_lazy_configuration_custom_locations_and_configuration_cache(self):
        with tempfile.TemporaryDirectory(prefix='gradle tool fixture ') as tmp:
            root = Path(tmp)
            (root / 'settings.gradle.kts').write_text('pluginManagement { includeBuild(' + json.dumps(str(PROJECT)) + ') }\nrootProject.name="fixture"\n')
            (root / 'build.gradle.kts').write_text('''
plugins {
    id("dev.tronto.buildtools.cmake")
    id("dev.tronto.buildtools.ninja")
    id("dev.tronto.buildtools.emscripten")
    id("dev.tronto.buildtools.vcpkg")
}
cmakeTool { directory.set(layout.projectDirectory.dir("custom cmake")) }
ninjaTool { directory.set(layout.projectDirectory.dir("custom ninja")) }
emscriptenTool { directory.set(layout.projectDirectory.dir("custom emsdk")) }
vcpkgTool { directory.set(layout.projectDirectory.dir("custom vcpkg")) }
val locations = listOf(cmakeTool, ninjaTool, emscriptenTool, vcpkgTool).map { it.installationDirectory.get().asFile.absolutePath }
tasks.register("locations") {
    val paths = locations
    doLast { paths.forEach { println("TOOL_LOCATION=" + it) } }
}
''')
            command = (['cmd.exe', '/d', '/c'] if os.name == 'nt' else []) + [os.environ['GRADLE_EXECUTABLE'], '-p', str(root), 'locations', '--offline', '--configuration-cache', '--max-workers=1', '--console=plain', '-Dorg.gradle.jvmargs=-Xmx256m']
            first = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            # Windows TEMP can use an 8.3 alias while Gradle prints the long path.
            locations = [Path(line.removeprefix('TOOL_LOCATION=')).parents[1]
                         for line in first.stdout.splitlines() if line.startswith('TOOL_LOCATION=')]
            names = ('custom cmake', 'custom ninja', 'custom emsdk', 'custom vcpkg')
            self.assertEqual({os.path.normcase(str((root / name).resolve())) for name in names},
                             {os.path.normcase(str(path.resolve())) for path in locations})
            for name in names:
                self.assertFalse((root / name).exists(), 'Configuration must not install tools')
            second = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertIn('Reusing configuration cache', second.stdout)
            # A CLI override must also be observed through the task's managed properties.
            result = subprocess.run(command[:-6] + ['--dry-run', '-PbuildTools.cmake.directory=cli tools', '--console=plain'], text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
