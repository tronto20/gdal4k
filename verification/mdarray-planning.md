# Multidimensional array count and chunk planning

`MDArray.getTotalElementsCount()` returns the native stored-element count as a
detached `ULong`. A scalar has one element. GDAL 3.12.4 returns zero silently when
the product of dimension sizes overflows UInt64; zero must therefore not always
be interpreted as an empty array. A large count does not promise that one read or
allocation can represent the entire array.

`getProcessingChunkSize(maxChunkMemory)` returns detached per-axis element
counts using native natural blocks and the stored extended datatype's record
size. Its explicit `ULong` argument is a byte budget. Zero requests no expansion;
it does not select a default memory budget. A natural block can exceed the
requested budget, and edge extents need not be multiples of the block size.
This operation does not read samples or allocate a chunk-sized sample buffer.

Numeric, complex, text and compound shapes are supported. For text, the stored
record size includes a native pointer slot rather than the pointed-to string
payload. Compound dynamic fields have the same limitation. These results are
planning hints rather than a peak-memory limit. Native pointer width can affect
chunk choices, especially for dynamic datatypes.

The returned scalar/list owns no handle and remains usable after its source and
ancestors close. A new query still requires a live source. Both calls use
operation-boundary cancellation; neither native C API has options or a progress
callback. They do not provide inner-native preemption.

The bridge uses canonical unsigned decimal strings for the scalar count, each
chunk axis, and the byte budget. It preserves values through `ULong.MAX_VALUE`
without Double or signed-Long conversion. The new detached methods require an
empty allocation ledger. Count/chunk data is never interpreted as a resource ID;
exception cleanup can use only validated fresh IDs explicitly declared in a
ledger. Existing generic block-size decoding remains unchanged.

The budget must fit the engine's native `size_t` before conversion. Current JS
and wasmJs engines are wasm32, so budgets above 4,294,967,295 fail. The current
non-web x64/ARM64 engine ABIs use 64-bit `size_t`, including Windows LLP64, whose
`unsigned long` is still 32-bit. Do not assume identical chunk vectors across
32-bit and 64-bit engines.

Prepared common fixtures cover tiny MEM numeric and nonnumeric arrays, scalars,
composed views, detached values after owner closure, and native `size_t` budget
boundaries. A bounded metadata-only VRT checks UInt64.MAX, 2^53+1 and native
product-overflow zero without reading huge arrays. A small blocked VRT proves
that an indivisible block can exceed the advisory budget. Existing reopened NC4
scientific/combined fixtures additionally query real chunked-driver metadata;
this does not add netCDF to default or web packages.

The common class is copied into independent published JVM/Native consumers.
Wire tests separately qualify canonical spelling, exact unsigned values, empty
ledgers, boundary cancellation and ledger-only cleanup. Fake-transport tests
prove client behavior; actual common/native fixtures are needed to qualify the
native parser, ABI and driver behavior. Source preparation and Kotlin-only
verification do not establish native runtime, package, external publication or
whole-platform acceptance. Preserve actual-host, simulator, iOS-device
compile/link and Android native-bridge/emulation evidence labels.
