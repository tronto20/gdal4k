# Vector column transfer verification record

The current `readFeatureBatch` extension repeatedly calls `getNextFeature`; reading
fields, geometries and closing their handles adds separate serialized bridge calls.
The first diagnostic measures this existing path, before implementing columns.

## Existing path diagnostic

`VectorMemoryProbe` loads 8,192 rows with nullable exact Int64 values above 2^53,
Float64, nullable Korean strings and nullable Point WKB. GeoJSON is read directly;
MEM and GPKG use copies of the same source. Every measured value and WKB coordinate
is checked. Each fresh JVM performs three reads, following warmup; the reporter
runs three trials per driver in alternating driver order. Compilation and probes
run sequentially.

The explicit baseline library is the preserved, hash-checked macOS ARM64 library
from PR30 CI run 39157753902, whose source tree is now in main. It is a reference
library under `verification/build/vector-baseline`, not a current producer output.
The report records library/manifest, source, compiled probe/production classes and
raw stdout/OS time hashes. Recovery copies preserve the probe, harness and actual
API/runtime JVM jars. Results and recovery bytes are ignored local evidence.

The initial `feature-baseline` report observes about 233,000 JSON calls, 1.86 GB
cumulative JVM allocation and 3.7–3.8 seconds per three reads. This identifies a
bridge-call/serialization bottleneck. It establishes no improvement claim.
Instrumented elapsed time includes result checking and nonallocating UTF-8 byte
counting. Whole-process OS RSS includes fixture setup and warmup; elapsed time and
cumulative allocation exclude those phases. Sampled heap, cumulative allocation
and OS peak RSS are different measurements.

## Implementation constraints

Use GDAL's Arrow C stream without introducing an Arrow library dependency. GDAL
3.12.4's generic `ReleaseStream` resets layer reading. Opening/releasing a new
stream on each batch would therefore repeat rows. Keep a scoped native stream
alive across batches and release it on close/error/cancellation and parent close.
Detached column values must outlive the native Arrow arrays/stream/dataset.

Guard native layer aliases against concurrent stream/cursor/filter/schema changes,
including operations reached through dataset and SQL result handles. Release
stream/schema/array resources before their owning layer can be destroyed. Preserve
nullable strings, exact signed/unsigned integers, geometry WKB and column order;
do not silently convert unsupported schemas. Arrow validity does not distinguish
an unset feature field from an explicit null: document that distinction and retain
the existing Feature path for callers that need it.

After the column path exists, measure both paths with the same final compiled
probe, fixture, settings and compiled API/runtime classes. If the diagnostic source
changes, keep the initial report and run a separately labeled controlled baseline;
never relabel it as a final before/after result. Verify source and independent
published consumers, representative drivers, errors, cancellation, schema/alias
exclusivity and exact binary wire bounds before CI/Ready/merge.

Pinned upstream references:

- [GDAL 3.12.4 Arrow implementation](https://github.com/OSGeo/gdal/blob/v3.12.4/ogr/ogrsf_frmts/generic/ogrlayerarrow.cpp), especially `GetArrowStream` and `ReleaseStream`.
- [GDAL 3.12.4 Arrow C interface definitions](https://github.com/OSGeo/gdal/blob/v3.12.4/ogr/ogr_recordbatch.h).
- [GDAL Arrow stream tutorial](https://gdal.org/en/stable/tutorials/vector_api_tut.html#reading-from-ogr-using-the-arrow-c-stream-data-interface).
