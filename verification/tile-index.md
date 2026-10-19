# Explicit raster tile index

The implementation wraps the pinned [GDALTileIndex C API](https://github.com/OSGeo/gdal/blob/v3.12.4/apps/gdal_utils.h)
and [implementation/options](https://github.com/OSGeo/gdal/blob/v3.12.4/apps/gdaltindex_lib.cpp).
The inspected implementation Git blob is `f843fcaa13ce4ed575df5949e8894f9c6629a22f`.
It accepts source names, opens them internally, and transfers the successful output handle;
its owned output dataset is closed on failure. A wrapper RAII handle covers success and validation failures.

The upstream iterator can enumerate even a nonrecursive directory. Rejecting `-recursive`
alone is insufficient: every source is checked as an existing nondirectory file before calling
GDALTileIndex. Only explicit local file paths and `/vsimem/` are supported. Network/VSI
providers and connection strings are rejected rather than bypassing the existing openDataset
HTTP policies and mount lifetimes. Explicit VRT dependency access follows GDAL's existing VRT
semantics; files and dependencies must stay available through completion. The wrapper does
not inspect/rewrite embedded VRT dependency names or copy pixels into the index.

The legacy upstream call may warn and skip bad inputs or write mixed-CRS polygons without
a target CRS. Preflight opens/closes every source as a raster, checks nonzero dimensions,
finite nonsingular geotransforms and compatible CRS. A target CRS explicitly permits mixed
CRS and requires a CRS on every source. The final layer must contain exactly one feature per
input, in input order, with each exact expected location; truncation/laundering of the location
field is a failure. This validation reopens the completed output after closing its writer,
so it works with creation-only layers such as GeoJSON. There is no skip/filter mode, broad
traversal or overwrite/append mode.
Existing main files and recognized Shapefile sidecars / GPKG journals are rejected before
creation, including orphan sidecars when the main file is absent. Other driver-specific
sidecar names still follow the selected driver's creation rules.
Input preflight and GDAL's reopen are separate reads: callers must not mutate the files during
the call. This does not provide an atomic filesystem snapshot.

Stored relative locations are preserved, interpreted relative to process cwd; they are not
rewritten relative to the output. `absolutePaths` uses GDAL's local cwd expansion. It does
not change already absolute `/vsimem/` paths or promise portability across machines.
The API deliberately stores caller-supplied paths: absolute paths may reveal private filesystem
layout. No source-derived metadata field is silently added. Source CRS metadata is opt-in.
All sources may omit CRS when no target CRS is requested; mixing known and unknown is rejected.

Web inputs must already exist in the same runtime's `/vsimem/`. No Node path, browser File,
OPFS or HTTP URL is implicitly mounted. Explicit writeMemoryFile copies and their sidecars
remain full buffers and caller-owned. Output vector geometry/attributes are independent of
input file lifetime, but stored references do not keep the files or VRT/URI leases alive.
Direct output retains the existing Node/OPFS provider and partial-output ownership contract.
This is a vector extent/location catalog, with no GTI creation or tested GTI availability claim.

Seven common tests cover rotated corner polygons/area/order and NoData independence, named
location/source-CRS fields and independent result lifetime, strict mixed CRS and warped-grid
reprojection, memory Shapefile sidecars plus GPKG/GeoJSON/Shapefile direct output/overwrite
refusal, VRT dependencies/unknown CRS/absolute vsimem paths, cancellation/callback errors and
subsequent reuse, and invalid input/provider/directory/options/driver handling. The same cases
are copied to independent published consumers. JVM cases add real cwd-relative/absolute paths
and prove actual GDAL progress sequences for both result forms.

`TileIndexMemoryProbe` and `tile-index-memory-report.py` provide functional diagnostics on
32 explicit 64x64 Byte GTiff files, rotated affine transform `(2, .5, .25, -3)`, EPSG3857,
GPKG output. All files close before the measured operation. Each round checks feature order,
exact location, area `tiles * 64 * 64 * 6.125` and independent ordinal checksum
`tiles * (tiles+1) / 2 * 64 * 64`. Three rounds per fresh JVM, three trials per memory/direct
path alternate in order; warmup and fixture setup are outside elapsed/allocation but included
in whole-process OS peak RSS. Measured work includes preflight, GDAL indexing, output reopening,
geometry/attribute checks, closing and direct deletion. The report retains raw stdout/time
logs, settings, source inventory, all production/probe class hashes, current manifest and JNI
hashes. CI uploads the raw probe class files as well. Cumulative allocation, sampled heap at
2ms and OS RSS are distinct; sampled peaks can miss short spikes.

These are functional diagnostics, not a before/after improvement comparison. Complete
GdalFiles buffers, source metadata and GDAL intermediate geometries remain. There is no
streaming, zero-copy, total-memory bound, pixel mosaic or universal/Native/Web/Android
performance claim. Local template compilation is separate from actual pinned producer/runtime
verification. Current actual runtime, diagnostics and complete exact-HEAD CI remain pending.
