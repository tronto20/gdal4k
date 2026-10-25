# GDAL coverage goal

gdal4k aims to expose the public functionality of its pinned GDAL version on every platform that gdal4k supports. JVM is the first target for complete support. The current upstream baseline is GDAL 3.12.4; changing that baseline requires a new compatibility assessment.

The coordinate, vector-column, file-transfer, multidimensional-transfer, HTTP, footprint, tile-index and multidimensional-conversion workstreams are delivery milestones toward this goal. Completing those workstreams does not establish complete GDAL coverage.

## JVM first

The first complete-support acceptance matrix must cover the four existing JVM engine classifiers: Linux x64 glibc, Linux ARM64 glibc, macOS ARM64 and Windows x64. For each public API family, algorithm and driver capability, record the exposed Kotlin operation, engine build configuration, representative fixture, source test, independent published-consumer test and actual host execution evidence.

Generated adapters cover the declared Kotlin interfaces. Successful generation alone does not establish coverage of the upstream GDAL API. Likewise, a driver being registered does not prove its read, create, update, metadata, type or algorithm capabilities.

Optional scientific, Parquet, HTTP and PostgreSQL profiles currently have separate versioned dependency configurations and limited tested host matrices. Passing each isolated profile does not establish that their combined capabilities work in one process. Complete JVM support therefore requires a coherent combined-capability engine distribution and tests of its dependency closure and combined runtime behavior on all four JVM classifiers. The small default distribution remains a separate packaging policy.

The Linux x64 glibc combined profile was verified and merged in PR38; macOS ARM64 followed in PR40 and Linux ARM64 in PR41. They bring HDF5/netCDF, Parquet/Arrow, HTTP/TLS and PostgreSQL into one isolated engine and Maven version, with source and independent published-consumer runtime fixtures. Mac and ARM64 Linux evidence include their owned PostGIS providers and terminal cleanup. The existing seven isolated profile jobs remain separate checks. See [combined driver verification](verification/combined-drivers.md) for the acceptance scope and evidence requirements.

Linux ARM64 combined verification uses an x64 cross producer and a separate actual ARM64 JVM/Native execution job. Transferred source, complete engine hashes and isolated packages must match before normal JVM tasks accept the staged engine; Native compilation and actual execution retain separate receipts. PR41 completed these requirements with actual source/published JVM and Native execution and owned-provider cleanup. PR42 completed Windows x64 combined verification with actual source/published consumers, portable owned PostGIS cleanup, Windows kernel/process evidence, Schannel revocation controls and Winsock2 imports. Its tested head was merged and confirmed as an ancestor of fetched main. The remaining API/driver acceptance matrix and full GDAL support are incomplete.

## Extend the same contracts to the other current platforms

The existing targets also include six Kotlin/Native variants, Android ARM64, and JavaScript and wasmJs in Node and browsers. Functionality should use the same public contracts across these targets, with explicit platform access and resource-lifetime rules.

Compilation, simulator execution, emulated instruction execution and execution on a physical device are distinct evidence. In particular, current iOS-device compile/link evidence and Android ARM64 JNI execution through an x86 native bridge must retain those labels. A missing runtime fixture remains a verification gap.

Host, sandbox, filesystem, network, dependency or upstream limitations must be recorded per capability and platform. A documented limitation does not count as an implemented capability; it identifies the next implementation, packaging or verification work. Do not infer HDF5, netCDF or other optional-driver availability on hosts that have not executed the corresponding fixtures.

## Preserve behavior while expanding coverage

Tests must cover meaningful values and failures, including exact Int64 and UInt64, complex and string values, nullability, shape and dimension order, NoData, attributes, CRS, cancellation, callbacks and ownership. Converters must preserve these properties or explicitly reject or describe the target format's losses. A successful GDAL return handle alone is insufficient when a utility can report copy failures separately.

Returned independent results must remain usable after their source closes. Reference-based outputs must state their dependency lifetime instead. Existing and partial output ownership must be explicit, including sidecars and directories.

Performance claims require a measured bottleneck and controlled before/after runs with identical inputs and environment. Elapsed time, cumulative allocation, sampled heap and whole-process peak RSS are separate measurements. New functional diagnostics are useful evidence but do not imply an improvement, streaming, zero-copy behavior or a bound on total memory.

## Remaining coverage assessment

With multidimensional conversion merged in PR37, continue the JVM acceptance matrix and close the remaining API and packaging gaps. The initial source inspection identifies multidimensional views and metadata, algorithm registry and pipelines, additional utility algorithms, Arrow writes, geometry precision and style tables, general VSI access, scoped configuration and error handling, and asynchronous/thread-safe dataset facilities as areas requiring further assessment. Existing APIs must be checked before adding overlapping replacements.

The JSON string subtype workstream adds `MDDataType.JsonText` with protocol-6 capability negotiation, strict schema fields and raw nullable text values. Local Linux x64 v6 MEM/schema/lifetime/copy and old-transport controls pass; a client compiled against the previous Text API also runs against the new API JAR with identical Text class bytes. The general netCDF writer cannot retain this subtype, so conversion rejects the selected JSON schemas before output creation. Fresh published consumers on all four JVM classifiers, optional-driver execution and the remaining platform matrix are still acceptance gates; local checks alone do not close this coverage gap.

The goal is complete only when the applicable capabilities have both an implementation and the required platform evidence. A passing PR, a count of generated methods or completion of a fixed workstream list does not satisfy that acceptance condition.
