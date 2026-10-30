# Multidimensional transpose verification

`MDArray.transpose(IntArray)` creates an owned C wrapper for a lazy aliased array.
The Kotlin and native handle registries bind its lifetime to its source array.
The input map is copied before queued work; each original axis appears once and
repeated `-1` entries insert unit axes. Scalar arrays accept an empty map.

The GDAL 3.12.4 overlay patch changes only the transpose view's CRS remapping.
It preserves the sign of valid data-axis mappings, leaves zero or out-of-rank
entries unmapped, and widens before negating `INT_MIN`. Its port version is 4;
the upstream version and download digest are unchanged. Every platform needs a
fresh engine built with that patch. An older JNI library cannot verify it.

`MDArrayTransposeApiTest` supplies four common source and independent-consumer
fixtures:

- Exact Int64/UInt64 values, including values above 2^53, reorder and write
  through the view. Inserted axes and scalar views retain their shapes.
- Complex values and compound Int64/nullable JsonText records retain both data
  and schema. NoData, unit and scale/offset reads follow the source; metadata
  changes are made on the source array.
- A 2×3 VRT with mapping `[-2, 1]` and blocks `[1, 2]` becomes mapping `[-1, 2]`
  and blocks `[2, 1]`. Inserting axes with `[-1, 1, 0, -1]` yields mapping
  `[-2, 3]` and blocks `[0, 2, 1, 0]`. Zero, `INT_MIN` and out-of-rank stored
  mappings exercise the patched bounds without altering the source CRS.
- Invalid maps preserve other handles. Closing a view leaves its source and
  siblings usable; closing the source invalidates descendants. A returned CRS
  clone remains usable after its array and dataset close.

`MDArrayTransposeWireTest` uses a scripted engine to check signed input snapshots,
parent ownership, queued and completion-boundary cancellation, observer failure,
malformed replies and cleanup of known allocated handles. It does not exercise
GDAL or establish native execution. Missing or corrupt allocation metadata cannot
identify an unknown child to close safely.

The scientific/combined profile entrypoint also reopens a chunked, compressed NC4
Int16 array. Transposing the raw 2×3 values `[-9999, 0, 2, 4, 6, 8]` produces
`[-9999, 4, 0, 6, 2, 8]` with shape 3×2 and reordered blocks. NoData and typed
Float32 scale/Float64 offset remain metadata; no unscaled arithmetic is applied.
The current rendered fixture must be hash-bound to passing source and published
JVM/Native entrypoints. Individual assertions are not separate test cases.

Run the local JVM fixtures with a freshly built engine:

```shell
./gradlew :gdal4k-runtime:jvmTest --tests dev.gdal4k.MDArrayTransposeApiTest --tests dev.gdal4k.MDArrayTransposeWireTest
```

Retain current source, engine/patch hashes, command logs and JUnit results as
functional diagnostics. Fresh full CI, package/classifier inspection and actual
independent consumer execution remain required for the committed head. Keep
cross-compilation, actual ARM64 execution, iOS device compile/link, simulator
execution and Android native-bridge emulation distinct. These fixtures establish
no performance improvement or full GDAL coverage. General slice, unscaled and
mask views remain separate workstreams.
