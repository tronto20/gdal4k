# Multidimensional coordinate variables

`MDArray.getCoordinateVariables()` returns the driver's current auxiliary coordinate arrays.
This is distinct from `Dimension.getIndexingVariable()`: a scanline/pixel data array can refer
to two-dimensional longitude and latitude arrays, or to other arrays with different shapes
and types. The getter preserves native order and repeated references. It does not infer axes,
sort names, deduplicate arrays, read samples, convert values or generate coordinate grids.

The list container is detached. Each array is a fresh closeable wrapper for its actual native
array, with the queried array as its parent. Closing one result leaves the source and other
results usable; closing the queried array or an ancestor invalidates all its results and their
descendants. Values remain aliased, including writes when the actual driver permits them.
Use `readValues` and `writeValues` to retain exact stored integers, complex values and strings.
Later queries see current association metadata while earlier results remain live array handles.

Empty results are valid when the driver has no coordinates. MEM and Zarr interpret a scalar
String `coordinates` attribute as space-separated names; netCDF also accepts commas and has
a special NASA EMIT layout convention. Missing names can emit warnings and be omitted while
valid entries remain in order. Fatal native errors throw. Kotlin delegates parsing and lookup
to GDAL and does not require coordinate arrays to match source dimensions or numeric types.
A native view without a coordinate-variable override may return an empty list; identity views
can share the original array. No source forwarding or synthetic coordinate transformation is
added by this API.

The native adapter checks count representation, element handles and runtime ID headroom before
registration. A batch owns the complete GDAL handle array until its result JSON is constructed.
Failure detaches only this batch's registry entries and releases its handles; success transfers
the individual handles and frees only the outer pointer container. These checks happen after
GDAL's own lookup and allocation.

Runtime replies include the existing `allocatedHandles` metadata on ordinary calls as well as
controlled calls. The field keeps its array-of-decimal-strings shape; existing result fields
and method semantics remain unchanged. No handle scan is needed when the allocation counter
did not change. The Kotlin engine observes canonical IDs under its operation lock before
generated wrappers are constructed. A monotonic UInt64 watermark uses constant retained state
and protects earlier results, parents, siblings and closed IDs. The new list operation requires
every result ID to be fresh and to match the allocation ledger one to one. Its error and
cancellation cleanup releases identifiable fresh IDs from the ledger and the exact array-shaped
result, once each. It never guesses recursively through malformed JSON or closes earlier IDs.
A transport failure that omits every identifier cannot identify otherwise unknown allocations.
This does not claim native progress callbacks or atomicity for arbitrary transport failures.

The source fixture `MDArrayCoordinateVariablesApiTest` covers real MEM associations, exact
heterogeneous values, duplicate/self aliases, unresolved names, live metadata and lifetime
cascades. It is copied into the independent published consumer. The scientific and combined
profile fixture creates a tiny NC4 file at a real OS path, closes its writer, then reopens it
and checks ordered coordinate arrays and exact typed values. The wire fixture covers complete
list validation, unsigned IDs, queued and completion cancellation, cleanup failures, and an
ordinary result whose wrapper has not yet been constructed.

This cohort is prepared on the unaccepted C17 source base and requires rebase after that
cohort's integration. Source preparation does not establish native execution, package identity
or platform acceptance. New exact-head source and independent consumer CI evidence is required
for all four JVM classifiers and the existing Native, Android and web routes. Physical iOS
execution and physical Android ARM64 execution are separate from the current simulator and
emulator native-bridge routes. Optional NC4 profiles retain their actual supported host scope;
Zarr execution is not inferred from MEM or NC4 fixtures.
