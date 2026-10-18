# Optional driver profiles

The default manifest, 32 publication coordinates, four JVM classifiers and target matrix are unchanged. Optional dependencies are produced in an independent source snapshot, vcpkg installation, CMake build and local Maven repository under `build/driver-profiles/`. They never write the default engine prefix or replace a default Maven version.

| Profile | Dependencies and capability | Initial verification matrix |
| --- | --- | --- |
| scientific | HDF5 dataset reads; netCDF create/write/read | Linux x64/glibc, macOS ARM64 |
| parquet | Apache Arrow C++ Parquet; GeoParquet create/read with geometry and attributes | Linux x64/glibc, macOS ARM64 |
| postgresql | libpq; PostgreSQL/PostGIS vector connection and create/write/read/delete | Linux x64/glibc |

These are candidate targets until their exact-head profile CI passes. Windows, Linux ARM64, Apple mobile, Android and ordinary single-thread Wasm are not claimed for these profiles. The producer rejects unverified combinations before installation. Arrow is restricted by its vcpkg architecture support, PostgreSQL requires native libpq/networking, and mobile/Wasm dependency closure and size need separate validation. Default drivers remain available on the existing targets.

```shell
python3 gdal4k-engine/tools/driver-profile.py scientific macos-arm64
python3 gdal4k-engine/tools/driver-profile.py parquet linux-amd64
```

Run on the matching host with Java 21 and the regular producer prerequisites. Scientific test fixtures additionally require `h5py==3.13.0` and `numpy==2.2.6`. The PostgreSQL profile's integration fixture requires a **disposable local** PostGIS service at port5432 with database/user `gdal4k` and password `profile-fixture`; the provided CI owns that service. It never connects to an application database. The fixture creates uniquely named test tables and deletes only those tables.

The command tests the source JVM and Native APIs and independent published JVM/Native consumers, including actual driver data operations. Native consumers hide all three producer archive/cinterop paths. A dependency audit rejects unbundled libraries. `report.json` records actual default/profile native archive and JNI bytes, deterministic gzip sizes, hashes, test counts and the selected source inventory. To obtain size comparisons, build the unchanged default engine for the same target first (CI does so explicitly).

CI uploads a local Maven repository archive, licenses and evidence. Extract its `maven/` folder, add it as a Maven repository, and select the profile version consistently for API, runtime and engine. For example a `0.1.0-SNAPSHOT` checkout yields `0.1.0-scientific-SNAPSHOT`:

```kotlin
repositories { maven { url = uri("/absolute/path/to/maven") }; mavenCentral() }
dependencies {
    implementation("dev.tronto.gdal4k:gdal4k-runtime:0.1.0-scientific-SNAPSHOT")
    runtimeOnly("dev.tronto.gdal4k:gdal4k-engine:0.1.0-scientific-SNAPSHOT:macos-arm64")
}
```

Only the target JVM classifier and Native variants tested in that archive are present. Do not combine different profile/default versions in one process or expect unsupported variants to resolve. Profiles are separately selected distributions, not dynamically loaded driver plugins. The snapshot refuses remote publish tasks; no Central publication is implied by CI artifact creation.

The producer copies Git-tracked source files only and rejects source symlinks. Commit or stage newly added producer files before preparing a snapshot. It verifies copied source hashes before reusing a prepared snapshot. Failed or changed snapshots are never silently overwritten. Remove only the owned generated directory when intentionally rebuilding it. Shared vcpkg download/binary caches may be explicitly configured, but installations remain isolated.

Upstream driver requirements: [HDF5](https://gdal.org/en/stable/drivers/raster/hdf5.html), [netCDF](https://gdal.org/en/stable/drivers/raster/netcdf.html), [Parquet](https://gdal.org/en/stable/drivers/vector/parquet.html), [PostgreSQL](https://gdal.org/en/stable/drivers/vector/pg.html).

The Parquet dependency Thrift0.25 generates its parser with Bison's `--file-prefix-map` option. macOS's system Bison2.3 is too old. CI provisions Homebrew Bison and places its bin directory first; local macOS profile builds need a modern Bison on PATH. This host tool is not included in the engine payload.

Parquet pins Arrow21.0.0 with the official CVE-2026-25087 fix backported for C++17. The default Kotlin/Native Linux sysroot's GCC8.3 standard library cannot compile Arrow25's C++20 span header. The pinned archive SHA512 and original vcpkg21.0.0#2 port patches are retained; the custom port revision adds the upstream reader/overflow regression changes with C++17 compatibility adaptations. No replacement C++ runtime or invented span header is used. Both Parquet target jobs must execute variadic StringView IPC prebuffer/generator regression against the actual packaged archive, including zero-copy/coalesced readers and checked arithmetic, before producing a report. See ports/arrow/gdal4k-notes.md for provenance. This profile validates individual GeoParquet files. Optional Arrow dataset-directory, compute, Flight, cloud and allocator features are not selected.
