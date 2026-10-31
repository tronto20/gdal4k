"""Bounded ZIP STORE/raw-DEFLATE member reader with exact compressed EOF.

The caller supplies central-directory values already joined to local headers.
Unlike ZipExtFile, this reader proves that every declared compressed byte is
part of exactly one completed stream. It neither closes nor owns its source.
"""
import zlib

CHUNK = 64 * 1024
MAX_READ = 1024 * 1024


class ZipStreamError(ValueError):
    pass


def need(value, message):
    if not value:
        raise ZipStreamError(message)


class RawZipReader:
    def __init__(self, source, payload_start, compressed_bytes, decoded_bytes,
                 crc32, method):
        need(all(type(value) is int and value >= 0 for value in
                 (payload_start, compressed_bytes, decoded_bytes, crc32)),
             'ZIP member offsets/counts/CRC must be nonnegative integers')
        need(crc32 <= 0xffffffff, 'ZIP CRC is outside UInt32')
        need(type(method) is int and method in (0, 8), 'Unsupported ZIP member codec')
        if method == 0:
            need(compressed_bytes == decoded_bytes, 'STORE compressed and decoded byte lengths differ')
        self.source = source
        self.payload_start = payload_start
        self.compressed_bytes = compressed_bytes
        self.decoded_bytes = decoded_bytes
        self.expected_crc32 = crc32
        self.method = method
        self.compressed_seen = 0
        self.decoded_generated = 0
        self.decoded_returned = 0
        self.actual_crc32 = 0
        self.pending = b''
        self.decoder = zlib.decompressobj(-15) if method == 8 else None
        self.done = False
        self.closed = False

    def __enter__(self):
        need(not self.closed, 'ZIP member reader is closed')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                need(self.decoded_returned == self.decoded_bytes,
                     'ZIP member reader was not fully consumed')
                need(not self.read(1), 'ZIP member has additional decoded bytes')
                need(self.done, 'ZIP member compressed EOF was not validated')
        finally:
            self.close()
        return False

    def close(self):
        self.closed = True
        self.pending = b''
        self.decoder = None

    def readable(self):
        return not self.closed

    def _fetch(self):
        count = min(CHUNK, self.compressed_bytes - self.compressed_seen)
        need(count > 0, 'No declared compressed bytes remain')
        expected_offset = self.payload_start + self.compressed_seen
        need(self.source.seek(expected_offset) == expected_offset,
             'ZIP compressed source seek differs from requested offset')
        data = self.source.read(count)
        need(isinstance(data, bytes) and 0 < len(data) <= count,
             'ZIP member compressed payload is truncated or over-read')
        self.compressed_seen += len(data)
        return data

    def _account(self, data):
        self.decoded_generated += len(data)
        need(self.decoded_generated <= self.decoded_bytes,
             'ZIP decoded bytes exceed declared member length')
        self.actual_crc32 = zlib.crc32(data, self.actual_crc32)

    def _finish(self):
        need(self.compressed_seen == self.compressed_bytes,
             'ZIP compressed stream ended before declared compressed bytes')
        need(not self.pending, 'ZIP compressed stream has an unconsumed tail')
        if self.decoder is not None:
            need(self.decoder.eof, 'ZIP DEFLATE final-block marker is missing')
            need(not self.decoder.unused_data and not self.decoder.unconsumed_tail,
                 'ZIP DEFLATE has trailing or unconsumed compressed bytes')
        need(self.decoded_generated == self.decoded_bytes,
             'ZIP decoded member length differs from declaration')
        need(self.actual_crc32 == self.expected_crc32, 'ZIP decoded member CRC32 mismatch')
        self.done = True

    def read(self, count=-1):
        need(not self.closed, 'ZIP member reader is closed')
        need(type(count) is int and 0 <= count <= MAX_READ,
             'ZIP read must have an explicit bounded byte count')
        if count == 0 or self.done:
            return b''
        output = bytearray()
        if self.method == 0:
            while len(output) < count and self.compressed_seen < self.compressed_bytes:
                # _fetch uses64KiB chunks. Limit this fetch to the caller's
                # remaining requested bytes so no retained payload buffer grows.
                wanted = min(CHUNK, count - len(output), self.compressed_bytes - self.compressed_seen)
                expected_offset = self.payload_start + self.compressed_seen
                need(self.source.seek(expected_offset) == expected_offset,
                     'ZIP compressed source seek differs from requested offset')
                data = self.source.read(wanted)
                need(isinstance(data, bytes) and 0 < len(data) <= wanted,
                     'ZIP STORE payload is truncated or over-read')
                self.compressed_seen += len(data)
                self._account(data)
                output.extend(data)
            if self.compressed_seen == self.compressed_bytes:
                self._finish()
        else:
            try:
                while len(output) < count and not self.done:
                    if self.pending:
                        incoming, self.pending = self.pending, b''
                    elif self.compressed_seen < self.compressed_bytes:
                        incoming = self._fetch()
                    else:
                        incoming = b''
                    # One excess decoded byte is sufficient to reject an
                    # inflated length. No unbounded flush/all-output call.
                    limit = min(CHUNK, count - len(output),
                                max(1, self.decoded_bytes - self.decoded_generated + 1))
                    data = self.decoder.decompress(incoming, limit)
                    self.pending = self.decoder.unconsumed_tail
                    self._account(data)
                    output.extend(data)
                    need(not self.decoder.unused_data, 'ZIP DEFLATE has trailing compressed bytes')
                    if self.decoder.eof:
                        self._finish()
                    elif not incoming and not data:
                        raise ZipStreamError('ZIP DEFLATE is truncated or missing its final-block marker')
            except zlib.error as error:
                raise ZipStreamError('Invalid raw ZIP DEFLATE stream: ' + str(error)) from error
        self.decoded_returned += len(output)
        return bytes(output)
