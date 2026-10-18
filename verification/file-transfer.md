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

The baseline on macOS ARM64 / JDK 21.0.2 uses a 1,024 x 1,024 deterministic
Byte raster, three measured rounds and three fresh JVM trials per case (nine
processes, 18 raw stdout/OS logs). The probe was committed as `12b9e45` before
measurement; no runtime implementation changed. The selected native library
is the preserved, hash-checked normal Gradle producer from merged PR31, source
fingerprint `953cb19f641f59a808a6838ad532232dd714e3fc9abea34189aeb8327beda2c3`.
The actual original compiled probe and 874 production classes are preserved
with member hashes alongside the selected engine and manifest.

| Existing API case, three rounds | Median elapsed | Cumulative JVM allocation | Whole-process peak RSS | Total bridge bytes |
| --- | ---: | ---: | ---: | ---: |
| Memory file write + read, 1,048,576 bytes | 1.609 s | 805.46 MB | 1,076.45 MB | 22.71 MB |
| Open file bundle + translate, 1,049,490-byte GTiff | 47.98 ms | 207.88 MB | 247.19 MB | 8.46 MB |
| Existing direct translateTo + OS readback, same GTiff | 6.05 ms | 3.21 MB | 144.75 MB | 984 bytes |

Each result validated complete byte equality and a checksum. The memory path
made six JSON calls, with a largest JSON message of 3,825,863 bytes; the bundle
path made nine JSON calls, largest 1,421,227 bytes. The direct path made three
small JSON calls and did not transfer its output through the bridge. Its
filesystem differs from the in-memory path, so the table is bottleneck evidence,
not a claimed filesystem comparison or a before/after improvement. MB is decimal.
RSS includes setup and warmup; sampled heap is recorded separately in the report.
Reports and raw logs are under `verification/results/file-memory-before-macos-arm64.json`
and `verification/results/file-memory/`; `file-before-baseline-verified.json`
checks raw values, OS units, source/classes and preserved baseline identities.

No improvement has been measured yet. A potential binary file path must preserve
isolated input workspaces, file basename and sidecar rules, exact bytes, existing
direct APIs, cancellation cleanup and caller-owned partial direct outputs. It
must reject malformed/overflowing buffers and negotiate compatibility separately
from raster, coordinate and column transfers. Full-buffer public `GdalFiles`
results inherently allocate their final ByteArrays; do not call that zero-copy
or promise a universal/native allocation bound. Any chunked extension must
maintain random access and await consumption rather than queue complete files.
