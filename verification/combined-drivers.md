# Combined driver verification

The `combined` profile is the first packaging step toward the [JVM-first GDAL coverage goal](../GDAL_SUPPORT.md). Its Linux x64 glibc producer, publication and actual runtime evidence are **pending** until the current source passes the checks below. Linux ARM64, macOS ARM64 and Windows x64 combined coverage also remains pending. No complete GDAL coverage or performance improvement is claimed.

## Distribution and CI scope

The profile combines the existing scientific HDF5/netCDF4, Parquet/Arrow, native HTTP/TLS and PostgreSQL capabilities in one dependency closure. It uses its own prepared source tree, engine fingerprint, installed dependencies and local Maven version, `0.1.0-combined-SNAPSHOT`. The small default distribution and the seven isolated profile jobs retain their existing roles.

The optional-profile workflow adds `Profile combined on linux-amd64` beside the existing PostgreSQL job. Each matrix cell has a fresh runner and its own disposable PostGIS service, using the existing pinned `postgis/postgis:16-3.5` image digest. The combined cell installs the pinned h5py/numpy fixture dependencies; `driver-profile.py` owns its loopback HTTP/TLS fixtures and closes them before reporting final request logs. Each cell runs one default-engine size baseline followed by its isolated profile verification. Cache keys and artifact names include the profile name.

The initial Linux job exercises source JVM and Native tests and independent published JVM and Native consumers. JVM remains the first complete-support target; requiring these initial Native checks does not add combined coverage on the other Native targets.

## Acceptance evidence

All combined capabilities must be exercised through the same runtime and packaged engine in each fixture context. Driver registration alone is insufficient. The combined fixture must retain the existing real-data assertions and verify cross-driver conversion without switching engines:

- Closed HDF5 input and actual OS-path NC4 conversion/readback, including exact wide integers, complex and string values, typed NoData, attributes, units, scale and offset.
- Parquet field and geometry readback plus the packaged Arrow IPC regression.
- HTTP bounded ranges, validators, retry/deadline/cancellation, TLS trust/hostname rejection, sidecars and the native cache/registry regression.
- Disposable PostgreSQL/PostGIS create, write, read and delete, including field values and geometry coordinates. Cleanup must remove only the test's own table and outputs.
- Representative transfers between these capabilities in the same process, with explicit source/result ownership.

Current API cases, multidimensional conversion and dimension lifetime checks must also pass in source and independent published consumers. The source hash inventory, prepared profile identity, actual packaged JVM class/source/native bytes, isolated Maven coordinates, native dependency audit, size/gzip measurements and relevant raw execution results must match the current source. A passing isolated profile cannot substitute for this combined evidence.

## Retained diagnostics and limits

Profile artifacts contain the measured report and isolated local Maven archive. Evidence artifacts retain actual XML, request logs, public fixture certificates, prepared-source hash receipts and the generated profile fixture/probe Kotlin sources. The workflow also selects the seven `HttpMemoryProbe*.class` files, `CombinedDriverMemoryProbe*.class`, the generated `DriverProfileApiTest*.class`/`DriverProfileApiTestKt*.class` and `DriverProfileFixtures*.class` files, their `DatasetApiTestKt*.class`/`DatasetOutputPathKt*.class` helpers, and the `gdal4k_http_probe`/`arrow-ipc-check` executables so their actual bytes can be checked when those files were produced. Missing optional outputs must be recorded; an upload pattern alone does not establish that the bytes exist.

Fresh-JVM HTTP diagnostics retain stdout, whole-process OS RSS logs, request logs and the deterministic fixture. Cumulative JVM allocation, sampled heap and OS RSS remain separate measurements. Adding optional dependencies and reporting their size does not establish a speed or memory improvement. No WAN/TLS performance, universal, Native/Android performance, zero-copy, streaming or total-memory-bound claim follows from these diagnostics.

The combined diagnostic starts three fresh JVMs sequentially. Each process selects one combined JNI library and keeps one runtime for one warmup plus three measured rounds. Every measured round includes the cross-driver conversions, exact value/geometry readback, assertions and owned file/table cleanup. Runtime initialization, the warmup and final runtime shutdown are outside elapsed/allocation measurements; whole-process OS RSS includes them. The reporter retains stdout and `/usr/bin/time -v` output and converts Linux peak RSS from KiB to bytes. Cumulative allocation covers JVM threads; the 2ms heap sampler can miss brief peaks, and native allocations are outside those JVM counters. These small-fixture integration measurements are functional diagnostics, with no throughput or before/after improvement claim. They require their own source/probe/class/engine and raw-log bindings and do not reuse the earlier isolated-profile measurements as a performance baseline.

Whole-process RSS covers the JVM and its native engine; the separate fixture servers and disposable PostGIS service are outside that process and their memory is excluded.

The primary Web workflow retains the per-target `related-inputs-js` and `related-inputs-wasmJs` result directories, alongside its existing execution logs. This corrects the earlier mismatch between the generated result paths and the artifact pattern; it does not replace actual Node/Chromium execution.
