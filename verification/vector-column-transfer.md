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


## Implemented column path and controlled local result

`Layer.openColumnStream` owns a persistent GDAL Arrow C stream; `read` returns
caller-owned typed columns with validity, exact Int64, UTF-8, binary and WKB plus
known CRS WKT. Stream/array/schema release and layer/dataset alias guards cover
EOF, failure, cancellation and parent close. Unsupported nested, dictionary,
date and UInt64 schemas fail explicitly; use existing Feature iteration when
its semantics are needed. No Arrow library is added to the default platform
profile. Web GeoPackage uses GDAL's generic Arrow producer because its optimized
producer can start a worker thread for non-contiguous FIDs; the Web engine is
threadless. The per-call thread-local override is restored on all exits.

The final controlled macOS ARM64 JVM experiment keeps the same compiled probe,
API/runtime classes, fixtures, JVM 21.0.2 flags and checksums. Nine fresh baseline
JVMs use the actual preserved PR30 native library with the current Kotlin Feature
path. Eighteen fresh current JVMs measure both Feature and column paths. Each
entry below is a median of three fresh JVM trials, each reading 8,192 rows three
times. Column batches contain at most 256 rows.

| Driver | Baseline Feature time | Current Feature time | Current columns time | Baseline allocation | Columns allocation | Baseline / columns OS peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GeoJSON | 3.749 s | 3.793 s | 78.2 ms | 1,875 MB | 13.06 MB | 228.0 / 152.5 MB |
| MEM | 3.768 s | 3.780 s | 40.8 ms | 1,875 MB | 13.05 MB | 233.3 / 148.9 MB |
| GPKG | 3.792 s | 3.801 s | 39.3 ms | 1,879 MB | 13.05 MB | 244.1 / 158.5 MB |

MB means decimal bytes. This instrumented workload reduces about 232,866 JSON
calls to six JSON calls plus 99 binary calls, and cumulative JVM allocation by
about 99.3%. It does not establish Native/Web/Android, cold I/O or universal
speedups. Whole-process RSS includes fixture setup and warmup; timed reads and
cumulative allocation exclude them. The 2 ms heap sampler can miss peaks.

The original 27 measurements and 54 raw stdout/OS time logs remain immutable.
After the Web-only conditional fix, a fresh normal Gradle Native producer emitted
an identical JVM library, and all 874 actually selected production class bytes
matched the measured classpath. Published JVM API/runtime classes, classifier
payload, Native embedded archive and both Web KLIB asset sets were checked against
their actual current producers; this identity proof does not relabel old reports.

Local verification passed 1,033 source/API tests and 889 independent published
consumer tests (JVM, macOS Native, JS/wasm Node and Chromium, plus missing-JVM-
payload failure). Every real consumer ran all 11 new public cases. The Native
consumer hid and restored all three producer paths. Full Web binary ownership,
truncation rejection, bounded HTTP/retry/identity and related-input regressions
passed from published assets. Android compiled locally; actual Android JNI and
other host runtime verification remain required in exact-HEAD CI before Ready.

Ignored local evidence: `verification/results/vector-columns-*-verified.json`,
`vector-columns-final-source/proof.json`,
`vector-columns-web-fixed-source/proof.json`, controlled `vector-memory` reports,
and `verification/build/vector-baseline/final-controlled-recovery`. CI archives
new Linux/macOS current-path diagnostics, compiled probes and raw OS logs.
