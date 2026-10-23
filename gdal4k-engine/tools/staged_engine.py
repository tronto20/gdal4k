"""Validate a cross-produced engine for normal Linux ARM64 JVM Gradle tasks.

This is a fail-closed consumer of the producer manifest, not another producer.
Archive download provenance and the prepared full-source inventory are verified
by the calling CI/profile orchestration separately.
"""
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import struct


TARGET = 'linux-arm64'
ELF_MACHINE_AARCH64 = 183


def _regular(path, root):
    if not path.is_relative_to(root):
        raise ValueError('Staged path is outside its owned build directory: ' + str(path))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('Staged paths must not use symlinks: ' + str(parent))
        if parent == root:
            break
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Missing regular staged file: ' + str(path))
    return path


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate staged JSON key: ' + key)
        result[key] = value
    return result


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
    return dict(bytes=size, sha256=digest.hexdigest())


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _elf_header(header, object_type, label):
    if (len(header) < 64 or header[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HHI', header, 16) != (object_type, ELF_MACHINE_AARCH64, 1)
            or struct.unpack_from('<H', header, 52)[0] != 64):
        raise ValueError('Expected ELF64 little-endian AArch64 ' + label)


def _static_archive(path):
    """Check every object header, without extracting or executing the archive."""
    objects = metadata = 0
    names = b''
    size = path.stat().st_size
    with path.open('rb') as stream:
        if stream.read(8) != b'!<arch>\n':
            raise ValueError('Expected a self-contained ar static archive')
        while stream.tell() < size:
            header = stream.read(60)
            if len(header) != 60 or header[58:] != b'`\n':
                raise ValueError('Malformed staged ar member header')
            try:
                name = header[:16].decode('ascii').strip()
                count = header[48:58].decode('ascii').strip()
                if not count.isdecimal():
                    raise ValueError('non-decimal size')
                count = int(count)
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError('Malformed staged ar member fields') from error
            end = stream.tell() + count
            if end > size:
                raise ValueError('Truncated staged ar member')
            if name in ('/', '/SYM64/'):
                metadata += 1
            elif name == '//':
                names = stream.read(count)
                metadata += 1
            else:
                if name.startswith('#1/'):
                    try:
                        name_length = int(name[3:])
                    except ValueError as error:
                        raise ValueError('Malformed BSD ar extended name') from error
                    if not 0 < name_length <= count:
                        raise ValueError('Invalid BSD ar extended name length')
                    name = stream.read(name_length).decode('utf-8').rstrip('\0')
                elif name.startswith('/'):
                    if not name[1:].isdecimal() or int(name[1:]) >= len(names):
                        raise ValueError('Invalid GNU ar long-name reference')
                    name = names[int(name[1:]):].split(b'/\n', 1)[0].split(b'\0', 1)[0].decode('utf-8')
                if name in ('__.SYMDEF', '__.SYMDEF SORTED', '__.SYMDEF_64', '__.SYMDEF_64 SORTED'):
                    metadata += 1
                else:
                    if end - stream.tell() < 64:
                        raise ValueError('Truncated staged ar object: ' + name)
                    _elf_header(stream.read(64), 1, 'static object: ' + name)
                    objects += 1
            stream.seek(end)
            if count % 2 and stream.read(1) != b'\n':
                raise ValueError('Invalid staged ar alignment padding')
        if stream.tell() != size or not objects:
            raise ValueError('Staged static archive has no complete AArch64 objects')
    return dict(format='ar', objectMembers=objects, metadataMembers=metadata,
                objectFormat='ELF64', machine='AArch64')


def verify_staged_engine(binary, target, fingerprint, vcpkg_revision, binary_version):
    """Use the caller's canonical producer fingerprint; never build on failure."""
    host = dict(system=platform.system(), architecture=platform.machine().lower())
    if target != TARGET or host['system'] != 'Linux' or host['architecture'] not in ('arm64', 'aarch64'):
        raise ValueError('Verified staged engines currently require target linux-arm64 on a Linux ARM64 host')
    binary = Path(binary).absolute()
    if binary.is_symlink() or (binary / 'build').is_symlink():
        raise ValueError('Staged engine requires an owned regular build directory')
    build = binary / 'build'
    out = build / 'engine' / target
    manifest_path = _regular(out / 'manifest.json', build)
    manifest_bytes = manifest_path.read_bytes()
    data = json.loads(manifest_bytes.decode('utf-8'), object_pairs_hook=_json_pairs)
    source_hash = fingerprint()
    if (not isinstance(data, dict) or data.get('target') != target or data.get('gdal') != '3.12.4'
            or data.get('jxl') is not True or data.get('vcpkg') != vcpkg_revision
            or not re.fullmatch(r'[0-9a-f]{64}', source_hash)
            or data.get('source_fingerprint') != source_hash):
        raise ValueError('Staged engine target, pinned versions or current source fingerprint differ')
    artifacts = data.get('artifacts')
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError('Staged manifest has no artifact inventory')
    prefix = f'engine/{target}/'
    static_name = prefix + 'libgdal4k.a'
    linker_name = prefix + 'native-linker-options.json'
    jni_name = f'jvm-resources/gdal4k/{target}/libgdal4k.so'
    required = {static_name, linker_name, jni_name, prefix + 'licenses/gdal.txt'}
    if not required <= artifacts.keys():
        raise ValueError('Staged manifest omits a required static, linker, JNI or GDAL license payload')
    actual = {}
    file_identities = {}
    license_names = set()
    for name, expected in artifacts.items():
        if (not isinstance(name, str) or '\\' in name or '\0' in name
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or name != PurePosixPath(name).as_posix()):
            raise ValueError('Unsafe staged artifact path: ' + repr(name))
        license_file = PurePosixPath(name).parent == PurePosixPath(prefix + 'licenses') and name.endswith('.txt')
        if name not in {static_name, linker_name, jni_name} and not license_file:
            raise ValueError('Unexpected staged artifact target or kind: ' + name)
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Invalid staged artifact SHA256: ' + name)
        path = _regular(build / name, build)
        before = _identity(path)
        record = _digest(path)
        if _identity(path) != before:
            raise ValueError('Staged artifact changed while hashing: ' + name)
        if record['sha256'] != expected:
            raise ValueError('Staged artifact SHA256 mismatch: ' + name)
        actual[name] = record
        file_identities[name] = before
        if license_file:
            path.read_bytes().decode('utf-8')
            license_names.add(name)
    license_folder = out / 'licenses'
    current_licenses = set()
    for path in license_folder.iterdir():
        _regular(path, build)
        if path.suffix != '.txt':
            raise ValueError('Unexpected current staged license file: ' + path.name)
        current_licenses.add(path.relative_to(build).as_posix())
    if current_licenses != license_names:
        raise ValueError('Staged manifest and current license directory inventory differ')
    with (build / jni_name).open('rb') as stream:
        _elf_header(stream.read(64), 3, 'JNI shared library')
    static = _static_archive(build / static_name)
    linker = json.loads((build / linker_name).read_text(encoding='utf-8'), object_pairs_hook=_json_pairs)
    if not isinstance(linker, list) or any(not isinstance(x, str) or not x or any(c in x for c in '\0\r\n') for x in linker):
        raise ValueError('Invalid staged native linker option list')
    if fingerprint() != source_hash or manifest_path.read_bytes() != manifest_bytes:
        raise ValueError('Staged engine sources or manifest changed during verification')
    if any(_identity(_regular(build / name, build)) != identity for name, identity in file_identities.items()):
        raise ValueError('Staged artifacts changed during verification')
    receipt = dict(schemaVersion=1, result='passed', mode='verified-staged-only',
                   target=target, host=host, binaryVersion=binary_version,
                   sourceFingerprint=source_hash, vcpkg=vcpkg_revision, gdal='3.12.4', jxl=True,
                   manifestSha256=hashlib.sha256(manifest_bytes).hexdigest(),
                   artifactCount=len(actual), artifacts=actual, licenseCount=len(license_names),
                   jni=dict(format='ELF64', byteOrder='little', type='ET_DYN', machine='AArch64'),
                   staticArchive=static, nativeLinkerOptions=linker,
                   producerExecuted=False, toolBootstrapExecuted=False,
                   provenance='Archive transport and prepared full-source identity require separate caller evidence',
                   verifiedAtUtc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    destination = build / 'verified-staged' / (target + '.json')
    if destination.parent.is_symlink() or destination.is_symlink():
        raise ValueError('Staged verification receipt must stay in the owned build directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
    return receipt
