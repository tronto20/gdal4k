# Valid-data footprint verification

`Dataset.footprint` and `footprintTo` wrap the pinned GDAL 3.12.4 C utility API.
The [upstream implementation](https://github.com/OSGeo/gdal/blob/v3.12.4/apps/gdal_footprint_lib.cpp)
defines band mask combination, NoData override, pixel/georeferenced output and geometry operations.
The wrapper preserves input ownership, rejects existing output paths and closes every successful
new output handle. GDAL closes its new output on utility failure. In-memory output workspaces
are removed after byte extraction or failure; direct partial files/sidecars stay caller-owned.

Default `-max_points unlimited` retains detailed mask boundaries. The raw CLI defaults to a
100-point simplification limit. Default location attributes are disabled to avoid persisting
signed source URLs/private VSI paths. Output driver/layer/path are controlled by named arguments,
and positional paths or overriding CLI options are rejected. Default engine dependencies and
publication coordinates are unchanged.

`FootprintApiTest` runs through the normal common runtime suite and is copied into the independent
published JVM/Native/Web/Android consumer by `verification/consumer.py`. Cases cover a rotated
ring/hole and CRS, empty/all-valid/disconnected masks, per-band union/intersection, explicit
dataset masks, NoData override without input mutation, pixel coordinates/reprojection, a detailed
comb without implicit simplification, memory independence, GPKG/GeoJSON/Shapefile direct output
and sidecars, existing output protection, VRT dependency leases, progress cancellation/callback
failure, invalid options/formats/paths and closed/vector inputs. A JVM transport test additionally
checks real GDAL progress event sequences for both new operations, rather than wrapper start/end
events alone.

On Linux x64 and Mac ARM64 primary CI, `footprint-memory-report.py` runs three fresh JVMs for
each of memory and direct output, alternating their order. Each JVM warms up once and measures
three complete footprint/reopen/check/close operations, with direct file deletion included.
The 512-square Byte MEM fixture contains a centered hole; valid pixels and area are independently
checked as `side² * 3/4` and `validPixels * abs(2*(-3)-0.5*0.25)`. Each result must retain two rings.
The report binds current source files, actual compiled probe/production bytes and the normal
Gradle-produced native library to its manifest/fingerprint. CI retains raw stdout, OS `/usr/bin/time`
logs, report and compiled probe bytes.

Elapsed time, cumulative JVM allocation, sampled JVM heap and whole-process OS RSS are distinct.
Warmup/setup are excluded from elapsed/allocation but included in RSS. Heap is sampled every
2ms and can miss peaks. This is a functional addition with representative diagnostics, not a
before/after optimization benchmark. No improvement, total-memory bound, zero-copy, universal
speed or Native/Web/Android performance claim follows. Complete result buffers and GDAL's
intermediate vector geometries remain. Full exact-HEAD CI, actual new-case XML/source and published
consumer execution, profile fixtures/IPC/dependency/size gates, all packaged payload identities,
fresh Ready/merge gates and tested-HEAD ancestry are required before acceptance.
