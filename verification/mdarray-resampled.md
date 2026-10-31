# Multidimensional resampled views

`MDArray.getResampled(newDimensions, resampling, targetSRS, options)` binds
`GDALMDArrayGetResampled` from GDAL 3.12.4. It returns one independently closed,
readonly view that depends on its source and ancestors. Numeric types, including
complex types, retain their stored datatype; interpolation and warp conversions
can still change or round values. Unsupported writes return false.

The dimensions list must match source rank, which must be at least two. Normally
the last two axes are Y/X. Null spatial entries request native size inference;
one explicit size can determine the other. Explicit nonspatial dimensions must
match the source name and size, and native retains the source dimension objects.
Output spatial dimensions are rebuilt as `dimY`/`dimX`. Indexed target spatial
axes must both have regular numeric one-dimensional indexing arrays of matching
length; unindexed target dimensions can specify sizes alone. Source coordinates
must permit native georeferencing. Native output sizes are limited to `INT_MAX`.

Nearest, Bilinear, Cubic, CubicSpline, Lanczos, Average, Mode and Rms have native
mappings. Gauss and unknown raw algorithm codes fail. CubicSpline is not an
identity operation merely because the grid is unchanged. Options are native
GetResampled options: generic resampling ignores them, while compatible NASA
EMIT inputs can use `EMIT_ORTHORECTIFICATION` and `USE_GOOD_WAVELENGTHS`.
These are not arbitrary gdalwarp command arguments.

The input wrappers can close after construction. Native clones an explicit
target SRS, but shares the source SRS when no target is supplied; source metadata
mutation behavior is not an immutable-snapshot guarantee. An output
`getSpatialRef()` copy is independently owned. Native construction supplies no
inner warp progress callback, and geolocation preparation can materialize
temporary TIFFs. Cancellation and sampled progress apply at operation boundaries.
They do not establish preemption, whole-input laziness or a process memory bound.

The bridge snapshots nullable dimensions, algorithm, SRS and option values before
queued work, checks runtime/open handles, and validates native handle kinds,
canonical IDs, rank, options and algorithm shape. The result must be exactly one
fresh canonical UInt64 handle with the matching allocation ledger. Cancellation,
observer/native errors and malformed replies release only identifiable fresh
allocations and preserve the primary failure. Pure-wire tests qualify this
protocol, not native resampling.

The exact GDAL 3.12.4 source needs a native composition correction:

- `gcore/gdalmultidim.cpp:8551` increments extra-axis offsets by one, ignoring
  supplied nonunit/signed `arrayStep`. `Read` passes those validated steps through
  unchanged. The separate `resampled-extra-axis-steps.patch` replaces this
  increment with the supplied validated step. Unsigned arithmetic handles signed
  steps without narrowing offsets or negating `INT64_MIN`. GDAL port-version 7
  requests fresh SDKs. Real rank-three/rank-four stepped, reversed, transposed and
  chained fixtures are prepared; this source-only change has not executed them.

The stock resampled raster adapter also remaps only positive CRS spatial-axis
IDs (`7793–7799`), forwarding negative IDs unchanged. A rank-three source X-axis
mapping of `-3` therefore fails to become adapter X-axis `-1`. The separate
`resampled-signed-srs-axis-mapping.patch` preserves the sign when remapping X/Y
IDs, using widened comparisons without taking an absolute value or negating an
input mapping. A real signed rank-three fixture compares every resampled value,
shape and output coordinate axis against an independent classic raster warp
whose CRS already has the intended two-dimensional signed mapping. A separate
null-target case checks that the public output retains its source signed mapping
and that its detached CRS copy survives source closure. These fixtures are
compiled source only; native signed-mapping correctness remains unaccepted.

The raster adapter overrides only Double NoData (`7858–7866`). Exact Int64 and
UInt64 sentinel-neighbor classification through the warp remains unqualified.

Prepared source fixtures cover tiny real MEM automatic/explicit/regular-indexed
grids, nearest and average values, numeric/complex types, consecutive extra-axis
reads, rank-three/rank-four signed/strided compositions, source-before-resampling slicing,
failure cases, readonly writes, wrapper lifetime, and local
EPSG:4326 to EPSG:3857 reprojection compared with classic warp. Scientific and
combined profile fixtures create and reopen an owned tiny NC4 source twice and
construct fresh resampled views. The actual common fixture is copied into the
published consumer package by `consumer.py`.

This cohort is source-only. Kotlin compilation and fake-transport execution do
not qualify native fixtures, NC4 persistence, ABI, publication or any platform.
Geolocation and EMIT cases need their own actual fixtures. The overall GDAL
3.12.4 full-function goal remains incomplete.
