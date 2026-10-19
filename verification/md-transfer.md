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

No baseline or effect has been measured for this workstream yet. Optional
scientific drivers require their own actual profile fixtures; this MEM probe
does not establish HDF5/netCDF availability or performance on other hosts.
