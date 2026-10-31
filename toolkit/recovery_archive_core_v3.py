"""Readonly, streaming C16 recovery archive audit. No capability verdicts.

The exact H17 gzip/XZ decoder is vendored and content pinned. Large TAR inputs
are consumed forwards. A nested ZIP is copied once to bounded opaque scratch,
verified, then randomly inspected without replaying its parent TAR prefix.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import gc
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tarfile
import time
import types
import uuid
import zipfile

CHUNK = 65536
MiB = 1024 ** 2
TRANSPORT_SHA256 = '850c3d70d92a6bfc2ffd95bddfe2d6635232e906077ec12e5c91c00390c881bc'
STRICT_ZIP_SHA256 = 'c7f39f982bdff50a04fe16aa85c76902b865172c821c8daa954c4b6e42f93388'


class AuditError(ValueError):
    pass


def need(value, message):
    if not value:
        raise AuditError(message)


def full11(value):
    return {name: getattr(value, name) for name in (
        'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
        'st_size', 'st_atime_ns', 'st_mtime_ns', 'st_ctime_ns', 'st_blocks')}


def open_readonly(path):
    """Walk every parent without following symlinks, then open without atime."""
    path = Path(path)
    need(path.is_absolute() and '..' not in path.parts, 'Input path must be absolute and canonical')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode), 'Input is not a regular file')
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, 'rb', buffering=0)
    finally:
        os.close(directory)


def _transport():
    path = Path(__file__).with_name('h17_archive_transport.py')
    with open_readonly(path) as source:
        before = full11(os.fstat(source.fileno()))
        raw = source.read(65536)
        need(not source.read(1), 'Vendored transport source exceeds source bound')
        need(full11(os.fstat(source.fileno())) == before, 'Vendored transport source changed')
    need(hashlib.sha256(raw).hexdigest() == TRANSPORT_SHA256, 'Vendored H17 transport source SHA mismatch')
    module = types.ModuleType('c16_recovery_h17_transport')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


transport = _transport()


def _strict_zip():
    path = Path(__file__).with_name('strict_zip_stream_v1.py')
    with open_readonly(path) as source:
        before = full11(os.fstat(source.fileno()))
        raw = source.read(65536)
        need(not source.read(1) and full11(os.fstat(source.fileno())) == before,
             'Strict ZIP source changed or exceeds bound')
    need(hashlib.sha256(raw).hexdigest() == STRICT_ZIP_SHA256, 'Strict ZIP source SHA mismatch')
    module = types.ModuleType('c16_recovery_strict_zip')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


strict_zip = _strict_zip()


@dataclass(frozen=True)
class AuditConfig:
    scratch_root: str | Path
    max_scratch_bytes: int = 96 * MiB
    max_nested_bytes: int = 64 * MiB
    max_depth: int = 8
    max_entries: int = 50000
    max_cd_bytes: int = 16 * MiB
    max_decoded_bytes: int = 16 * 1024 ** 3
    rss_limit: int = 128 * MiB
    xz_memory_limit: int = 64 * MiB


def safe_name(name, *, directory=False):
    need(isinstance(name, str) and 0 < len(name.encode('utf-8')) <= 4096,
         'Archive path length exceeds limit')
    if directory:
        need(name.endswith('/'), 'Noncanonical ZIP directory')
        name = name[:-1]
    need(name and '\\' not in name and ':' not in name and
         not any(ord(c) < 32 or ord(c) == 127 for c in name), 'Unsafe archive path')
    value = PurePosixPath(name)
    need(not value.is_absolute() and value.as_posix() == name and
         all(part not in ('', '.', '..') for part in value.parts), 'Noncanonical archive path')
    reserved = {'con', 'prn', 'aux', 'nul', *('com' + str(i) for i in range(1, 10)),
                *('lpt' + str(i) for i in range(1, 10))}
    need(all(part.rstrip(' .') == part and part.split('.')[0].casefold() not in reserved and
             not any(c in '<>"|?*' for c in part) for part in value.parts), 'Ambiguous archive path')
    return name


def _read_exact(source, count):
    need(0 <= count <= MiB, 'Unbounded parser read')
    result = bytearray()
    while len(result) < count:
        data = source.read(min(CHUNK, count - len(result)))
        need(data, 'Truncated archive structure')
        result.extend(data)
    return bytes(result)


def extra_fields(raw):
    """Parse bounded ZIP extra TLVs; repeated ZIP64 identities are ambiguous."""
    result, offset = {}, 0
    while offset < len(raw):
        need(offset + 4 <= len(raw), 'Truncated ZIP extra-field header')
        kind, size = struct.unpack_from('<HH', raw, offset)
        offset += 4
        need(offset + size <= len(raw), 'Truncated ZIP extra-field payload')
        if kind == 1:
            need(kind not in result, 'Duplicate ZIP64 extra field')
            result[kind] = raw[offset:offset + size]
        offset += size
    return result


class _HashReader:
    def __init__(self, source, maximum):
        self.source = source
        self.maximum = maximum
        self.count = 0
        self.sha = hashlib.sha256()
        self.prefix = bytearray()

    def read(self, count=-1):
        need(0 <= count <= MiB, 'Unbounded archive payload read')
        data = self.source.read(count)
        self.count += len(data)
        need(self.count <= self.maximum, 'Archive member exceeds declared length')
        self.sha.update(data)
        if len(self.prefix) < 4096:
            self.prefix.extend(data[:4096 - len(self.prefix)])
        return data


def binary_identity(prefix):
    if prefix.startswith(b'\xca\xfe\xba\xbe') and len(prefix) >= 8 and 45 <= int.from_bytes(prefix[6:8], 'big') <= 100:
        return {'format': 'JVM-class', 'major': int.from_bytes(prefix[6:8], 'big'),
                'minor': int.from_bytes(prefix[4:6], 'big')}
    if prefix.startswith(b'\x7fELF') and len(prefix) >= 20:
        need(prefix[4] in (1, 2) and prefix[5] in (1, 2), 'Invalid ELF identification')
        return {'format': 'ELF', 'bits': 32 if prefix[4] == 1 else 64,
                'machine': int.from_bytes(prefix[18:20], 'little' if prefix[5] == 1 else 'big')}
    if prefix.startswith(b'MZ') and len(prefix) >= 64:
        offset = int.from_bytes(prefix[60:64], 'little')
        if offset + 6 <= len(prefix) and prefix[offset:offset + 4] == b'PE\0\0':
            return {'format': 'PE', 'machine': int.from_bytes(prefix[offset + 4:offset + 6], 'little')}
        return {'format': 'MZ', 'peHeaderOffset': offset, 'machineVerified': False}
    if prefix.startswith(b'\x00asm') and len(prefix) >= 8:
        return {'format': 'WASM', 'version': int.from_bytes(prefix[4:8], 'little')}
    if len(prefix) >= 8 and prefix[:4] in (b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe',
                                          b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf'):
        byteorder = 'little' if prefix[:4] in (b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe') else 'big'
        return {'format': 'MachO', 'machine': int.from_bytes(prefix[4:8], byteorder),
                'bits': 64 if prefix[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf') else 32}
    if prefix[:4] in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf'):
        return {'format': 'MachO-fat', 'machineVerified': False}
    if len(prefix) >= 20 and int.from_bytes(prefix[:2], 'little') in (0x8664, 0xaa64, 0x14c):
        return {'format': 'COFF', 'machine': int.from_bytes(prefix[:2], 'little')}
    if prefix.startswith(b'\x00\x00\xff\xff') and len(prefix) >= 8:
        return {'format': 'COFF-import-or-bigobj', 'machine': int.from_bytes(prefix[6:8], 'little')}
    if prefix.startswith((b'BC\xc0\xde', b'\xde\xc0\x17\x0b')):
        return {'format': 'LLVM-bitcode', 'machineVerified': False}
    return None


class Audit:
    def __init__(self, config: AuditConfig, emit):
        self.config, self.emit = config, emit
        need(all(type(getattr(config, field)) is int and getattr(config, field) > 0 for field in
                 ('max_scratch_bytes', 'max_nested_bytes', 'max_depth', 'max_entries',
                  'max_cd_bytes', 'max_decoded_bytes', 'rss_limit', 'xz_memory_limit')),
             'Audit limits must be positive integers')
        self.scratch = Path(config.scratch_root)
        need(self.scratch.is_absolute(), 'Scratch directory must be absolute')
        fd = os.open(self.scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            need(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700,
                 'Scratch directory must be owned private mode0700')
            self.scratch_identity = (st.st_dev, st.st_ino)
        finally:
            os.close(fd)
        self.active_scratch = 0
        self.peak_scratch = 0
        self.record_count = 0
        self.last_sample = 0
        self.peak_rss = 0
        self._checkpoint(force=True)

    def _checkpoint(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_sample < 0.5:
            return
        self.last_sample = now
        gc.collect()
        with open('/proc/self/statm', 'r', encoding='ascii') as source:
            rss = int(source.read(256).split()[1]) * os.sysconf('SC_PAGE_SIZE')
        with open('/proc/self/status', 'r', encoding='ascii') as source:
            status = source.read(16384)
        matched = re.search(r'^VmHWM:\s+([0-9]+) kB$', status, re.MULTILINE)
        need(matched is not None, 'Current process VmHWM is unavailable')
        self.peak_rss = max(self.peak_rss, rss, int(matched.group(1)) * 1024)
        need(self.peak_rss <= self.config.rss_limit, 'Current RSS/process peak exceeds audit limit')

    def _emit(self, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        need(len(raw) <= 2 * MiB, 'Audit record exceeds metadata record limit')
        self.emit(value)
        self.record_count += 1

    @contextmanager
    def _spool(self, source, size, chain):
        need(size <= self.config.max_nested_bytes, 'Nested ZIP exceeds scratch member limit')
        unit = os.statvfs(self.scratch).f_frsize
        charge = ((size + unit - 1) // unit) * unit
        need(self.active_scratch + charge <= self.config.max_scratch_bytes,
             'Nested archive scratch limit exceeded')
        need(os.statvfs(self.scratch).f_bavail * unit >= charge, 'Scratch filesystem has insufficient space')
        current = os.lstat(self.scratch)
        need((current.st_dev, current.st_ino) == self.scratch_identity and
             stat.S_ISDIR(current.st_mode) and current.st_uid == os.getuid(), 'Scratch directory changed')
        path = self.scratch / ('nested-' + uuid.uuid4().hex + '.opaque')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        owned = os.fstat(fd)
        self.active_scratch += charge
        self.peak_scratch = max(self.peak_scratch, self.active_scratch)
        digest, count = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, 'wb', buffering=0) as destination:
                while data := source.read(CHUNK):
                    count += len(data)
                    need(count <= size, 'Nested ZIP grew beyond declared length')
                    destination.write(data)
                    digest.update(data)
                need(count == size, 'Nested ZIP length differs from declaration')
                destination.flush()
                os.fsync(destination.fileno())
                os.fchmod(destination.fileno(), 0o400)
                os.fsync(destination.fileno())
            with open_readonly(path) as checking:
                before = full11(os.fstat(checking.fileno()))
                need(before['st_blocks'] * 512 <= charge, 'Scratch allocation exceeds reserved physical bytes')
                verify = hashlib.sha256()
                actual = 0
                while data := checking.read(CHUNK):
                    actual += len(data)
                    verify.update(data)
                need(actual == count and verify.digest() == digest.digest(), 'Nested ZIP scratch readback mismatch')
                need(full11(os.fstat(checking.fileno())) == before, 'Nested ZIP scratch changed at readback')
                checking.seek(0)
                self._emit({'kind': 'scratch-sealed', 'chain': chain, 'path': str(path),
                            'bytes': count, 'sha256': digest.hexdigest(), 'full11': before})
                yield checking, count, digest.hexdigest()
                need(full11(os.fstat(checking.fileno())) == before, 'Nested ZIP scratch changed during audit')
        finally:
            found = os.lstat(path)
            need((found.st_dev, found.st_ino, found.st_uid, found.st_nlink) ==
                 (owned.st_dev, owned.st_ino, os.getuid(), 1), 'Refusing changed owned scratch release')
            self._emit({'kind': 'scratch-release-intent', 'chain': chain, 'path': str(path),
                        'full11': full11(found), 'derivedOnly': True})
            os.unlink(path)
            self.active_scratch -= charge
            self._emit({'kind': 'scratch-released', 'chain': chain, 'path': str(path),
                        'activeScratchBytes': self.active_scratch})

    def audit_file(self, path, logical_name, expected_sha256=None, expected_bytes=None):
        try:
            with open_readonly(path) as source:
                before = full11(os.fstat(source.fileno()))
                result = self.audit_seekable(source, logical_name, expected_sha256, expected_bytes)
                need(full11(os.fstat(source.fileno())) == before, 'Input full11 changed during audit')
                need(full11(os.lstat(path)) == before, 'Input pathname identity changed during audit')
                result['sourceFull11Unchanged'] = True
                result['sourceFull11'] = before
                return result
        except (zipfile.BadZipFile, tarfile.TarError, transport.ArchiveError,
                EOFError, OSError, struct.error, UnicodeError, strict_zip.ZipStreamError) as error:
            raise AuditError(str(error)) from error

    def audit_seekable(self, source, logical_name, expected_sha256=None, expected_bytes=None):
        source.seek(0, io.SEEK_END)
        size = source.tell()
        need(0 <= size <= self.config.max_decoded_bytes, 'Source archive exceeds byte limit')
        if expected_bytes is not None:
            need(type(expected_bytes) is int and size == expected_bytes, 'Source byte length mismatch')
        source.seek(0)
        digest = hashlib.sha256()
        count = 0
        while data := source.read(CHUNK):
            count += len(data)
            need(count <= size, 'Source archive grew during hash')
            digest.update(data)
        need(count == size, 'Source archive truncated during hash')
        sha = digest.hexdigest()
        if expected_sha256 is not None:
            need(isinstance(expected_sha256, str) and re.fullmatch('[0-9a-f]{64}', expected_sha256) and
                 sha == expected_sha256, 'Source SHA256 mismatch')
        source.seek(0)
        chain = [logical_name]
        if logical_name.lower().endswith(('.zip', '.jar', '.aar', '.klib')):
            details = self._zip(source, size, chain, 0)
            kind = 'zip'
        else:
            result = self._stream(source, logical_name, size, chain, 0)
            need(result['sha256'] == sha, 'Source sequential audit hash mismatch')
            details, kind = result.get('details'), result['kind']
        self._checkpoint(force=True)
        result = {'kind': kind, 'bytes': size, 'sha256': sha, 'details': details,
                  'peakScratchBytes': self.peak_scratch, 'processPeakRssBytes': self.peak_rss,
                  'recordCount': self.record_count}
        self._emit({'kind': 'archive-root-complete', 'chain': chain,
                    'payloadKind': result['kind'], **{k: v for k, v in result.items() if k != 'kind'}})
        return result

    def _zip_layout(self, source, size):
        need(size >= 22, 'Truncated ZIP end record')
        tail_count = min(size, 65557)
        source.seek(size - tail_count)
        tail = _read_exact(source, tail_count)
        end = tail.rfind(b'PK\x05\x06')
        need(end >= 0 and end + 22 <= len(tail), 'Missing ZIP end record')
        signature, disk, cd_disk, disk_entries, entries, cd_bytes, cd_offset, comment_bytes = \
            struct.unpack_from('<4s4H2IH', tail, end)
        eocd_at = size - tail_count + end
        need(end + 22 + comment_bytes == len(tail), 'Trailing or truncated ZIP comment bytes')
        need(disk == cd_disk == 0 and disk_entries == entries, 'Multidisk ZIP is unsupported')
        central_end = eocd_at
        locator_present = eocd_at >= 20 and tail[max(0, end - 20):max(0, end - 16)] == b'PK\x06\x07'
        if entries == 0xffff or cd_bytes == 0xffffffff or cd_offset == 0xffffffff or locator_present:
            need(eocd_at >= 20, 'Missing ZIP64 locator')
            source.seek(eocd_at - 20)
            locator = _read_exact(source, 20)
            lsig, ldisk, record_at, disks = struct.unpack('<4sIQI', locator)
            need(lsig == b'PK\x06\x07' and ldisk == 0 and disks == 1, 'Invalid ZIP64 locator')
            source.seek(record_at)
            data = _read_exact(source, 56)
            values = struct.unpack('<4sQ2H2I4Q', data)
            need(values[0] == b'PK\x06\x06' and values[1] == 44 and
                 values[4] == values[5] == 0 and values[6] == values[7], 'Invalid ZIP64 end record')
            need(record_at + 56 == eocd_at - 20, 'Unexpected ZIP64 extension or trailing bytes')
            actual_entries, actual_cd_bytes, actual_cd_offset = values[7:10]
            need(entries in (0xffff, actual_entries) and cd_bytes in (0xffffffff, actual_cd_bytes) and
                 cd_offset in (0xffffffff, actual_cd_offset), 'ZIP64 and ordinary end records disagree')
            entries, cd_bytes, cd_offset = actual_entries, actual_cd_bytes, actual_cd_offset
            central_end = record_at
        need(0 < entries <= self.config.max_entries, 'ZIP entry count limit exceeded or empty archive')
        need(0 < cd_bytes <= self.config.max_cd_bytes, 'ZIP central-directory metadata limit exceeded')
        need(cd_offset + cd_bytes == central_end and cd_offset >= 0,
             'ZIP central-directory boundary differs from end records')
        # CPython's ZipFile accepts short reads of variable central fields.
        # Prove the complete declared central byte region independently.
        source.seek(cd_offset)
        consumed = 0
        for _ in range(entries):
            need(consumed + 46 <= cd_bytes, 'Truncated ZIP central fixed header')
            header = _read_exact(source, 46)
            fields = struct.unpack('<4s6H3I5H2I', header)
            need(fields[0] == b'PK\x01\x02', 'Invalid ZIP central header signature')
            name_size, extra_size, comment_size, start_disk = fields[10:14]
            need(0 < name_size <= 4096 and start_disk == 0,
                 'ZIP central filename bound or start disk differs')
            variable_bytes = name_size + extra_size + comment_size
            need(consumed + 46 + variable_bytes <= cd_bytes,
                 'Truncated ZIP central variable metadata')
            _read_exact(source, name_size)
            extra_fields(_read_exact(source, extra_size))
            _read_exact(source, comment_size)
            consumed += 46 + variable_bytes
        need(consumed == cd_bytes and source.tell() == central_end,
             'Extra/missing central directory bytes or entries')
        source.seek(0)
        need(source.read(4) == b'PK\x03\x04', 'ZIP has an unexpected prefix or no local entries')
        return entries, cd_bytes, cd_offset

    def _zip(self, source, size, chain, depth):
        need(depth <= self.config.max_depth, 'Archive nesting depth limit exceeded')
        self._checkpoint(force=True)
        # Android's genuine resource-only AAR carries a canonical empty
        # classes.jar. This exact nested-JAR shape is the only empty-container
        # exception; top-level and other empty archive kinds remain rejected.
        if size == 22 and depth > 0 and chain[-1].lower().endswith('.jar'):
            source.seek(0)
            need(_read_exact(source, 22) == b'PK\x05\x06' + b'\x00' * 18 and not source.read(1),
                 'Empty nested JAR is not the exact canonical22-byte EOCD')
            details = {'entryCount': 0, 'centralDirectoryBytes': 0, 'decodedBytes': 0,
                       'allMemberCRCAndLengthVerified': True, 'strictLocalCentralAndEOCDVerified': True,
                       'canonicalEmptyNestedJAR': True}
            self._emit({'kind': 'archive-complete', 'format': 'zip', 'chain': chain, **details})
            return details
        entries, cd_bytes, cd_offset = self._zip_layout(source, size)
        with zipfile.ZipFile(source, 'r') as archive:
            infos = archive.infolist()
            need(len(infos) == entries and archive.start_dir == cd_offset, 'ZIP central-directory count/offset mismatch')
            self._checkpoint(force=True)
            seen, offsets, decoded = set(), set(), 0
            regular_paths, all_paths, payload_starts = set(), set(), {}
            for info in infos:
                directory = info.is_dir()
                need(info.orig_filename == info.filename, 'ZIP embedded NUL or altered member name')
                name = safe_name(info.filename, directory=directory)
                need(name.casefold() not in seen, 'Duplicate ZIP member path')
                seen.add(name.casefold())
                all_paths.add(name.casefold())
                if not directory:
                    regular_paths.add(name.casefold())
                need(not info.flag_bits & 0x2041, 'Encrypted/masked ZIP member')
                need(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                     'ZIP compression method is outside the declared STORE/DEFLATE contract')
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                need(kind in (0, stat.S_IFDIR if directory else stat.S_IFREG), 'Nonregular ZIP member')
                need(0 <= info.file_size <= self.config.max_decoded_bytes, 'ZIP member size limit exceeded')
                decoded += info.file_size
                need(decoded <= self.config.max_decoded_bytes, 'ZIP decoded byte limit exceeded')
                need(info.header_offset not in offsets, 'Duplicate ZIP local-header offset')
                offsets.add(info.header_offset)
                extra_fields(info.extra)
            for name in all_paths:
                need(all(str(parent) not in regular_paths for parent in PurePosixPath(name).parents if str(parent) != '.'),
                     'ZIP file conflicts with descendant path')
            # Validate local boundaries/descriptors independently of ZipFile's CRC reader.
            ordered = sorted(infos, key=lambda item: item.header_offset)
            need(ordered[0].header_offset == 0, 'ZIP local header prefix is unsupported')
            for index, info in enumerate(ordered):
                source.seek(info.header_offset)
                header = _read_exact(source, 30)
                signature, version, flags, method, mtime, mdate, crc, compressed, uncompressed, name_bytes, extra_bytes = \
                    struct.unpack('<4s5H3I2H', header)
                need(signature == b'PK\x03\x04' and flags == info.flag_bits and method == info.compress_type,
                     'ZIP local header differs from central directory')
                need(name_bytes <= 4096 and extra_bytes <= 65535, 'ZIP local metadata bound exceeded')
                encoded_name = _read_exact(source, name_bytes)
                need(encoded_name.decode('utf-8' if flags & 0x800 else 'cp437') == info.orig_filename,
                     'ZIP local member name differs from central directory')
                local_extra = extra_fields(_read_exact(source, extra_bytes))
                local_compressed, local_uncompressed = compressed, uncompressed
                if compressed == 0xffffffff or uncompressed == 0xffffffff:
                    need(1 in local_extra, 'Missing local ZIP64 size identity')
                    extended, cursor = local_extra[1], 0
                    if uncompressed == 0xffffffff:
                        need(cursor + 8 <= len(extended), 'Truncated local ZIP64 uncompressed length')
                        local_uncompressed = struct.unpack_from('<Q', extended, cursor)[0]
                        cursor += 8
                    if compressed == 0xffffffff:
                        need(cursor + 8 <= len(extended), 'Truncated local ZIP64 compressed length')
                        local_compressed = struct.unpack_from('<Q', extended, cursor)[0]
                        cursor += 8
                    need(cursor == len(extended), 'Unexpected local ZIP64 extension bytes')
                payload_start = source.tell()
                payload_starts[info.header_offset] = payload_start
                payload_end = payload_start + info.compress_size
                boundary = ordered[index + 1].header_offset if index + 1 < len(ordered) else cd_offset
                need(payload_end <= boundary, 'Overlapping ZIP member ranges')
                if flags & 8:
                    need(crc in (0, info.CRC) and local_compressed in (0, info.compress_size) and
                         local_uncompressed in (0, info.file_size), 'ZIP streaming local identity differs')
                    source.seek(payload_end)
                    first = _read_exact(source, 4)
                    has_signature = first == b'PK\x07\x08'
                    zip64 = compressed == 0xffffffff or uncompressed == 0xffffffff
                    length = 20 if zip64 else 12
                    descriptor = _read_exact(source, length) if has_signature else first + _read_exact(source, length - 4)
                    actual_crc, actual_compressed, actual_uncompressed = struct.unpack('<IQQ' if zip64 else '<III', descriptor)
                    need((actual_crc, actual_compressed, actual_uncompressed) ==
                         (info.CRC, info.compress_size, info.file_size), 'ZIP data descriptor differs')
                    payload_end += length + (4 if has_signature else 0)
                else:
                    need(crc == info.CRC and local_compressed == info.compress_size and
                         local_uncompressed == info.file_size, 'ZIP local byte/CRC declaration differs')
                need(payload_end == boundary, 'Unexpected bytes between ZIP entries or central directory')
            for info in infos:
                name = info.filename
                if info.is_dir():
                    need(info.file_size == info.CRC == 0, 'Nonempty ZIP directory')
                    with strict_zip.RawZipReader(source, payload_starts[info.header_offset], info.compress_size,
                                                 info.file_size, info.CRC, info.compress_type) as empty:
                        need(not empty.read(1), 'Nonempty ZIP directory payload')
                    self._emit({'kind': 'directory', 'chain': [*chain, name], 'container': 'zip'})
                    continue
                with strict_zip.RawZipReader(source, payload_starts[info.header_offset], info.compress_size,
                                             info.file_size, info.CRC, info.compress_type) as stream:
                    result = self._stream(stream, name, info.file_size, [*chain, name], depth + 1)
                self._emit({'kind': 'zip-member', 'chain': [*chain, name], 'bytes': result['bytes'],
                            'sha256': result['sha256'], 'crc32': f'{info.CRC:08x}',
                            'crcVerified': True, 'payloadKind': result['kind'], 'details': result.get('details')})
                self._checkpoint()
        details = {'entryCount': entries, 'centralDirectoryBytes': cd_bytes,
                   'decodedBytes': decoded, 'allMemberCRCAndLengthVerified': True,
                   'strictLocalCentralAndEOCDVerified': True}
        self._emit({'kind': 'archive-complete', 'format': 'zip', 'chain': chain, **details})
        return details

    def _stream(self, stream, name, size, chain, depth):
        need(depth <= self.config.max_depth, 'Archive nesting depth limit exceeded')
        need(type(size) is int and 0 <= size <= self.config.max_decoded_bytes, 'Member size exceeds limit')
        source = _HashReader(stream, size)
        suffix = name.lower()
        details, kind = None, 'file'
        if suffix.endswith(('.jar', '.aar', '.klib', '.zip')):
            with self._spool(source, size, chain) as (scratch, copied, copied_sha):
                details = self._zip(scratch, copied, chain, depth)
                need(copied_sha == source.sha.hexdigest(), 'Nested ZIP copied hash mismatch')
            kind = 'zip'
        elif suffix.endswith(('.tar.gz', '.tar.xz')):
            details = self._tar(source, 'xz' if suffix.endswith('.xz') else 'gzip', chain, depth)
            kind = 'tar'
        elif suffix.endswith('.a'):
            details = self._ar(source)
            kind = 'ar'
        else:
            while source.read(CHUNK):
                pass
            details = binary_identity(bytes(source.prefix))
            if details is not None:
                kind = 'binary'
        need(not source.read(1) and source.count == size, 'Member byte length differs from declaration')
        return {'kind': kind, 'bytes': source.count, 'sha256': source.sha.hexdigest(), 'details': details}

    def _tar(self, source, codec, chain, depth):
        decoder = transport._Decoder(source, codec, memory_limit=self.config.xz_memory_limit,
                                     max_decoded_bytes=self.config.max_decoded_bytes)
        buffered = io.BufferedReader(transport._ChunkReader(decoder.chunks()), buffer_size=CHUNK)
        seen, count, member_bytes, last_end = set(), 0, 0, 0
        regular_paths, directory_count, file_count = set(), 0, 0
        required_directories = set()
        try:
            with tarfile.open(fileobj=buffered, mode='r|', tarinfo=transport._LimitedTarInfo) as archive:
                for member in archive:
                    directory = member.type == tarfile.DIRTYPE
                    name = safe_name(member.name[:-1] if directory and member.name.endswith('/') else member.name)
                    need(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) and member.sparse is None,
                         'Only regular files and canonical directories are allowed in TAR')
                    need(not directory or member.size == 0 and not member.linkname,
                         'TAR directory has a payload or link target')
                    need(name.casefold() not in seen, 'Duplicate TAR member path')
                    folded = name.casefold()
                    parents = {str(parent) for parent in PurePosixPath(folded).parents if str(parent) != '.'}
                    need(not parents.intersection(regular_paths) and (directory or folded not in required_directories),
                         'TAR file conflicts with descendant path')
                    required_directories.update(parents)
                    seen.add(folded)
                    if not directory:
                        regular_paths.add(folded)
                    count += 1
                    need(count <= self.config.max_entries, 'TAR member count limit exceeded')
                    need(0 <= member.size <= self.config.max_decoded_bytes, 'TAR member size limit exceeded')
                    member_bytes += member.size
                    need(member_bytes <= self.config.max_decoded_bytes, 'TAR member decoded byte limit exceeded')
                    if directory:
                        directory_count += 1
                        self._emit({'kind': 'directory', 'container': 'tar', 'chain': [*chain, name],
                                    'bytes': 0, 'archiveMode': member.mode, 'mtime': member.mtime})
                        last_end = member.offset_data
                        archive.members.clear()
                        self._checkpoint()
                        continue
                    file_count += 1
                    with archive.extractfile(member) as data:
                        result = self._stream(data, name, member.size, [*chain, name], depth + 1)
                    self._emit({'kind': 'tar-member', 'chain': [*chain, name], 'bytes': result['bytes'],
                                'sha256': result['sha256'], 'archiveMode': member.mode, 'mtime': member.mtime,
                                'payloadKind': result['kind'], 'details': result.get('details')})
                    last_end = member.offset_data + ((member.size + 511) // 512) * 512
                    archive.members.clear()
                    self._checkpoint()
                padding = archive.fileobj.tell() - last_end
                while tail := archive.fileobj.read(CHUNK):
                    need(not tail.strip(b'\x00'), 'Nonzero data follows TAR end marker')
                    padding += len(tail)
                need(padding >= 1024, 'TAR end markers are truncated')
        finally:
            buffered.close()
        need(file_count > 0, 'Empty/directory-only TAR archive is outside staged contract')
        details = {'format': codec, **decoder.summary(), 'memberCount': file_count,
                   'entryCount': count, 'directoryCount': directory_count,
                   'memberBytes': member_bytes, 'strictCompressedEOFAndZeroTarEndVerified': True}
        self._emit({'kind': 'archive-complete', 'format': 'tar', 'chain': chain, **details})
        return details

    def _ar(self, source):
        need(_read_exact(source, 8) == b'!<arch>\n', 'Invalid/thin native AR archive')
        count, objects, special, longnames = 0, Counter(), 0, b''
        while True:
            first = source.read(1)
            if not first:
                break
            header = first + _read_exact(source, 59)
            need(header[-2:] == b'`\n', 'Malformed AR member header')
            try:
                name = header[:16].decode('ascii').rstrip()
                raw_size = header[48:58].decode('ascii').strip()
                need(re.fullmatch('[0-9]+', raw_size), 'Malformed AR member size')
                size = int(raw_size)
            except UnicodeDecodeError as error:
                raise AuditError('Non-ASCII AR header') from error
            need(size <= self.config.max_decoded_bytes, 'AR member exceeds decoded limit')
            count += 1
            need(count <= self.config.max_entries, 'AR member count limit exceeded')
            if name == '//':
                need(size <= MiB, 'AR long-name table exceeds metadata limit')
                longnames = _read_exact(source, size)
                special += 1
            else:
                effective_name = name
                if name.startswith('#1/'):
                    need(re.fullmatch('#1/[0-9]+', name), 'Malformed BSD AR name length')
                    length = int(name[3:])
                    need(0 < length <= min(4096, size), 'BSD AR name length limit exceeded')
                    raw_name = _read_exact(source, length)
                    need(b'\x00' not in raw_name.rstrip(b'\x00'), 'Invalid BSD AR member name')
                    effective_name = raw_name.rstrip(b'\x00').decode('utf-8')
                    size -= length
                elif re.fullmatch('/[0-9]+', name):
                    offset = int(name[1:])
                    need(offset < len(longnames), 'AR name offset outside long-name table')
                    ending = longnames.find(b'/\n', offset)
                    if ending < 0:
                        ending = longnames.find(b'\x00', offset)
                    need(ending >= offset, 'Unterminated AR long member name')
                    effective_name = longnames[offset:ending].decode('utf-8')
                elif effective_name.endswith('/') and effective_name not in ('/', '/SYM64/'):
                    effective_name = effective_name[:-1]
                if effective_name not in ('/', '/SYM64/', '__.SYMDEF', '__.SYMDEF SORTED'):
                    safe_name(effective_name)
                prefix = bytearray()
                remaining = size
                while remaining:
                    data = source.read(min(CHUNK, remaining))
                    need(data, 'Truncated AR member')
                    prefix.extend(data[:max(0, 4096 - len(prefix))])
                    remaining -= len(data)
                if effective_name in ('/', '/SYM64/', '__.SYMDEF', '__.SYMDEF SORTED'):
                    special += 1
                else:
                    identity = binary_identity(bytes(prefix))
                    key = ('unknown' if identity is None else identity['format'] + ':' + str(identity.get('machine', 'unknown')))
                    objects[key] += 1
            # BSD names are included in the original size used for even padding.
            original_size = int(raw_size)
            if original_size & 1:
                need(_read_exact(source, 1) == b'\n', 'Invalid AR member padding')
            self._checkpoint()
        need(count > 0 and objects, 'AR archive has no object members')
        return {'memberCount': count, 'specialMembers': special, 'objectKindsAndMachines': dict(sorted(objects.items()))}
