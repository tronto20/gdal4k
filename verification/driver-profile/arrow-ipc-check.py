"""Run IPC variadic/prebuffer regression with the packaged archive's exact C++ toolchain."""
import datetime
import hashlib
import json
from pathlib import Path
import platform
import re
import shlex
import struct
import subprocess
import sys

STAGED_TARGET = 'linux-arm64'
MARKER = 'GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK'
CASES = '16 repeats x coalesce/zero-copy x prebuffer/on-off x4 variadic StringView batches; async reentrant generator, reader lifetime, checked-overflow helpers'


def compiler_linker_options(options):
    """Expand Native's grouped framework pairs for a compiler's argv contract."""
    assert isinstance(options, list) and all(
        isinstance(option, str) and option and '\0' not in option for option in options
    ), 'Invalid Native linker options'
    arguments = []
    for option in options:
        if option.startswith('-framework'):
            framework = re.fullmatch(r'-framework ([A-Za-z][A-Za-z0-9_]+)', option)
            assert framework, 'Invalid grouped framework option'
            # Keep repeated -framework tokens and preserve other options verbatim,
            # including paths containing spaces. These are argv, not shell input.
            arguments.extend(['-framework', framework.group(1)])
        else:
            arguments.append(option)
    return arguments


def compiler_from_cmake(cache, directory):
    """A toolchain may set a normal CMake variable instead of a cache entry."""
    cached = cache.get('CMAKE_CXX_COMPILER')
    candidates = list((directory / 'CMakeFiles').glob('*/CMakeCXXCompiler.cmake'))
    version = '.'.join(cache.get('CMAKE_CACHE_' + name + '_VERSION', '') for name in ['MAJOR', 'MINOR', 'PATCH'])
    selected = directory / 'CMakeFiles' / version / 'CMakeCXXCompiler.cmake'
    if selected.is_file():
        candidates = [selected]
    compiled = None
    if candidates:
        assert len(candidates) == 1, 'Ambiguous CMake compiler metadata'
        match = re.search(r'^set\(CMAKE_CXX_COMPILER "([^"\n]+)"\)', candidates[0].read_text(), re.M)
        assert match, 'Missing compiler in CMake compiler metadata'
        compiled = match.group(1)
    assert cached or compiled, 'Missing exact engine C++ compiler'
    if cached and compiled:
        assert Path(cached).resolve() == Path(compiled).resolve(), 'CMake compiler metadata mismatch'
    compiler = Path(cached or compiled)
    assert compiler.is_absolute() and compiler.is_file(), 'Invalid exact engine C++ compiler'
    return str(compiler)


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _owned(root, path, required=True):
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    if '..' in path.parts or not path.is_relative_to(root):
        raise ValueError('IPC staged path must stay in its source workspace: ' + str(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('IPC staged paths must not use symlinks: ' + str(parent))
        if parent == root:
            break
    if required and not path.is_file():
        raise ValueError('Missing IPC staged file: ' + str(path))
    return path


def _arm_executable(path):
    with path.open('rb') as stream:
        header = stream.read(64)
    if (len(header) < 64 or header[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', header, 16)[0] not in (2, 3)
            or struct.unpack_from('<HI', header, 18) != (183, 1)
            or struct.unpack_from('<H', header, 52)[0] != 64):
        raise ValueError('IPC staged executable must be ELF64 little-endian AArch64')


def _original_archive(root, target):
    engine = root / 'gdal4k-engine/build/engine' / target
    archive = _owned(root, engine / 'libgdal4k.a')
    manifest_path = _owned(root, engine / 'manifest.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    name = archive.relative_to(root / 'gdal4k-engine/build').as_posix()
    digest = _sha(archive)
    if (manifest.get('target') != target or manifest.get('gdal') != '3.12.4'
            or manifest.get('jxl') is not True
            or not re.fullmatch(r'[0-9a-f]{64}', manifest.get('source_fingerprint', ''))
            or manifest.get('artifacts', {}).get(name) != digest):
        raise ValueError('IPC static archive differs from the original target engine manifest')
    return archive, manifest_path, manifest, digest


def _invoke(command, root, timeout):
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        value = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
        stdout, stderr, code, expired, error, started_process = value.stdout, value.stderr, value.returncode, False, None, True
    except subprocess.TimeoutExpired as failure:
        stdout, stderr, code, expired, error, started_process = failure.stdout or '', failure.stderr or '', None, True, str(failure), True
    except OSError as failure:
        stdout, stderr, code, expired, error, started_process = '', '', None, False, str(failure), False
    def raw(value):
        return value if isinstance(value, bytes) else value.encode('utf-8')
    return dict(argv=command, cwd=str(root), timeoutSeconds=timeout, returncode=code, timedOut=expired, processStarted=started_process,
                startedAtUtc=started, completedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                stdout=raw(stdout), stderr=raw(stderr), error=error)


def _write_phase(root, output, data, execution):
    output = _owned(root, output, required=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {key: value for key, value in execution.items() if key not in ('stdout', 'stderr')}
    for key in ('stdout', 'stderr'):
        path = _owned(root, output.with_suffix('.' + key + '.log'), required=False)
        path.write_bytes(execution[key])
        record[key + 'Log'] = path.relative_to(root).as_posix()
        record[key + 'Sha256'] = _sha(path)
        data[key] = execution[key].decode('utf-8', errors='replace')
    data['execution'] = record
    output.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def compile_staged(root, target=STAGED_TARGET, receipt_path=None):
    """Cross-compile the actual regression; this phase never executes it."""
    root = Path(root).absolute()
    if target != STAGED_TARGET or platform.system() != 'Linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('IPC staged compilation requires the Linux x64 producer for target linux-arm64')
    archive, manifest_path, manifest, archive_hash = _original_archive(root, target)
    engine = archive.parent
    cache_path = _owned(root, engine / 'cmake/CMakeCache.txt')
    cache_bytes = cache_path.read_bytes()
    cache = dict(re.findall(r'^([^#/\n:=]+):[^=\n]+=(.*)$', cache_bytes.decode('utf-8'), re.M))
    compiler = compiler_from_cmake(cache, engine / 'cmake')
    source = _owned(root, root / 'verification/driver-profile/arrow-ipc-check.cpp')
    include = engine / 'installed' / ('gdal4k-' + target) / 'include'
    headers = {name: _sha(_owned(root, include / name)) for name in
               ['arrow/api.h', 'arrow/io/memory.h', 'arrow/ipc/api.h', 'arrow/util/future.h', 'arrow/util/int_util_overflow.h']}
    linker_path = _owned(root, engine / 'native-linker-options.json')
    linker_name = linker_path.relative_to(root / 'gdal4k-engine/build').as_posix()
    if manifest['artifacts'].get(linker_name) != _sha(linker_path):
        raise ValueError('IPC linker options differ from the original engine manifest')
    options = compiler_linker_options(json.loads(linker_path.read_text(encoding='utf-8')))
    flags = shlex.split(cache.get('CMAKE_CXX_FLAGS', '')) + shlex.split(cache.get('CMAKE_CXX_FLAGS_RELEASE', ''))
    link = shlex.split(cache.get('CMAKE_EXE_LINKER_FLAGS', ''))
    if (not any(flag.startswith('--gcc-toolchain=') for flag in flags)
            or not any(flag.startswith('--sysroot=') for flag in flags)
            or '--target=aarch64-unknown-linux-gnu' not in flags):
        raise ValueError('IPC staged compilation requires the exact AArch64 engine sysroot and compiler target')
    program = _owned(root, root / 'verification/build/arrow-ipc-regression/arrow-ipc-check', required=False)
    program.parent.mkdir(parents=True, exist_ok=True)
    old_program = _identity(program) if program.exists() else None
    command = [compiler, *flags, '-std=c++17', '-DARROW_STATIC', '-DPARQUET_STATIC', '-pthread',
               '-I' + str(include), str(source), str(archive), *options, *link, '-o', str(program)]
    compiler_metadata = {str(p.relative_to(root)): _sha(_owned(root, p)) for p in
                         sorted((engine / 'cmake/CMakeFiles').glob('*/CMakeCXXCompiler.cmake'))}
    input_paths = [source, archive, manifest_path, cache_path, linker_path, Path(compiler)]
    before = {str(p): _sha(p) for p in input_paths}
    if (before[str(archive)] != archive_hash or cache_path.read_bytes() != cache_bytes
            or json.loads(manifest_path.read_text(encoding='utf-8')) != manifest
            or manifest['artifacts'].get(linker_name) != before[str(linker_path)]
            or compiler_from_cmake(cache, engine / 'cmake') != compiler):
        raise ValueError('IPC original archive or CMake inputs changed before compilation')
    execution = _invoke(command, root, 180)
    data = dict(schemaVersion=1, utc=execution['completedAtUtc'], result='failed', target=target,
                arrow='21.0.0+backport-CVE-2026-25087', compiled_only=True, actual_execution=False,
                sourcePath=source.relative_to(root).as_posix(), sourceSha256=before[str(source)],
                archivePath=archive.relative_to(root).as_posix(), archiveSha256=archive_hash,
                engineManifestPath=manifest_path.relative_to(root).as_posix(), engineManifestSha256=before[str(manifest_path)],
                engineSourceFingerprint=manifest['source_fingerprint'], executablePath=program.relative_to(root).as_posix(),
                compiler=compiler, compilerResolvedPath=str(Path(compiler).resolve()), compilerSha256=before[compiler],
                cmakeCacheSha256=before[str(cache_path)], cmakeCompilerMetadataSha256=compiler_metadata,
                directArrowHeaderSha256=headers, flags=flags, cases=CASES,
                compilerHost=dict(system=platform.system(), architecture=platform.machine().lower()),
                provenance='Compiled from current regression source and exact original engine archive; transport/source inventory separately bound by caller')
    output = receipt_path or root / 'verification/results/arrow-ipc-build-linux-arm64.json'
    failure = None
    if execution['returncode'] != 0 or execution['timedOut']:
        failure = 'Actual staged IPC compilation failed or timed out'
    else:
        try:
            _owned(root, program)
            if old_program is not None and _identity(program) == old_program:
                raise ValueError('IPC compiler did not refresh the staged executable')
            _arm_executable(program)
            if any(_sha(p) != before[str(p)] for p in input_paths):
                raise ValueError('IPC build inputs changed during compilation')
            if any(_sha(include / name) != digest for name, digest in headers.items()):
                raise ValueError('IPC direct Arrow headers changed during compilation')
            if any(_sha(root / name) != digest for name, digest in compiler_metadata.items()):
                raise ValueError('IPC CMake compiler metadata changed during compilation')
            data.update(result='compiled', executableSha256=_sha(program),
                        executableFormat=dict(format='ELF64', byteOrder='little', machine='AArch64'))
        except (ValueError, OSError) as error:
            failure = str(error)
    if failure:
        data['failure'] = failure
    _write_phase(root, output, data, execution)
    if failure:
        raise RuntimeError(failure)
    return data


def run_staged(root, executable, build_receipt, result_path=None):
    """Execute verified transported bytes on ARM; no compiler/install tree needed."""
    root = Path(root).absolute()
    if platform.system() != 'Linux' or platform.machine().lower() not in ('aarch64', 'arm64'):
        raise ValueError('IPC staged execution requires an actual Linux ARM64 runtime host')
    receipt_path = _owned(root, build_receipt)
    receipt_hash = _sha(receipt_path)
    built = json.loads(receipt_path.read_text(encoding='utf-8'))
    if (built.get('schemaVersion') != 1 or built.get('target') != STAGED_TARGET or built.get('result') != 'compiled'
            or built.get('compiled_only') is not True or built.get('actual_execution') is not False):
        raise ValueError('IPC runtime requires a successful compile-only build receipt')
    program = _owned(root, executable)
    source = _owned(root, built['sourcePath'])
    if (source.relative_to(root).as_posix() != 'verification/driver-profile/arrow-ipc-check.cpp'
            or program.relative_to(root).as_posix() != built['executablePath']
            or _sha(program) != built['executableSha256'] or _sha(source) != built['sourceSha256']):
        raise ValueError('IPC current executable or source differs from its build receipt')
    _arm_executable(program)
    archive, manifest_path, manifest, archive_hash = _original_archive(root, STAGED_TARGET)
    if (archive.relative_to(root).as_posix() != built['archivePath'] or archive_hash != built['archiveSha256']
            or manifest_path.relative_to(root).as_posix() != built['engineManifestPath']
            or _sha(manifest_path) != built['engineManifestSha256']
            or manifest['source_fingerprint'] != built['engineSourceFingerprint']):
        raise ValueError('IPC runtime archive/manifest differs from the original compiled engine identity')
    execution = _invoke([str(program)], root, 120)
    data = {key: built[key] for key in ['arrow', 'sourceSha256', 'archiveSha256', 'compiler', 'flags', 'cases']}
    data.update(schemaVersion=1, utc=execution['completedAtUtc'], result='failed', target=STAGED_TARGET,
                compiled_only=False, actual_execution=execution['processStarted'], executableSha256=built['executableSha256'],
                buildReceiptSha256=receipt_hash, engineManifestSha256=built['engineManifestSha256'],
                engineSourceFingerprint=built['engineSourceFingerprint'],
                host=dict(system=platform.system(), architecture=platform.machine().lower()),
                compilerProvenance='Cross-producer build receipt; compiler is not required or executed by this runtime phase')
    valid = (execution['returncode'] == 0 and not execution['timedOut']
             and execution['stdout'].decode('utf-8', errors='replace').splitlines().count(MARKER) == 1)
    stable = (_sha(program) == built['executableSha256'] and _sha(source) == built['sourceSha256']
              and _sha(archive) == archive_hash and _sha(manifest_path) == built['engineManifestSha256']
              and _sha(receipt_path) == receipt_hash)
    if valid and stable:
        data['result'] = 'passed'
    else:
        data['failure'] = 'Actual staged IPC execution failed, timed out, missed its marker or changed inputs'
    _write_phase(root, result_path or root / 'verification/results/arrow-ipc-regression.json', data, execution)
    if data['result'] != 'passed':
        raise RuntimeError(data['failure'])
    return data


def main():
    snapshot, target = Path(sys.argv[1]), sys.argv[2]
    engine = snapshot / 'gdal4k-engine/build/engine' / target
    cache = dict(re.findall(r'^([^#/\n:=]+):[^=\n]+=(.*)$', (engine / 'cmake/CMakeCache.txt').read_text(), re.M))
    compiler = compiler_from_cmake(cache, engine / 'cmake')
    work = snapshot / 'verification/build/arrow-ipc-regression'
    work.mkdir(parents=True, exist_ok=True)
    source = snapshot / 'verification/driver-profile/arrow-ipc-check.cpp'
    include = engine / 'installed' / ('gdal4k-' + target) / 'include'
    archive = engine / 'libgdal4k.a'
    assert source.is_file() and archive.is_file() and (include / 'arrow/ipc/api.h').is_file()
    options = compiler_linker_options(json.loads((engine / 'native-linker-options.json').read_text()))
    flags = shlex.split(cache.get('CMAKE_CXX_FLAGS', '')) + shlex.split(cache.get('CMAKE_CXX_FLAGS_RELEASE', ''))
    link = shlex.split(cache.get('CMAKE_EXE_LINKER_FLAGS', ''))
    if target.startswith('linux'):
        assert any(flag.startswith('--gcc-toolchain=') for flag in flags)
        assert any(flag.startswith('--sysroot=') for flag in flags)
    else:
        assert target == 'macos-arm64'
        flags += ['-arch', 'arm64', '-mmacosx-version-min=11.0']
    program = work / 'arrow-ipc-check'
    command = [compiler, *flags, '-std=c++17', '-DARROW_STATIC', '-DPARQUET_STATIC',
               '-pthread', '-I' + str(include), str(source), str(archive), *options, *link, '-o', str(program)]
    subprocess.run(command, check=True, timeout=180)
    result = subprocess.run([str(program)], capture_output=True, text=True, timeout=120, check=True)
    assert 'GDAL4K_ARROW_IPC_VARIADIC_PREFETCH_OK' in result.stdout
    with archive.open('rb') as file: archive_hash = hashlib.file_digest(file, 'sha256').hexdigest()
    output = snapshot / 'verification/results/arrow-ipc-regression.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'result': 'passed', 'target': target, 'arrow': '21.0.0+backport-CVE-2026-25087',
        'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'archiveSha256': archive_hash,
        'compiler': compiler, 'flags': flags,
        'cases': '16 repeats x coalesce/zero-copy x prebuffer/on-off x4 variadic StringView batches; async reentrant generator, reader lifetime, checked-overflow helpers',
        'stdout': result.stdout, 'stderr': result.stderr}, indent=2) + '\n')
    print(result.stdout, end='')


if __name__ == '__main__':
    main()
