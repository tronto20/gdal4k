"""Force a legal handoff window in the existing pinned Winpthread machine code.

The SDK is read only. A private cond.o copy changes one undefined symbol's name
to call an unlock hook. All section bytes, symbol indices and relocation records
remain identical. Linux can cross-compile this probe; only actual Windows can
run the three owned, bounded child processes. This is a backend scheduling
regression control, not a GDAL build/runtime acceptance test.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_SHA256 = '4821b991241355b6841d55f53e17c98da9334d2c2ac9914f371567b71b321a70'
OBJECT_SHA256 = '3fd0358553d94a4bc0e03761e28e4d4e959dae1cde4c127029c7ce6be5946ab1'
MEMBER = 'libwinpthread_la-cond.o'
OLD_SYMBOL = b'pthread_mutex_unlock'
NEW_SYMBOL = b'probe_unlock_hook'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ar_member(data, wanted):
    """Read the ordinary GNU archive format used by this exact SDK; no tools."""
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('Expected an ordinary GNU archive')
    at, names, matches = 8, b'', []
    while at < len(data):
        if at + 60 > len(data) or data[at + 58:at + 60] != b'`\n':
            raise ValueError('Malformed archive member header')
        header = data[at:at + 60]
        try:
            size = int(header[48:58].strip())
        except ValueError as error:
            raise ValueError('Malformed archive member size') from error
        if size < 0 or at + 60 + size > len(data):
            raise ValueError('Archive member exceeds archive')
        content = data[at + 60:at + 60 + size]
        name = header[:16].decode('ascii').strip()
        if name == '//':
            names = content
        elif name.startswith('/') and name[1:].isdigit():
            offset = int(name[1:])
            end = names.find(b'/\n', offset)
            if offset >= len(names) or end < offset:
                raise ValueError('Invalid GNU archive long name')
            name = names[offset:end].decode('ascii')
        else:
            name = name.rstrip('/')
        if name == wanted:
            matches.append(content)
        at += 60 + size + (size & 1)
    if at != len(data) or len(matches) != 1:
        raise ValueError('Expected exactly one named archive member')
    return matches[0]


def rewrite_unlock(data):
    """Change only an unaliased, undefined COFF external name, fail closed."""
    if len(data) < 20:
        raise ValueError('Truncated COFF header')
    machine, sections, _, symbols_at, count, optional, _ = struct.unpack_from('<HHIIIHH', data)
    if machine != 0x8664 or optional != 0 or not sections or not count:
        raise ValueError('Expected an AMD64 COFF object without optional header')
    sections_end = 20 + sections * 40
    strings_at = symbols_at + count * 18
    if sections_end > symbols_at or strings_at + 4 > len(data):
        raise ValueError('COFF table bounds are invalid')
    strings_size = struct.unpack_from('<I', data, strings_at)[0]
    if strings_size < 4 or strings_at + strings_size != len(data):
        raise ValueError('Unexpected COFF string-table extent')
    decoded, index = [], 0
    while index < count:
        at = symbols_at + index * 18
        value, section, kind, storage, aux = struct.unpack_from('<IhHBB', data, at + 8)
        offset = None
        if data[at:at + 4] == b'\0' * 4:
            offset = struct.unpack_from('<I', data, at + 4)[0]
            if not 4 <= offset < strings_size:
                raise ValueError('Invalid COFF symbol string offset')
            end = data.find(b'\0', strings_at + offset, strings_at + strings_size)
            if end < 0:
                raise ValueError('Unterminated COFF symbol name')
            name = data[strings_at + offset:end]
        else:
            name = data[at:at + 8].rstrip(b'\0')
        if index + aux >= count:
            raise ValueError('COFF auxiliary symbol exceeds table')
        decoded.append((index, name, offset, value, section, kind, storage, aux))
        index += 1 + aux
    targets = [s for s in decoded if s[1] == OLD_SYMBOL]
    if len(targets) != 1:
        raise ValueError('Expected one pthread_mutex_unlock symbol')
    target = targets[0]
    index, _, offset, value, section, kind, storage, aux = target
    if offset is None or value or section or storage != 2 or aux or kind not in (0, 0x20):
        raise ValueError('Unlock symbol must be an undefined external')
    if any(s[1] == NEW_SYMBOL for s in decoded):
        raise ValueError('Hook symbol already exists')
    length = len(OLD_SYMBOL) + 1
    if any(s[0] != index and s[2] is not None and s[2] < offset + length
           and s[2] + len(s[1]) + 1 > offset for s in decoded):
        raise ValueError('Unlock string is aliased by another symbol')
    at = strings_at + offset
    replacement = NEW_SYMBOL + b'\0' * (length - len(NEW_SYMBOL))
    changed = data[:at] + replacement + data[at + length:]
    proof = []
    relocation_targets = 0
    primary_symbols = {s[0] for s in decoded}
    for i in range(sections):
        h = 20 + 40 * i
        size, raw, reloc, _, nreloc, _, characteristics = struct.unpack_from('<IIIIHHI', data, h + 16)
        stored_size = size if raw else 0
        if size and not raw and not characteristics & 0x80:
            raise ValueError('Only uninitialized sections may omit raw storage')
        if raw + stored_size > symbols_at or reloc + nreloc * 10 > symbols_at:
            raise ValueError('COFF section or relocation exceeds object body')
        if stored_size and raw < sections_end or nreloc and reloc < sections_end:
            raise ValueError('COFF section overlaps header')
        for j in range(nreloc):
            entry = reloc + j * 10
            address, symbol, _ = struct.unpack_from('<IIH', data, entry)
            if address >= size or symbol not in primary_symbols:
                raise ValueError('Invalid COFF relocation address or symbol')
            relocation_targets += symbol == index
        raw_bytes, relocation_bytes = data[raw:raw + stored_size], data[reloc:reloc + nreloc * 10]
        if changed[raw:raw + stored_size] != raw_bytes or changed[reloc:reloc + nreloc * 10] != relocation_bytes:
            raise AssertionError('Instruction/data/relocation bytes changed')
        name = data[h:h + 8].rstrip(b'\0').decode('ascii')
        if name.startswith('/'):
            section_name_offset = int(name[1:])
            if not 4 <= section_name_offset < strings_size:
                raise ValueError('Invalid COFF section name string offset')
            end = data.find(b'\0', strings_at + section_name_offset, strings_at + strings_size)
            if end < 0 or section_name_offset < offset + length and end - strings_at + 1 > offset:
                raise ValueError('Section name overlaps changed symbol string')
            name = data[strings_at + section_name_offset:end].decode('ascii')
        proof.append(dict(name=name, bytes=size, storedBytes=stored_size,
                          sha256=sha(raw_bytes), relocations=nreloc,
                          relocationSha256=sha(relocation_bytes)))
    if not relocation_targets or changed[:strings_at] != data[:strings_at] or len(changed) != len(data):
        raise ValueError('Expected referenced symbol and unchanged object structure')
    return changed, dict(originalSha256=sha(data), rewrittenSha256=sha(changed),
                         changedStringOffset=at, changedStringBytes=length,
                         symbolIndex=index, hookRelocations=relocation_targets,
                         sections=proof, instructionsAndDataAndRelocationsUnchanged=True)


def pinned_object(archive):
    if sha(archive) != ARCHIVE_SHA256:
        raise ValueError('Existing SDK libwinpthread.a does not match the pinned archive SHA256')
    obj = ar_member(archive, MEMBER)
    if sha(obj) != OBJECT_SHA256:
        raise ValueError('Existing SDK cond.o does not match the pinned member SHA256')
    return obj


def existing_tools(clang=None, sysroot=None):
    dependencies = Path(os.environ.get('KONAN_DATA_DIR', Path.home() / '.konan')) / 'dependencies'
    sdk = Path(sysroot) if sysroot else dependencies / 'msys2-mingw-w64-x86_64-2'
    if clang:
        compiler = Path(clang)
    else:
        host = {'Windows': 'windows', 'Linux': 'linux', 'Darwin': 'macos'}[platform.system()]
        suffix = '.exe' if os.name == 'nt' else ''
        candidates = sorted(p / ('bin/clang++' + suffix) for p in dependencies.glob(f'llvm-21-x86_64-{host}-*')
                            if (p / ('bin/clang++' + suffix)).is_file())
        if len(candidates) != 1:
            raise ValueError('Initialize the existing pinned Kotlin/Native SDK first, or pass --clang')
        compiler = candidates[0]
    linker = compiler.with_name('ld.lld.exe' if os.name == 'nt' else 'ld.lld')
    if not compiler.is_file() or not linker.is_file() or not sdk.is_dir():
        raise ValueError('Existing Clang21, lld and MinGW SDK paths are required; this probe never downloads')
    # LLVM dispatches clang++/ld.lld by argv[0]. Preserve their final symlink
    # names while canonicalizing the containing directory.
    return compiler.parent.resolve() / compiler.name, linker.parent.resolve() / linker.name, sdk.resolve()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8', newline='\n')


def validate_control(mode, observed, record):
    fields = {'mode', 'result', 'pid', 'producerTid', 'consumerTid', 'consumerUnlocks',
              'queueBeforeSecondWait', 'queueBeforeThirdWait', 'observedBlockedQueueCount',
              'oldStallObserved', 'hookError', 'threadsJoined', 'rowsObservedAfterJoin', 'rows'}
    expected = {'shared': 'expected-stall-observed-and-cancelled', 'split': 'progress-and-eof-completed',
                'split-cancel': 'cancel-completed'}[mode]
    integers = ['pid', 'producerTid', 'consumerTid', 'consumerUnlocks', 'queueBeforeSecondWait',
                'queueBeforeThirdWait', 'observedBlockedQueueCount']
    booleans = ['oldStallObserved', 'hookError', 'threadsJoined', 'rowsObservedAfterJoin']
    if (not isinstance(observed, dict) or set(observed) != fields
            or any(type(observed[key]) is not int for key in integers)
            or any(type(observed[key]) is not bool for key in booleans)
            or not isinstance(observed['rows'], list) or any(type(x) is not int for x in observed['rows'])
            or observed['mode'] != mode or observed['result'] != expected
            or observed['pid'] != record['pid'] or observed['producerTid'] <= 0
            or observed['consumerTid'] <= 0 or observed['producerTid'] == observed['consumerTid']
            or observed['threadsJoined'] is not True or observed['rowsObservedAfterJoin'] is not True
            or observed['hookError'] is not False or record['status'] != 'passed'
            or record['outputComplete'] is not True or record['returncode'] != 0
            or record['cleanup'] != dict(activeProcessesAfter=0, jobClosed=True, forcedTreeTermination=False)
            or observed['rows'] != ([3, 1, 0, 0] if mode == 'split' else [3])):
        raise ValueError('Actual probe execution did not establish the required bounded control')
    if mode == 'shared':
        if (observed['oldStallObserved'] is not True or observed['consumerUnlocks'] != 3
                or observed['queueBeforeSecondWait'] != 1 or observed['queueBeforeThirdWait'] != 0
                or observed['observedBlockedQueueCount'] != -2):
            raise ValueError('Actual shared-CV token-consumption schedule was not established')
    elif (observed['oldStallObserved'] is not False or observed['observedBlockedQueueCount'] != -999
          or observed['consumerUnlocks'] != (1 if mode == 'split-cancel' else 2)
          or observed['queueBeforeSecondWait'] != (-999 if mode == 'split-cancel' else 0)
          or observed['queueBeforeThirdWait'] != -999):
        raise ValueError('Actual split-CV control did not establish the required separate queues')


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        if os.name == 'nt':
            from windows_process import private_directory
            private_directory(output)
        if args.run and os.name != 'nt':
            raise ValueError('--run requires actual Windows; cross-compile alone does not establish the mechanism')
        compiler, linker, sdk = existing_tools(args.clang, args.sysroot)
        return run_prepared(args, compiler, linker, sdk, output)
    except Exception as error:
        path = output / 'probe-result.json'
        receipt = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
            schemaVersion=1, accepted=False, windowsRuntimeAccepted=False, gdalRuntimeAccepted=False,
            actualWindowsExecution=False, mechanismReproducedAndSplitControlsPassed=False,
            status='preparation-failed')
        if receipt['status'] not in ('control-failed', 'sdk-changed', 'preparation-failed'):
            receipt['failedStage'] = receipt['status']
            receipt['status'] = 'build-or-validation-failed'
        receipt['failure'] = dict(type=type(error).__name__, message=str(error))
        receipt['failureEvidence'] = [dict(path=p.relative_to(output).as_posix(), bytes=p.stat().st_size,
                                          sha256=sha(p.read_bytes())) for p in sorted(output.rglob('*'))
                                      if p.is_file() and p != path]
        write_json(path, receipt)
        raise


def run_prepared(args, compiler, linker, sdk, output):
    source = output / 'probe.cpp'
    source.write_bytes((ROOT / 'verification/native/winpthread_handoff_probe.cpp').read_bytes())
    archive = sdk / 'x86_64-w64-mingw32/lib/libwinpthread.a'
    original_archive = archive.read_bytes()
    original = pinned_object(original_archive)
    sdk_inputs = ['include/c++/9.2.0/condition_variable', 'include/c++/9.2.0/bits/std_mutex.h',
                  'include/c++/9.2.0/bits/fs_path.h', 'x86_64-w64-mingw32/include/intrin.h',
                  'x86_64-w64-mingw32/include/pthread.h',
                  'lib/gcc/x86_64-w64-mingw32/9.2.0/libstdc++.a',
                  'lib/gcc/x86_64-w64-mingw32/9.2.0/libgcc.a']
    input_hashes = {name: sha((sdk / name).read_bytes()) for name in sdk_inputs}
    licenses = []
    for name in ['gcc-libs/COPYING3', 'gcc-libs/COPYING.RUNTIME', 'gcc-libs/COPYING.LIB',
                 'libwinpthread/COPYING', 'libwinpthread/mingw-w64-libraries/winpthreads/COPYING']:
        content = (sdk / 'share/licenses' / name).read_bytes()
        destination = output / 'licenses' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        licenses.append(dict(sourceRelative='share/licenses/' + name, preservedRelative='licenses/' + name,
                             bytes=len(content), sha256=sha(content)))
    changed, proof = rewrite_unlock(original)
    if proof['hookRelocations'] != 2:
        raise ValueError('Pinned cond.o must have exactly two unlock-hook relocations')
    (output / 'sdk-cond-original.o').write_bytes(original)
    hooked = output / 'sdk-cond-hooked.o'
    hooked.write_bytes(changed)
    spec = importlib.util.spec_from_file_location('handoff_toolchains', ROOT / 'gdal4k-engine/tools/toolchains.py')
    toolchains = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolchains)
    overlay = toolchains.mingw_header_overlay(sdk, output / 'compiler-headers')
    executable = output / 'winpthread-handoff.exe'
    frontend = [str(compiler), '--target=x86_64-w64-windows-gnu', '--sysroot=' + str(sdk),
                '-femulated-tls', '-isystem', str(overlay), '-std=c++17', '-O2']
    command = frontend + [str(source), str(hooked),
               '--ld-path=' + str(linker), '-pthread', '-static-libgcc', '-static-libstdc++',
               '-Wl,-Bstatic', '-lwinpthread', '-Wl,-Bdynamic', '-o', str(executable)]
    version = subprocess.run([str(compiler), '--version'], capture_output=True, timeout=10)
    (output / 'compiler-version.txt').write_bytes(version.stdout + version.stderr)
    if version.returncode or not re.match(rb'clang version 21\.', version.stdout):
        raise ValueError('The existing pinned Clang21 compiler is required')
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    (output / 'compile.stdout').write_bytes(result.stdout)
    (output / 'compile.stderr').write_bytes(result.stderr)
    receipt = dict(schemaVersion=1, scope='private pinned-SDK handoff regression control',
                   accepted=False, windowsRuntimeAccepted=False, gdalRuntimeAccepted=False,
                   actualWindowsExecution=False, mechanismReproducedAndSplitControlsPassed=False,
                   status='compiled' if not result.returncode else 'compile-failed', sourceSha256=sha(source.read_bytes()),
                   driverSha256=sha(Path(__file__).read_bytes()),
                   overlayHelperSha256=sha((ROOT / 'gdal4k-engine/tools/toolchains.py').read_bytes()),
                   compiler=dict(path=str(compiler), sha256=sha(compiler.read_bytes()), versionReturncode=version.returncode,
                                 versionOutputSha256=sha(version.stdout + version.stderr)),
                   linker=dict(path=str(linker), sha256=sha(linker.read_bytes())),
                   sdk=dict(path=str(sdk), archiveSha256=sha(original_archive), member=MEMBER,
                            originalMemberSha256=sha(original), sdkArchiveUnchanged=archive.read_bytes() == original_archive,
                            inputSha256=input_hashes), preservedLicenses=licenses,
                   privateHeaderOverlay={p.relative_to(overlay).as_posix(): sha(p.read_bytes())
                                         for p in sorted(overlay.rglob('*')) if p.is_file()},
                   objectRewrite=proof, compile=dict(command=command, returncode=result.returncode,
                   stdoutSha256=sha(result.stdout), stderrSha256=sha(result.stderr)), modes=[])
    write_json(output / 'probe-result.json', receipt)
    if result.returncode:
        raise RuntimeError('Pinned-SDK probe compilation failed; inspect compile.stderr')
    assembly = output / 'probe.s'
    assembly_command = frontend + ['-S', str(source), '-o', str(assembly)]
    assembly_result = subprocess.run(assembly_command, capture_output=True, timeout=120)
    (output / 'assembly.stdout').write_bytes(assembly_result.stdout)
    (output / 'assembly.stderr').write_bytes(assembly_result.stderr)
    receipt['assembly'] = dict(command=assembly_command, returncode=assembly_result.returncode,
                               stdoutSha256=sha(assembly_result.stdout), stderrSha256=sha(assembly_result.stderr),
                               sha256=sha(assembly.read_bytes()) if assembly.is_file() else None)
    write_json(output / 'probe-result.json', receipt)
    if assembly_result.returncode:
        raise RuntimeError('Probe assembly retention failed; inspect assembly.stderr')
    binary = executable.read_bytes()
    from windows_pe import inspect, validate_system_dependencies
    pe = inspect(binary)
    validate_system_dependencies(pe['imports']['all'])
    receipt['executable'] = dict(bytes=len(binary), sha256=sha(binary), pe=pe)
    write_json(output / 'probe-result.json', receipt)
    if args.run:
        from command_evidence import run_with_evidence
        receipt['actualWindowsExecution'] = True
        receipt['status'] = 'running'
        write_json(output / 'probe-result.json', receipt)
        for mode in ['shared', 'split', 'split-cancel']:
            try:
                record_path = run_with_evidence([str(executable), mode], cwd=output,
                                                directory=output, timeout=20)
                record = json.loads(record_path.read_text(encoding='utf-8'))
                log = (output / record['log']).read_bytes()
                if sha(log) != record['logSha256']:
                    raise ValueError('Retained probe log differs from owned command receipt')
                lines = [line[len(b'WINPTHREAD_HANDOFF '):] for line in log.splitlines()
                         if line.startswith(b'WINPTHREAD_HANDOFF ')]
                if len(lines) != 1:
                    raise ValueError('Probe must emit exactly one complete result')
                observed = json.loads(lines[0])
                validate_control(mode, observed, record)
                receipt['modes'].append(dict(observed=observed, commandReceipt=record_path.name,
                                            commandReceiptSha256=sha(record_path.read_bytes()), logSha256=sha(log)))
                write_json(output / 'probe-result.json', receipt)
            except Exception as error:
                receipt.update(status='control-failed', failedMode=mode,
                               failure=dict(type=type(error).__name__, message=str(error)),
                               failureEvidence=[dict(path=p.name, bytes=p.stat().st_size, sha256=sha(p.read_bytes()))
                                                for p in sorted(output.glob('[0-9][0-9][0-9].*'))])
                write_json(output / 'probe-result.json', receipt)
                raise
        receipt['mechanismReproducedAndSplitControlsPassed'] = True
        receipt['status'] = 'controls-passed'
    receipt['sdk']['sdkArchiveUnchanged'] = archive.read_bytes() == original_archive
    receipt['sdk']['sdkInputsUnchanged'] = all(sha((sdk / name).read_bytes()) == value
                                             for name, value in input_hashes.items())
    if not receipt['sdk']['sdkArchiveUnchanged'] or not receipt['sdk']['sdkInputsUnchanged']:
        receipt.update(status='sdk-changed', mechanismReproducedAndSplitControlsPassed=False)
        write_json(output / 'probe-result.json', receipt)
        raise ValueError('Read-only SDK archive changed during probe')
    write_json(output / 'probe-result.json', receipt)
    print(json.dumps(dict(probeReceipt=str(output / 'probe-result.json'),
                          actualWindowsExecution=receipt['actualWindowsExecution'])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clang', help='Existing cached Clang21 clang++ path; default detects existing K/N cache')
    parser.add_argument('--sysroot', help='Existing pinned msys2-mingw-w64-x86_64-2; default detects K/N cache')
    parser.add_argument('--output', required=True, help='New private evidence directory; never overwrite')
    parser.add_argument('--run', action='store_true', help='Run all three bounded owned children on actual Windows')
    args = parser.parse_args()
    run(args)


if __name__ == '__main__':
    main()
