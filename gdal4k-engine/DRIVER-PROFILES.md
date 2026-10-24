# Optional driver profiles

The default manifest, 32 publication coordinates, four JVM classifiers and target matrix are unchanged. Optional dependencies are produced in an independent source snapshot, vcpkg installation, CMake build and local Maven repository under `build/driver-profiles/`. They never write the default engine prefix or replace a default Maven version.

| Profile | Dependencies and capability | Verification scope |
| --- | --- | --- |
| scientific | HDF5 dataset reads; netCDF create/write/read | Linux x64/glibc, macOS ARM64 |
| parquet | Apache Arrow C++ Parquet; GeoParquet create/read with geometry and attributes | Linux x64/glibc, macOS ARM64 |
| postgresql | libpq; PostgreSQL/PostGIS vector connection and create/write/read/delete | Linux x64/glibc |
| http | CURL bounded HTTP/TLS input and policy/cache fixtures | Linux x64/glibc, macOS ARM64 |
| combined | Scientific, Parquet, HTTP/TLS and PostgreSQL in one engine/runtime | Linux x64/glibc PR38, macOS ARM64 PR40 and Linux ARM64 PR41 verified; Windows x64 implementation and actual CI acceptance pending |

Host acceptance requires exact-HEAD producer/package evidence and actual source and independent published-consumer fixtures; adding a CI cell does not establish its runtime result. Linux ARM64 combined uses the verified cross-build/runtime phases below. Windows combined is being implemented with a dedicated actual-host job and owned portable fixtures; its runtime result is pending. Apple mobile, Android and ordinary single-thread Wasm are not claimed for these profiles. The producer rejects unsupported combinations before installation. Arrow is restricted by its vcpkg architecture support, PostgreSQL requires native libpq/networking, and mobile/Wasm dependency closure and size need separate validation. Default drivers remain available on the existing targets. The isolated `postgresql` profile remains Linux x64-only; Mac, ARM64 Linux and proposed Windows coverage are scoped to `combined`.

```shell
python3 gdal4k-engine/tools/driver-profile.py scientific macos-arm64
python3 gdal4k-engine/tools/driver-profile.py parquet linux-amd64
python3 gdal4k-engine/tools/driver-profile.py combined macos-arm64
```

Run on the matching host with Java 21 and the regular producer prerequisites. Scientific and combined fixtures additionally require `h5py==3.13.0` and `numpy==2.2.6`. Linux database CI cells own a pinned disposable PostGIS container at port5432. The Mac combined command owns a provider pinned by `verification/driver-profile/postgis-fixture-lock.json`, a fresh private data directory and a loopback-only server at an allocated port. Its generated DSN contains no password; a private mode0600 `PGPASSFILE` is passed only through child environments. No existing database or machine service is used. Tests create fresh uniquely named tables and delete only those exact tables.

The Mac server stays alive through source tests, independent consumers and diagnostics, then stops before the report is accepted. Public receipts and sanitized command logs bind the current fixture/lock, provider hashes and architectures, startup checks, server version, port closure and owned-process exit. Private credentials, PGDATA and the provider bundle remain outside uploaded evidence. See [combined verification](../verification/combined-drivers.md).

The Windows combined job uses a portable PostgreSQL/PostGIS provider and private Microsoft Desktop VCLibs runtime with exact archive identities, extracted without installing them. Its server has a private data directory and passfile protected by Windows ACLs, an allocated loopback port and child-only DLL search paths. No machine service, registry, global PATH or trust store is changed. Before the producer, an actual Windows helper check must establish kernel peak working-set reporting, process identity, private ACLs and cleanup of an owned child tree. Windows TLS fixture certificate tools are hash-pinned separately; the GDAL CURL backend retains Schannel. These source definitions remain pending actual Windows CI evidence.

The command tests the source JVM and Native APIs and independent published JVM/Native consumers, including actual driver data operations. Native consumers hide all three producer archive/cinterop paths. A dependency audit rejects unbundled libraries. `report.json` records actual default/profile native archive and JNI bytes, deterministic gzip sizes, hashes, test counts and the selected source inventory. To obtain size comparisons, build the unchanged default engine for the same target first (CI does so explicitly).

Linux ARM64 uses `verification/driver-profile/arm64-profile.py cross-build --transfer <archive>` on Linux x64 after its normal default engine build, then `run --transfer <archive>` on actual Linux ARM64. The cross phase links source and independent Native tests and publishes the isolated JVM/Native packages. The runtime phase verifies all transported files, current source and engine fingerprints before normal JVM tasks use `gdal4kVerifiedStagedEngine=linux-arm64`. That narrowly scoped property verifies a complete existing engine and has no build fallback; it is not a test skip or a Native compiler on ARM. Actual Native stdout/command receipts supplement, and never overwrite, the original compile-only receipt. Test-only runtime configuration supplies the owned HTTP/TLS, HDF5 and PostGIS fixtures without patching prepared source. The ARM provider uses an exact official PostgreSQL ARM image and offline hash-locked DEBs; [provider provenance](../verification/driver-profile/postgis-arm64-notes.md) distinguishes metadata from actual server execution.

CI uploads a local Maven repository archive and verification evidence; license notices are embedded in the engine. This does not imply that every original copyright file is uploaded separately. Extract its `maven/` folder, add it as a Maven repository, and select the profile version consistently for API, runtime and engine. For example a `0.1.0-SNAPSHOT` checkout yields `0.1.0-scientific-SNAPSHOT`:

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

The Parquet dependency Thrift0.25 generates its parser with Bison's `--file-prefix-map` option. macOS's system Bison2.3 is too old. Both Mac Parquet and combined CI cells provision Homebrew Bison and place its bin directory first; local Mac builds of either profile need a modern Bison on PATH. This host tool is not included in the engine payload.

Parquet pins Arrow21.0.0 with the official CVE-2026-25087 fix backported for C++17. The default Kotlin/Native Linux sysroot's GCC8.3 standard library cannot compile Arrow25's C++20 span header. The pinned archive SHA512 and original vcpkg21.0.0#2 port patches are retained; the custom port revision adds the upstream reader/overflow regression changes with C++17 compatibility adaptations. No replacement C++ runtime or invented span header is used. Both Parquet target jobs must execute variadic StringView IPC prebuffer/generator regression against the actual packaged archive, including zero-copy/coalesced readers and checked arithmetic, before producing a report. See ports/arrow/gdal4k-notes.md for provenance. This profile validates individual GeoParquet files. Optional Arrow dataset-directory, compute, Flight, cloud and allocator features are not selected.
