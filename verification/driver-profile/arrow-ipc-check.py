"""Run IPC variadic/prebuffer regression with the packaged archive's exact C++ toolchain."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


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
