# File transfer investigation

The existing `translateTo`, `warpTo`, vector and analysis `*To` APIs already write
directly to OS/Node/OPFS paths without returning complete output buffers. They
remain the large-output path; this work must not duplicate them or claim they
currently stage complete outputs. Random access, sidecars and caller ownership
of partial direct outputs remain required.

The current in-memory APIs have two separate candidate costs:

- `writeMemoryFile`/`readMemoryFile` encode every byte as a JSON number. This
  creates JSON arrays/strings, parsed native objects and additional byte buffers.
- `openDataset(GdalFiles)` and file-returning utilities encode complete flat file
  bundles as Base64 JSON. Input decoding copies Base64 into a native buffer and
  then into an isolated `/vsimem` workspace. Output encoding copies back through
  JSON and the Kotlin Base64 decoder. Sidecar names and workspace lifetimes are
  part of the existing contract.

`FileMemoryProbe` and `file-memory-report.py` measure both existing paths plus
the existing direct `translateTo` control under fresh JVMs, identical deterministic
fixtures and three rounds/trials. The direct control includes caller OS readback;
it is not a claim that OS and `/vsimem` filesystems have equal costs. Raw bridge
counts/bytes/largest JSON, complete byte validation, wall time, cumulative JVM
allocation, sampled heap and OS peak RSS are recorded separately. Setup/warmup
are outside elapsed/allocation but inside process peak RSS. Engine, source,
compiled production/probe classes and all raw logs are bound by hashes.

No improvement has been measured yet. Establish the baseline before selecting
or changing the implementation. A potential binary file path must preserve
isolated input workspaces, file basename and sidecar rules, exact bytes, existing
direct APIs, cancellation cleanup and caller-owned partial direct outputs. It
must reject malformed/overflowing buffers and negotiate compatibility separately
from raster, coordinate and column transfers. Full-buffer public `GdalFiles`
results inherently allocate their final ByteArrays; do not call that zero-copy
or promise a universal/native allocation bound. Any chunked extension must
maintain random access and await consumption rather than queue complete files.
