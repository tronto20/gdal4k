"""Lossless, bounded gzip/XZ transport for staged CI TAR files.

Only outer CI containers change. Maven payloads and historical evidence remain
opaque bytes. XZ is optional until explicitly selected or checked.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys
import tarfile
import tempfile
import zlib

CHUNK = 64 * 1024
XZ_MEMORY_LIMIT = 128 * 1024**2
MAX_DECODED_BYTES = 16 * 1024**3
MAX_MEMBERS = 100_000
MAX_METADATA_BYTES = 1024 * 1024
SUFFIXES = {'gzip': '.tar.gz', 'xz': '.tar.xz'}
MAGIC = {'gzip': b'\x1f\x8b\x08', 'xz': b'\xfd7zXZ\x00'}


class ArchiveError(ValueError):
    pass


class _LimitedTarInfo(tarfile.TarInfo):
    # TarInfo reads PAX/GNU name bodies before yielding a regular member. Bound
    # those allocations separately; the128MiB XZ limit is not a process-RSS cap.
    def _proc_pax(self, archive):
        _need(0 <= self.size <= MAX_METADATA_BYTES, 'TAR metadata header exceeds the byte limit')
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        _need(0 <= self.size <= MAX_METADATA_BYTES, 'TAR metadata header exceeds the byte limit')
        return super()._proc_gnulong(archive)

    def _reject_sparse(self, *args):
        raise ArchiveError('Sparse TAR members are not staged regular files')

    _proc_sparse = _reject_sparse
    _proc_gnusparse_00 = _reject_sparse
    _proc_gnusparse_01 = _reject_sparse
    _proc_gnusparse_10 = _reject_sparse


def _need(value, message):
    if not value:
        raise ArchiveError(message)


def _lzma():
    try:
        import lzma
    except ImportError as error:
        raise ArchiveError('XZ requires the optional Python lzma/_lzma extension') from error
    return lzma


def _format(path):
    for codec, suffix in SUFFIXES.items():
        if str(path).endswith(suffix):
            return codec
    raise ArchiveError('Expected a declared .tar.gz or .tar.xz archive: ' + str(path))


def _stat(path):
    value = Path(path).stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _name(name):
    _need(isinstance(name, str) and name and '\\' not in name and ':' not in name,
          'Unsafe TAR member path')
    path = PurePosixPath(name)
    _need(bool(path.parts) and not path.is_absolute() and str(path) == name and all(p not in ('', '.', '..') for p in path.parts),
          'Unsafe TAR member path: ' + name)
    reserved = {'con', 'prn', 'aux', 'nul', *('com' + str(i) for i in range(1, 10)),
                *('lpt' + str(i) for i in range(1, 10))}
    _need(all(p.rstrip(' .') == p and p.split('.')[0].casefold() not in reserved for p in path.parts),
          'Ambiguous cross-platform TAR member path: ' + name)
    return path


def _target(destination, name):
    path = destination.joinpath(*_name(name).parts)
    current = destination
    parts = _name(name).parts
    for index, part in enumerate(parts):
        current = current / part
        _need(not current.is_symlink(), 'Refusing an existing destination symlink: ' + str(current))
        if index < len(parts) - 1:
            _need(not current.exists() or current.is_dir(), 'Destination parent is not a directory: ' + str(current))
    _need(not path.exists() or path.is_file(), 'Destination member is not a regular file: ' + str(path))
    return path


class _Encoder:
    def __init__(self, output, codec):
        self.output = output
        self.compressor = (_lzma().LZMACompressor(format=_lzma().FORMAT_XZ, check=_lzma().CHECK_CRC64, preset=6)
                           if codec == 'xz' else zlib.compressobj(level=9, wbits=31))
        self.decoded_hash, self.archive_hash = hashlib.sha256(), hashlib.sha256()
        self.decoded_bytes = self.archive_bytes = 0

    def _emit(self, data):
        self.output.write(data)
        self.archive_hash.update(data)
        self.archive_bytes += len(data)

    def write(self, data):
        self.decoded_hash.update(data)
        self.decoded_bytes += len(data)
        self._emit(self.compressor.compress(data))
        return len(data)

    def finish(self):
        self._emit(self.compressor.flush())
        return dict(archiveBytes=self.archive_bytes, archiveSha256=self.archive_hash.hexdigest(),
                    decodedTarBytes=self.decoded_bytes, decodedTarSha256=self.decoded_hash.hexdigest())


class _Decoder:
    def __init__(self, source, codec, *, memory_limit=XZ_MEMORY_LIMIT, max_decoded_bytes=MAX_DECODED_BYTES):
        _need(memory_limit > 0 and max_decoded_bytes > 0, 'Decode limits must be positive')
        self.source, self.codec = source, codec
        self.decoder = (_lzma().LZMADecompressor(format=_lzma().FORMAT_XZ, memlimit=memory_limit)
                        if codec == 'xz' else zlib.decompressobj(31))
        self.archive_hash, self.decoded_hash = hashlib.sha256(), hashlib.sha256()
        self.archive_bytes = self.decoded_bytes = 0
        self.limit = max_decoded_bytes

    def chunks(self):
        pending = b''
        try:
            while not self.decoder.eof:
                needs_input = self.decoder.needs_input if self.codec == 'xz' else not pending
                if needs_input:
                    data = self.source.read(CHUNK)
                    _need(data, 'Truncated ' + self.codec + ' archive')
                    self.archive_hash.update(data)
                    self.archive_bytes += len(data)
                else:
                    data = b'' if self.codec == 'xz' else pending
                result = self.decoder.decompress(data, CHUNK)
                if self.codec == 'gzip':
                    pending = self.decoder.unconsumed_tail
                self.decoded_bytes += len(result)
                _need(self.decoded_bytes <= self.limit, 'Decoded TAR exceeds the byte limit')
                if result:
                    self.decoded_hash.update(result)
                    yield result
            _need(not self.decoder.unused_data and not self.source.read(1),
                  'Trailing data or multiple compressed streams are not allowed')
        except (zlib.error, EOFError) as error:
            raise ArchiveError('Invalid ' + self.codec + ' archive: ' + str(error)) from error
        except Exception as error:
            if type(error).__module__ == '_lzma':
                raise ArchiveError('Invalid or over-memory-limit XZ archive: ' + str(error)) from error
            raise

    def summary(self):
        return dict(archiveBytes=self.archive_bytes, archiveSha256=self.archive_hash.hexdigest(),
                    decodedTarBytes=self.decoded_bytes, decodedTarSha256=self.decoded_hash.hexdigest())


class _ChunkReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks, self.pending = iter(chunks), b''

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            self.pending = next(self.chunks, b'')
            if not self.pending:
                return 0
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


class _HashReader:
    def __init__(self, source):
        self.source, self.hash, self.bytes = source, hashlib.sha256(), 0

    def read(self, size=-1):
        _need(0 <= size <= CHUNK, 'Unbounded source file read requested')
        data = self.source.read(size)
        self.hash.update(data)
        self.bytes += len(data)
        return data


def write_archive(output, files, root):
    """Serialize the caller's ordered regular files once, then atomically publish."""
    output, root = Path(output), Path(root).resolve()
    codec = _format(output)
    if codec == 'xz':
        _lzma()  # Fail before creating a candidate when this optional codec is absent.
    _need(not output.is_symlink(), 'Refusing a symlink archive output')
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = None
    members, seen = [], set()
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.' + output.name + '-', suffix='.tmp', delete=False) as raw:
            candidate = Path(raw.name)
            encoder = _Encoder(raw, codec)
            with tarfile.open(fileobj=encoder, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for source in files:
                    source = Path(source)
                    _need(not source.is_symlink() and source.is_file() and source.resolve().is_relative_to(root),
                          'Archive input must be a regular owned file: ' + str(source))
                    name = source.relative_to(root).as_posix()
                    _name(name)
                    _need(name.casefold() not in seen, 'Duplicate archive input: ' + name)
                    _need(source.resolve() != output.resolve(), 'Output cannot be an input member')
                    seen.add(name.casefold())
                    _need(len(seen) <= MAX_MEMBERS, 'Archive member limit exceeded')
                    before = _stat(source)
                    info = archive.gettarinfo(str(source), arcname=name)
                    _need(info.isfile(), 'Only regular archive inputs are allowed')
                    with source.open('rb') as stream:
                        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'Archive input changed type')
                        reader = _HashReader(stream)
                        archive.addfile(info, reader)
                        _need(reader.bytes == info.size and not stream.read(1), 'Archive input size changed')
                    _need(before == _stat(source), 'Archive input changed during serialization: ' + name)
                    members.append(dict(path=name, bytes=info.size, mode=info.mode, sha256=reader.hash.hexdigest()))
            _need(members, 'Refusing an empty staged archive')
            summary = encoder.finish()
            raw.flush()
        # Validate the actual candidate through decoder EOF, including all TAR
        # headers/padding. No raw TAR file or whole-TAR RAM copy is created.
        with candidate.open('rb') as stream:
            decoder = _Decoder(stream, codec, max_decoded_bytes=max(summary['decodedTarBytes'], 1))
            for _ in decoder.chunks():
                pass
        _need(decoder.summary() == summary, 'Compressed candidate changed the exact TAR stream')
        os.replace(candidate, output)
        candidate = None
        return dict(format=codec, archive=str(output), **summary, memberCount=len(members), members=members)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def _scan(path, destination, *, memory_limit, max_decoded_bytes, max_members,
          expected=None, installed=None):
    codec = _format(path)
    before = _stat(path)
    members, seen = [], set()
    with path.open('rb') as source:
        _need(source.read(len(MAGIC[codec])) == MAGIC[codec], 'Archive suffix/magic mismatch: ' + str(path))
        source.seek(0)
        decoder = _Decoder(source, codec, memory_limit=memory_limit, max_decoded_bytes=max_decoded_bytes)
        buffered = io.BufferedReader(_ChunkReader(decoder.chunks()), buffer_size=CHUNK)
        try:
            with tarfile.open(fileobj=buffered, mode='r|', tarinfo=_LimitedTarInfo) as archive:
                last_end = 0
                for member in archive:
                    name = member.name
                    _name(name)
                    _need(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and member.sparse is None,
                          'Only regular TAR members are allowed')
                    _need(0 <= member.size <= max_decoded_bytes, 'TAR member size exceeds the byte limit')
                    _need(name.casefold() not in seen, 'Duplicate TAR member: ' + name)
                    seen.add(name.casefold())
                    _need(len(seen) <= max_members, 'TAR member limit exceeded')
                    target = _target(destination, name)
                    filtered = tarfile.data_filter(member, str(destination))
                    _need(filtered is not None, 'TAR data filter rejected a member')
                    effective_mode = filtered.mode
                    record = dict(path=name, bytes=member.size, archiveMode=member.mode,
                                  restoredMode=effective_mode, mtime=member.mtime)
                    temporary = None
                    try:
                        writing = installed is not None and name not in installed
                        if writing:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            _target(destination, name)
                            temporary = tempfile.NamedTemporaryFile(dir=target.parent, prefix='.archive-restore-', delete=False)
                        digest, count = hashlib.sha256(), 0
                        stream = archive.extractfile(member)
                        while chunk := stream.read(CHUNK):
                            digest.update(chunk)
                            count += len(chunk)
                            if temporary is not None:
                                temporary.write(chunk)
                        _need(count == member.size, 'Truncated TAR file payload')
                        record['sha256'] = digest.hexdigest()
                        if expected is not None:
                            _need(len(members) < len(expected['members']) and record == expected['members'][len(members)],
                                  'Archive changed between validation and restore')
                        if temporary is not None:
                            temporary.close()
                            os.chmod(temporary.name, effective_mode)
                            os.utime(temporary.name, (member.mtime, member.mtime))
                            _target(destination, name)
                            os.replace(temporary.name, target)
                            temporary = None
                            installed.add(name)
                    finally:
                        if temporary is not None:
                            temporary.close()
                            Path(temporary.name).unlink(missing_ok=True)
                    members.append(record)
                    last_end = member.offset_data + ((member.size + 511) // 512) * 512
                # tarfile stops at the first end marker. Drain its own stream
                # (including prefetched bytes) to compressed EOF and reject a
                # hidden second TAR/nonzero trailing decoded payload.
                padding = archive.fileobj.tell() - last_end
                while tail := archive.fileobj.read(CHUNK):
                    _need(not tail.strip(b'\x00'), 'Nonzero data follows the TAR end marker')
                    padding += len(tail)
                _need(padding >= 1024, 'TAR end markers are truncated')
        except (tarfile.TarError, OSError) as error:
            raise ArchiveError('Invalid TAR archive: ' + str(error)) from error
        finally:
            buffered.close()
    _need(members, 'Refusing an empty staged archive')
    _need(before == _stat(path), 'Archive changed while reading: ' + str(path))
    summary = dict(format=codec, archive=str(path), **decoder.summary(), memberCount=len(members), members=members)
    if expected is not None:
        _need(summary == expected, 'Archive identity changed between validation and restore')
    return summary


def restore_archives(directory, destination, expected, *, memory_limit=XZ_MEMORY_LIMIT,
                     max_decoded_bytes=MAX_DECODED_BYTES, max_members=MAX_MEMBERS):
    """Validate every selected archive before any destination writes, then restore."""
    directory, destination = Path(directory), Path(destination).resolve()
    expected = list(expected)
    _need(expected and len(expected) == len(set(expected)), 'Expected logical archive names must be nonempty and unique')
    _need(all(name and '/' not in name and '\\' not in name and ':' not in name for name in expected), 'Unsafe expected archive name')
    selected = {}
    for path in directory.iterdir():
        _need(path.is_file() and not path.is_symlink(), 'Unexpected download entry: ' + str(path))
        codec = _format(path)
        name = path.name[:-len(SUFFIXES[codec])]
        _need(name in expected, 'Unknown logical archive: ' + name)
        _need(name not in selected, 'Ambiguous gzip/XZ candidates for: ' + name)
        selected[name] = path
    _need(set(selected) == set(expected), 'Required logical archives are missing')
    summaries, combined, case_names, decoded_total, duplicates, total_members = [], {}, {}, 0, 0, 0
    input_keys = [path.resolve().as_posix().casefold() for path in selected.values()]
    for name in expected:
        summary = _scan(selected[name], destination, memory_limit=memory_limit,
                        max_decoded_bytes=max_decoded_bytes, max_members=max_members)
        decoded_total += summary['decodedTarBytes']
        _need(decoded_total <= max_decoded_bytes, 'Combined decoded TAR byte limit exceeded')
        total_members += summary['memberCount']
        _need(total_members <= max_members, 'Combined TAR member limit exceeded')
        for member in summary['members']:
            path = member['path']
            target = _target(destination, path)
            target_key = target.resolve().as_posix().casefold()
            _need(not any(target_key == source or target_key.startswith(source + '/') or source.startswith(target_key + '/')
                          for source in input_keys), 'Destination overlaps a selected input archive')
            _need(not target.exists() or all(not target.samefile(source) for source in selected.values()),
                  'Destination aliases a selected input archive')
            _need(path.casefold() not in case_names or case_names[path.casefold()] == path,
                  'Conflicting cross-platform member spelling')
            case_names[path.casefold()] = path
            identity = member['bytes'], member['sha256'], member['restoredMode']
            _need(path not in combined or combined[path] == identity, 'Conflicting member across archives: ' + path)
            duplicates += path in combined
            combined[path] = identity
        summaries.append(summary)
    # A regular file cannot also be another member's parent directory.
    for path in combined:
        _need(not any(str(parent).casefold() in case_names for parent in PurePosixPath(path).parents if str(parent) != '.'),
              'Conflicting file/directory member paths')
    installed = set()
    for summary in summaries:
        _scan(Path(summary['archive']), destination, memory_limit=memory_limit,
              max_decoded_bytes=max_decoded_bytes, max_members=max_members,
              expected=summary, installed=installed)
    return dict(result='restored', archives=summaries, restoredMembers=len(installed),
                identicalDuplicateMembers=duplicates, decodedTarBytes=decoded_total,
                limits=dict(xzDecoderMemoryBytes=memory_limit, decodedTarBytes=max_decoded_bytes,
                            totalMembers=max_members, metadataHeaderBytes=MAX_METADATA_BYTES,
                            memoryScope='XZ decoder allocation limit, not a whole-process resident-memory bound.'))


def check_codecs(*, memory_limit=XZ_MEMORY_LIMIT):
    """Exercise both real codecs and the optional native extension without files."""
    payload = b'gdal4k lossless CI archive codec check\n' * 32
    checks = []
    for codec in ['gzip', 'xz']:
        output = io.BytesIO()
        encoder = _Encoder(output, codec)
        encoder.write(payload)
        summary = encoder.finish()
        decoder = _Decoder(io.BytesIO(output.getvalue()), codec, memory_limit=memory_limit,
                           max_decoded_bytes=len(payload))
        actual = b''.join(decoder.chunks())
        _need(actual == payload and decoder.summary() == summary, 'Codec self-check failed: ' + codec)
        checks.append(dict(format=codec, **summary))
    import _lzma
    return dict(result='passed', python=platform.python_version(),
                zlibCompileVersion=zlib.ZLIB_VERSION, zlibRuntimeVersion=zlib.ZLIB_RUNTIME_VERSION,
                lzmaExtension=_lzma.__name__, lzmaExtensionPath=getattr(_lzma, '__file__', None),
                liblzmaVersion=None, liblzmaVersionNote='Python does not expose the linked liblzma version.',
                xzPreset=6, xzMemoryLimitBytes=memory_limit,
                memoryLimitScope='XZ decoder allocation limit, not a whole-process resident-memory bound.', checks=checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    restore = commands.add_parser('restore')
    restore.add_argument('--directory', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    restore.add_argument('--expected', nargs='+', required=True)
    args = parser.parse_args()
    try:
        result = check_codecs() if args.command == 'check' else restore_archives(args.directory, args.destination, args.expected)
    except (ArchiveError, OSError) as error:
        parser.exit(1, json.dumps(dict(result='failed', error=str(error))) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
