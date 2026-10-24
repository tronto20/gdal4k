"""Cross-link the production MinGW target blocks with the existing pinned SDK.

The GDAL/CURL graph is a small model: all 45 Winsock imports observed in the
775e5e0b failed HTTP probe originate in a separate static dependency, after the
prefix import library. The real production CMake blocks, Clang/LLD, SDK import
archives and PE bytes decide which DLL supplies them. This does not build GDAL,
execute Windows target code, or promise direct CMake static-target consumption.

Windows requires the prepared tools. Other hosts skip only when the MinGW SDK
is absent. No download/bootstrap is allowed. Windows evidence is retained in
verification/results/windows-winsock-link-selfcheck; an explicit local output
can be selected with GDAL4K_WINSOCK_LINK_RESULTS.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from command_evidence import run_with_evidence
from windows_pe import inspect, validate_system_dependencies
from winpthread_handoff_probe import existing_tools


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'gdal4k-engine/tools'
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location('winsock_producer', TOOLS / 'build-engine.py')
producer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(producer)
SPEC = importlib.util.spec_from_file_location('winsock_toolchains', TOOLS / 'toolchains.py')
toolchains = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolchains)

# Exact named imports from current775 probe c2eebf260e0ea4a9849e53f8f4b4a114
# f3a00c6e67bcb57a7030c48b0b536425 (source artifact12567122256). All are declared
# by the real SDK headers; no fabricated import library or DLL patch is used.
FUNCTIONS = frozenset('''WSACleanup WSAGetLastError WSASetLastError WSAStartup
__WSAFDIsSet accept bind closesocket connect gethostbyaddr gethostbyname
gethostname getpeername getservbyname getservbyport getsockname getsockopt htonl
htons inet_addr inet_ntoa ioctlsocket listen ntohl ntohs recv recvfrom select send
sendto setsockopt shutdown socket WSACloseEvent WSACreateEvent WSAEnumNetworkEvents
WSAEventSelect WSAIoctl WSAResetEvent WSASetEvent WSASocketA WSAWaitForMultipleEvents
freeaddrinfo getaddrinfo getnameinfo'''.split())
SDK_LIBRARIES = {
    'x86_64-w64-mingw32/lib/libwsock32.a': '8aaaaefbb96690ff893eadbed771d7e9b1513891bf34e5a2c502966a60318765',
    'x86_64-w64-mingw32/lib/libws2_32.a': 'b2558b4f886f1e4022f34ec29a8d05dde3fa8813c3b0bb5c260a20e92f82166b',
    'x86_64-w64-mingw32/lib/libwinpthread.a': '4821b991241355b6841d55f53e17c98da9334d2c2ac9914f371567b71b321a70',
}
RUNTIME = ['-static-libgcc', '-static-libstdc++', '-Wl,-Bstatic', '-lwinpthread', '-Wl,-Bdynamic']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def production_blocks(source):
    """Keep complete production branches; exclude JNI/resource/package setup."""
    begin = source.index('    add_library(gdal4k SHARED ')
    creation = source[begin:source.index('    if(NOT ANDROID', begin)]
    begin = source.index('target_compile_features(gdal4k PRIVATE cxx_std_17)')
    common = source[begin:source.index('\nif(GDAL4K_DEPENDENCIES)', begin)]
    for block in [creation, common]:
        depth = 0
        for line in block.splitlines():
            if re.match(r'\s*if\s*\(', line):
                depth += 1
            elif re.match(r'\s*endif\s*\(', line):
                depth -= 1
                if depth < 0:
                    raise AssertionError('Production CMake extraction is unbalanced')
        if depth:
            raise AssertionError('Production CMake extraction omitted a branch')
    return creation, common


def winsock_functions(data):
    """Read named IAT functions as well as the production module-name audit."""
    inspect(data)
    pe = struct.unpack_from('<I', data, 60)[0]
    optional = pe + 24
    count = struct.unpack_from('<H', data, pe + 6)[0]
    size = struct.unpack_from('<H', data, pe + 20)[0]
    sections = []
    for index in range(count):
        offset = optional + size + index * 40
        virtual, rva, raw_size, raw = struct.unpack_from('<IIII', data, offset + 8)
        sections.append((rva, max(virtual, raw_size), raw, raw_size))

    def location(rva, width=1):
        for start, span, raw, stored in sections:
            if start <= rva and rva + width <= start + span and rva - start + width <= stored:
                return raw + rva - start, raw + stored
        raise AssertionError('IAT reference exceeds actual section bytes')

    def string(rva):
        at, end = location(rva)
        stop = data.find(b'\0', at, min(end, at + 1024))
        if stop < at:
            raise AssertionError('Unterminated IAT name')
        return data[at:stop].decode('ascii')

    rva, size = struct.unpack_from('<II', data, optional + 120)
    result = {}
    for offset in range(0, size, 20):
        at, _ = location(rva + offset, 20)
        fields = struct.unpack_from('<IIIII', data, at)
        if not any(fields):
            return result
        module = string(fields[3]).lower()
        if module not in ('wsock32.dll', 'ws2_32.dll'):
            continue
        lookup = fields[0] or fields[4]
        names = set()
        for index in range(len(data) // 8):
            at, _ = location(lookup + index * 8, 8)
            value = struct.unpack_from('<Q', data, at)[0]
            if not value:
                break
            if value >> 63:
                raise AssertionError('Expected named Winsock imports')
            names.add(string(value + 2))
        else:
            raise AssertionError('Unterminated IAT lookup table')
        if module in result:
            raise AssertionError('Duplicate Winsock import descriptor')
        result[module] = names
    raise AssertionError('Unterminated PE import descriptors')


class WindowsWinsockLinkTest(unittest.TestCase):
    def setUp(self):
        if 'KONAN_DATA_DIR' in os.environ:
            roots = [Path(os.environ['KONAN_DATA_DIR'])]
        else:
            roots = [Path.home() / '.konan', ROOT.parent / '.cache/gdal4k/konan']
        installed = [root for root in roots if (root / 'dependencies/msys2-mingw-w64-x86_64-2').is_dir()]
        if not installed:
            if os.name == 'nt':
                raise RuntimeError('Windows requires the already prepared pinned MinGW SDK')
            raise unittest.SkipTest('Existing pinned MinGW SDK is absent; no download/bootstrap')
        dependencies = installed[0] / 'dependencies'
        host = {'Windows': 'windows', 'Linux': 'linux', 'Darwin': 'macos'}[platform.system()]
        suffix = '.exe' if os.name == 'nt' else ''
        compilers = sorted(dependencies.glob(f'llvm-21-x86_64-{host}-*/bin/clang++{suffix}'))
        if len(compilers) != 1:
            raise RuntimeError('Installed SDK requires exactly one prepared Clang21 compiler')
        self.compiler, self.linker, self.sdk = existing_tools(compilers[0], dependencies / 'msys2-mingw-w64-x86_64-2')
        metadata_path = ROOT / 'build/build-tools/native.json'
        metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
        self.cmake = Path(metadata.get('CMAKE_COMMAND') or shutil.which('cmake') or '')
        self.ninja = Path(metadata.get('NINJA_COMMAND') or shutil.which('ninja') or '')
        if not self.cmake.is_file() or not self.ninja.is_file():
            raise RuntimeError('Prepared CMake and Ninja are required; no tool installer is called')

    def command(self, *arguments, cwd=None):
        args = [str(value) for value in arguments]
        with (self.output / 'command-console.log').open('a', encoding='utf-8') as console, contextlib.redirect_stdout(console):
            record_path = run_with_evidence(args, cwd=cwd or self.output,
                                            directory=self.output / 'commands', timeout=120)
        record = json.loads(record_path.read_text())
        log = record_path.parent / record['log']
        self.assertEqual('passed', record['status'])
        self.assertEqual(0, record['returncode'])
        self.assertTrue(record['outputComplete'])
        self.assertEqual(record['logSha256'], sha(log))
        if os.name == 'nt':
            self.assertEqual(dict(kind='Windows Job Object', assignedBeforeResume=True, killOnClose=True), record['ownership'])
            self.assertEqual(dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False), record['cleanup'])
        self.receipt['commands'].append(dict(path=str(record_path.relative_to(self.output)), sha256=sha(record_path)))
        return log.read_text(encoding='utf-8')

    def assert_image(self, path, old):
        data = path.read_bytes()
        pe = inspect(data)
        functions = winsock_functions(data)
        self.assertEqual(FUNCTIONS, set().union(*functions.values()))
        self.assertIn('ws2_32.dll', functions)
        if old:
            self.assertIn('wsock32.dll', functions)
            with self.assertRaisesRegex(ValueError, 'wsock32.dll'):
                validate_system_dependencies(pe['imports']['all'])
        else:
            self.assertNotIn('wsock32.dll', functions)
            validate_system_dependencies(pe['imports']['all'])
        return dict(path=str(path.relative_to(self.output)), sha256=sha(path), bytes=len(data), pe=pe,
                    winsockFunctions={key: sorted(value) for key, value in functions.items()})

    def build_variant(self, name, creation, common):
        directory = self.output / name
        source, build = directory / 'source with spaces', directory / 'build with spaces'
        source.mkdir(parents=True)
        # The SDK headers supply all actual signatures. The volatile array keeps
        # every imported function live, even if the compiler optimizes the loop.
        required = '#include <winsock2.h>\n#include <ws2tcpip.h>\nstatic void (*volatile required[])() = {\n'
        required += ''.join('reinterpret_cast<void(*)()>(&' + function + '),\n' for function in sorted(FUNCTIONS))
        required += '};\nextern "C" int required_imports() { for(auto function: required) if(!function) return 1; return 0; }\n'
        (source / 'required.cpp').write_text(required)
        (source / 'gdal4k.cpp').write_text('extern "C" int required_imports();\n#ifdef GDAL4K_STATIC\n#define API\n#else\n#define API __declspec(dllexport)\n#endif\nextern "C" API int bridge() { return required_imports(); }\n')
        (source / 'http-cache-probe.cpp').write_text('extern "C" int required_imports(); int main() { return required_imports(); }\n')
        (source / 'consumer.cpp').write_text('extern "C" int bridge(); int main() { return bridge(); }\n')
        (source / 'resources.cpp').write_text('int fixture_resource=0;\n')
        package = source / 'mock-installed/share/curl'
        package.mkdir(parents=True)
        (package / 'CURLConfig.cmake').write_text('add_library(CURL::libcurl INTERFACE IMPORTED)\nset_target_properties(CURL::libcurl PROPERTIES INTERFACE_LINK_LIBRARIES ws2_32)\n')
        (source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.22)
project(ProductionWinsockTargetBranches CXX)
add_library(observed_dependency STATIC required.cpp)
add_library(GDAL::GDAL INTERFACE IMPORTED)
set_target_properties(GDAL::GDAL PROPERTIES INTERFACE_LINK_LIBRARIES "observed_dependency;wsock32")
set(GDAL4K_DEPENDENCIES "${CMAKE_CURRENT_SOURCE_DIR}/mock-installed")
set(GDAL4K_RESOURCE_CPP "${CMAKE_CURRENT_SOURCE_DIR}/resources.cpp")
set(GDAL4K_ENABLE_HTTP ON)
''' + creation + common + '''
file(WRITE "${CMAKE_BINARY_DIR}/identity.txt" "${CMAKE_CXX_COMPILER_ID};${CMAKE_CXX_COMPILER_FRONTEND_VARIANT};${WIN32};${MINGW}")
''')
        toolchain = directory / 'toolchain.cmake'
        toolchain.write_text(f'''set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)
set(CMAKE_CXX_COMPILER "{self.compiler.as_posix()}")
set(CMAKE_CXX_COMPILER_TARGET x86_64-w64-windows-gnu)
set(CMAKE_SYSROOT "{self.sdk.as_posix()}")
set(CMAKE_CXX_FLAGS_INIT "--target=x86_64-w64-windows-gnu --sysroot={self.sdk.as_posix()} -femulated-tls -isystem \\\"{self.overlay.as_posix()}\\\"")
set(CMAKE_EXE_LINKER_FLAGS_INIT "--ld-path={self.linker.as_posix()} -pthread")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "--ld-path={self.linker.as_posix()} -pthread")
set(CMAKE_AR "{self.compiler.with_name('llvm-ar.exe' if os.name == 'nt' else 'llvm-ar').as_posix()}")
''')
        self.command(self.cmake, '-S', source, '-B', build, '-G', 'Ninja',
                     '-DCMAKE_MAKE_PROGRAM=' + str(self.ninja), '-DCMAKE_TOOLCHAIN_FILE=' + str(toolchain))
        self.command(self.cmake, '--build', build, '--verbose')
        self.assertEqual('Clang;GNU;1;1', (build / 'identity.txt').read_text())
        database = json.loads(self.command(self.ninja, '-C', build, '-t', 'compdb', '-x'))
        links = {}
        for output in ['gdal4k.dll', 'gdal4k_http_probe.exe']:
            entries = [entry for entry in database if Path(entry['directory']).resolve() == build.resolve()
                       and (build / entry['output']).resolve() == (build / output).resolve()]
            self.assertEqual(1, len(entries))
            arguments = producer.link_arguments(entries[0]['command'])
            self.assertFalse(any(value.startswith('@') for value in arguments))
            for value in RUNTIME:
                self.assertIn(value, arguments)
            links[output] = dict(command=entries[0]['command'], arguments=arguments)
        # This is the unchanged producer's system-option extraction expression,
        # applied to actual expanded Ninja output (not a synthetic argv).
        options = list(dict.fromkeys(value for value in links['gdal4k.dll']['arguments']
                                     if re.fullmatch(r'-l[\w.+-]+', value)))
        options = [value for value in options if value not in ['-lwinpthread', '-lpthread', '-lstdc++']]
        self.assertEqual('-lwsock32' if name == 'old' else '-lws2_32',
                         next(value for value in options if value in ['-lwsock32', '-lws2_32']))
        if name != 'old':
            command = links['gdal4k.dll']['arguments']
            archive_at = next(i for i, value in enumerate(command) if value.endswith('libobserved_dependency.a'))
            self.assertLess(command.index('-lws2_32'), archive_at)
        result = dict(name=name, cmakeIdentity=(build / 'identity.txt').read_text(),
                      links=links, sharedExtractedSystemOptions=options, images=[])
        for output in ['gdal4k.dll', 'gdal4k_http_probe.exe']:
            result['images'].append(self.assert_image(build / output, name == 'old'))
        # Model the published combined archive from the actual objects, then
        # use shared-derived argv as Native/Arrow consumers do. No direct CMake
        # consumption of the uninstalled gdal4k_static target is promised.
        archive = directory / 'merged-native.a'
        objects = [build / 'CMakeFiles/gdal4k_static.dir/gdal4k.cpp.obj',
                   build / 'CMakeFiles/gdal4k_static.dir/resources.cpp.obj',
                   build / 'CMakeFiles/observed_dependency.dir/required.cpp.obj']
        ar = self.compiler.with_name('llvm-ar.exe' if os.name == 'nt' else 'llvm-ar')
        self.command(ar, 'rcs', archive, *objects)
        program = directory / 'native-arrow-style-consumer.exe'
        self.command(self.compiler, '--target=x86_64-w64-windows-gnu', '--sysroot=' + str(self.sdk),
                     '--ld-path=' + str(self.linker), '-pthread', source / 'consumer.cpp', archive,
                     *options, *RUNTIME, '-o', program)
        result['images'].append(self.assert_image(program, name == 'old'))
        result['mergedArchive'] = dict(path=str(archive.relative_to(self.output)), sha256=sha(archive))
        return result

    def test_actual_cmake_and_late_static_imports_select_winsock2(self):
        explicit = os.environ.get('GDAL4K_WINSOCK_LINK_RESULTS')
        if explicit or os.name == 'nt':
            self.output = Path(explicit).resolve() if explicit else ROOT / 'verification/results/windows-winsock-link-selfcheck'
            self.output.mkdir(parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory(prefix='gdal4k winsock link ')
            self.addCleanup(temporary.cleanup)
            self.output = Path(temporary.name)
        source = (ROOT / 'gdal4k/native/CMakeLists.txt').read_text()
        creation, common = production_blocks(source)
        inputs = {name: sha(self.sdk / name) for name in SDK_LIBRARIES}
        self.assertEqual(SDK_LIBRARIES, inputs)
        for name in ['x86_64-w64-mingw32/include/winsock2.h', 'x86_64-w64-mingw32/include/ws2tcpip.h',
                     'x86_64-w64-mingw32/include/intrin.h', 'include/c++/9.2.0/bits/fs_path.h',
                     'lib/gcc/x86_64-w64-mingw32/9.2.0/libstdc++.a',
                     'lib/gcc/x86_64-w64-mingw32/9.2.0/libgcc.a']:
            inputs[name] = sha(self.sdk / name)
        self.receipt = dict(schemaVersion=1, scope=__doc__, sourceHeadIndependent=True,
                            sourceCmakeSha256=hashlib.sha256(source.encode()).hexdigest(),
                            testSourceSha256=sha(__file__), sdk=dict(path=str(self.sdk), inputSha256=inputs),
                            helperSourceSha256={name: sha(ROOT / name) for name in
                                               ['gdal4k-engine/tools/build-engine.py', 'gdal4k-engine/tools/toolchains.py',
                                                'verification/windows_pe.py', 'verification/command_evidence.py']},
                            tools={name: dict(path=str(path), sha256=sha(path)) for name, path in
                                   [('clang', self.compiler), ('lld', self.linker), ('cmake', self.cmake), ('ninja', self.ninja)]},
                            status='running', commands=[], variants=[], actualWindowsTargetExecution=False,
                            actualKotlinNativeCompilation=False, fullGdalBuild=False, accepted=False)
        try:
            version = self.command(self.compiler, '--version')
            self.assertRegex(version, r'^clang version 21\.')
            self.overlay = toolchains.mingw_header_overlay(self.sdk, self.output / 'compiler headers')
            for name in ['gcc-libs/COPYING3', 'gcc-libs/COPYING.RUNTIME', 'gcc-libs/COPYING.LIB',
                         'libwinpthread/COPYING', 'libwinpthread/mingw-w64-libraries/winpthreads/COPYING']:
                destination = self.output / 'licenses' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((self.sdk / 'share/licenses' / name).read_bytes())
            self.receipt['preservedLicenses'] = {str(p.relative_to(self.output)): sha(p)
                                                 for p in (self.output / 'licenses').rglob('*') if p.is_file()}
            self.receipt['privateHeaders'] = {str(p.relative_to(self.output)): sha(p)
                                              for p in self.overlay.rglob('*') if p.is_file()}
            (self.output / 'production-creation.cmake').write_text(creation)
            (self.output / 'production-common-http.cmake').write_text(common)
            # Remove only the two new prefix selections for a causal old-style
            # control; keep all real runtime flags and GDAL/CURL link statements.
            old_creation, count = re.subn(r'(?m)^\s*target_link_libraries\(gdal4k PRIVATE ws2_32\)\n', '', creation)
            self.assertEqual(1, count)
            old_common, count = re.subn(r'(?m)^\s*target_link_libraries\(gdal4k_http_probe PRIVATE ws2_32\)\n', '', common)
            self.assertEqual(1, count)
            self.assertEqual(45, len(FUNCTIONS))
            self.receipt['variants'].append(self.build_variant('old', old_creation, old_common))
            self.receipt['variants'].append(self.build_variant('current', creation, common))
            self.assertEqual(inputs, {name: sha(self.sdk / name) for name in inputs})
            self.receipt.update(status='passed', sdkInputsUnchanged=True)
        except Exception as error:
            self.receipt.update(status='failed', error=dict(type=type(error).__name__, message=str(error)))
            raise
        finally:
            (self.output / 'selfcheck.json').write_text(json.dumps(self.receipt, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
