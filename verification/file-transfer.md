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

The implementation now negotiates private binary protocol 4 independently from
raster (1–4), coordinates (2–4) and columns (3–4). Existing public memory/bundle
APIs keep their signatures and old file engines retain numeric JSON/Base64 paths.
File names and sizes stay in small JSON metadata; exact bytes are concatenated in
metadata order. Native input writes directly from the received frame into the
owned isolated workspace. Queued callers defer packing until the runtime lock;
copying and decoding yield/check cancellation in 256 KiB pieces. This is bounded
copy work per checkpoint, not a chunk-streaming API or a bound on total memory.

Final memory reads return their detached binary reply directly; bundles allocate
their final per-file buffers after validating the full metadata/length plan.
Individual output files retain the existing Int-sized bridge limit; an aggregate
larger than one Int-sized binary frame keeps the legacy encoding, selected from
the completed output without rerunning processing. Input bundles exceeding that
aggregate limit also keep the legacy path. Native workspace/file RAII and newly
allocated-handle rollback cover native failures, cancellation and invalid reply
decoding. Explicit memory-file writes and direct outputs retain caller-owned
partial contents on failure. Existing direct `*To` operations still bypass output
byte transfer and keep their random-access filesystem behavior.

All 1,107 local source/API tests passed: JVM 190, macOS Native 181, four JS/wasmJs
Node/Chromium contexts with 183 each, and API 4. Every source context ran seven
new public cases and five internal controls; JVM also ran two actual native
malformed-frame/method-gating cases. Current normal fresh Gradle Native/Web
producers verified all 24 + 34 artifact hashes and real owned install prefixes.
The direct-output test respects Web's existing read-only external input policy
and verifies positional window reads after the source closes. All 37 offline
tests, actionlint and generator idempotence passed. Independent consumers,
current published package identities, Android compilation and full CI remain
unverified at this checkpoint.

## Final controlled JVM comparison

The original baseline above remains immutable. Adding file capability delegation
and recompiling changed the probe's generated bytecode/inlined source maps even
with unchanged probe source. The final comparison therefore uses identical
final compiled Kotlin production/probe classes for both sides: current Kotlin's
numeric JSON/Base64 fallback with the preserved PR31 native engine, versus the
new native binary engine. This is not unmodified old Kotlin. An interim run also
caught an allocation regression from using the JSON `int` accessor in the legacy
per-byte decoder; actual dependency bytecode showed a new numeric lexer per byte.
The original `content.toInt()` conversion was restored before this final pair.
Interim reports are retained separately and do not supply the final claims.

| Case, three rounds | Controlled before elapsed / allocation | Binary after elapsed / allocation | Before / after whole-process peak RSS |
| --- | ---: | ---: | ---: |
| Memory write + read | 1.676 s / 805.57 MB | 5.37 ms / 6.39 MB | 1,122.04 / 113.93 MB |
| Open file bundle + translate | 48.67 ms / 207.88 MB | 8.18 ms / 9.60 MB | 244.35 / 125.94 MB |
| Existing direct translateTo + OS readback control | 5.96 ms / 3.21 MB | 5.76 ms / 3.20 MB | 145.39 / 112.66 MB |

Memory transfers changed from six JSON calls / 22.71 MB total bridge bytes to
six binary calls / 6.29 MB. Bundle transfers changed from nine JSON calls / 8.46 MB
to three JSON closes plus six binary calls / 6.30 MB. Largest JSON messages fell
from 3,825,863 to 176 bytes and from 1,421,227 to 254 bytes, respectively.
Every complete output byte and checksum matched. The direct control still has
three small JSON calls and no output-byte bridge transfer. Its elapsed/allocation
change is not claimed as an improvement; lower process RSS includes optimized
fixture setup outside its measured direct operation.

These are medians from 18 fresh JVMs / 36 raw logs, 1,024 x 1,024 Byte/GTiff
fixtures, JDK 21.0.2, `-Xms32m -Xmx1536m -XX:+UseG1GC`, three rounds/trials.
Native source fingerprints are `953cb19...` (preserved baseline) and `19e4ee13...`
(current). Source/harness, all 878 production classes, 16 probe classes, settings,
raw values/OS units, actual library/manifest hashes and complete-byte checksums
were verified; original compiled class members are retained. Final reports are
`verification/results/file-memory-{controlled-before-final,after-final}-macos-arm64.json`;
the complete proof is `verification/results/file-transfer-stage1/final-controlled-measurements-verified.json`.
No Native/Web/Android or universal speedup is established. These in-memory APIs
still hold complete buffers; copy checkpoints do not imply bounded total memory
or zero-copy. No direct-output speed improvement is claimed.

A binary file path must preserve
isolated input workspaces, file basename and sidecar rules, exact bytes, existing
direct APIs, cancellation cleanup and caller-owned partial direct outputs. It
must reject malformed/overflowing buffers and negotiate compatibility separately
from raster, coordinate and column transfers. Full-buffer public `GdalFiles`
results inherently allocate their final ByteArrays; do not call that zero-copy
or promise a universal/native allocation bound. Any chunked extension must
maintain random access and await consumption rather than queue complete files.
