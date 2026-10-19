# Multidimensional transfer investigation

The existing `MDArray.read`/`write` methods transfer numeric slices as Float64 in
row-major order. `MDArray.readValues`/`writeValues` preserve the stored schema,
including exact Int64/UInt64, complex components, strings and nested records.
Attributes already expose the same typed values for complete metadata arrays.
These APIs exist and their type, shape, range and handle lifetime rules remain
the contract for this work.

Current Float64 slices serialize every value as a JSON number. Typed values
serialize one JSON object per element and format numeric components as decimal
strings. Native `mdRaster`, `mdValues` and `attributeValues` parse/format these
values and allocate native staging buffers; Kotlin allocates JSON trees and
public result values. This establishes a candidate cost in the code, not a
measured performance improvement.

`MDMemoryProbe` uses the real bundled MEM multidimensional driver with three
existing API cases: a nonzero two-dimensional Float64 slice, an exact Int64 typed
slice containing values outside Float64's exact integer range, and a 1,024-element
UInt64 metadata attribute. It validates every result value/bit and a checksum.
Each case runs a warmup and three measured write/read rounds in a fresh JVM.
Setup/warmup are excluded from elapsed time/allocation and included in process
peak RSS. Bridge counts/bytes, cumulative JVM allocation, sampled heap and OS peak
RSS are separate diagnostics. There are no timing pass thresholds.

Before changing runtime code, commit the versioned harness, preserve a verified
normal Gradle native library/manifest and compiled classes, then run identical
trials sequentially without build/consumer work. Keep historical measurements
immutable. A final comparison must use matching compiled probes, settings,
inputs and public validation; if a new capability changes delegated probe
bytecode, label a separate current-Kotlin/old-engine controlled fallback run.

The intended optimization may negotiate a numeric binary channel while keeping
old-engine compatibility and explicit fallback for unsupported schemas. It must
preserve requested windows/scalars, exact Int64/UInt64, complex components,
nonfinite values, string/compound fallback, validation before writes, cancellation
and parent/child lifetimes. Avoid per-element metadata traffic and speculative
operation retries. Public `List<MDValue>` results still allocate values and
strings; no zero-copy or universal speed/memory claim is justified.

The committed harness `d68b822` was compiled with normal Gradle, then all three
small smoke cases passed. The original baseline uses the preserved normal Gradle
PR32 macOS ARM64 native engine (fingerprint `19e4ee13...`) with unchanged runtime
code, JDK 21.0.2, `-Xms32m -Xmx1536m -XX:+UseG1GC`, three rounds/trials and nine
fresh JVMs. Slices contain 65,536 values in a nonzero two-dimensional window;
the attribute contains 1,024 values. Every returned Float64 bit or exact integer
string and an independently recomputed checksum matched.

| Existing API, three write/read rounds | Median elapsed | Cumulative JVM allocation | Whole-process OS peak RSS | Bridge bytes |
| --- | ---: | ---: | ---: | ---: |
| Float64 numeric slice | 219.19 ms | 123.16 MB | 231.23 MB | 3.34 MB |
| Exact Int64 typed slice | 511.67 ms | 694.41 MB | 746.80 MB | 25.76 MB |
| UInt64 metadata attribute | 15.03 ms | 11.39 MB | 117.44 MB | 406.42 KB |

All cases made six JSON calls and zero binary calls. The typed Int64 case's
largest JSON message was 4,292,785 bytes. This is measured candidate overhead;
no optimization or before/after improvement has been established. MB/KB are
decimal; allocation, sampled heap and OS RSS are distinct. Validation/checksum
work is included in measured elapsed/allocation on both future comparison sides.

The 18 raw stdout/OS logs, original 878 production classes and 14 probe classes,
source/harness hashes, native library/manifest and byte identities are retained
under `verification/results/md-memory/` and `verification/build/md-baseline`.
The selected native library is hardlinked to the frozen retained PR32 producer
in a separate checkout; neither name/inode may be rebuilt or modified. Future
native producers run in the new MD checkout. The complete raw/value/source/class/
OS-unit proof is `verification/results/md-transfer-stage1/before-baseline-verified.json`.
Do not overwrite, relabel or repeat this original baseline absent a concrete
failure or changed harness. Optional scientific drivers require their own actual
profile fixtures; MEM does not establish HDF5/netCDF availability or performance
on other hosts.


## Implementation under verification

Private protocol 5 uses raw little-endian Float64 frames for the existing numeric
slice conversion APIs. Numeric `MDValue` components use ASCII length-prefixed
strings (four-byte little-endian length per component, absent imaginary length
`0xffffffff`). The native engine still calls the original `mdParse`/`mdFormat`,
so exact unsigned/signed ranges, complex presence checks, canonical floating-point
strings and nonfinite spellings follow the existing typed contract. This avoids
per-value JSON objects and repeated keys without claiming raw typed storage for
the public string-valued model. Complete numeric frames/public objects remain.

Numeric writes validate the complete frame and stage/parse every component before
calling the same GDAL write API. Reads inspect the native schema and return either
a numeric frame or the existing string/compound JSON result in that same call;
there is no speculative retry, per-element schema call or cached type to outlive
its handle. Old engines retain the JSON path. Input packing and decoding remain
under the runtime lock with bounded cancellation checkpoints. No direct buffer
lease or total-memory bound is exposed.

The local normal Gradle JVM compilation passed, and ten transport-only control
tests passed (five new MD plus five file regression controls). These controls use
explicit test transports and do not establish native runtime behavior. New public
cases cover Float64 nonzero windows/scalars/conversion, exact integers, complex/
nonfinite values, numeric attributes, string/compound fallback, invalid shape/range,
cancellation and parent closure. Two JVM cases exercise actual malformed native
frames; those and the full source/published consumers await a current engine.

Local disk space is severely constrained. Actual fresh normal Gradle native/Web
CI producers must finish before new runtime tests or measurements can be accepted.
An exact source/fingerprint/hash-verified CI prebuilt engine can be consumed through
the normal producer's prebuilt verification path; an older baseline engine cannot
validate changed native code. No new speed or memory effect is claimed yet.
