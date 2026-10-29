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

The JSON string subtype workstream was merged in PR44. It adds `MDDataType.JsonText` with protocol-6 capability negotiation, strict schema fields and raw nullable text values. A client compiled against the previous Text API runs against the new API JAR with identical Text class bytes. The general netCDF writer cannot retain this subtype, so conversion rejects the selected JSON schemas before output creation. All 34 required CI jobs and retained evidence for the 55 artifacts and 19 named validation families were accepted for the tested PR44 head; its main ancestry and merged file tree were verified. The evidence keeps its actual execution and coverage labels, including the existing iOS and Android limits described above. This closes that scoped workstream, not the remaining API/driver matrix.

The multidimensional metadata workstream was merged in PR45. It exposes an independent MDArray CRS copy, unsigned natural block sizes, detached structural information, nullable scale/offset metadata with the driver's storage type, and signed SRS axis mapping through the existing runtime dispatcher. All 25 required CI jobs and retained source, independent published-consumer and platform evidence for 46 logical artifacts were accepted for the tested PR45 head; its main ancestry and merged file tree were verified. These results retain the compilation, simulator and emulation limits described above. This closes that scoped workstream; multidimensional views and the remaining GDAL API/driver matrix stay open.

PR46 completed the private Windows dump self-check readiness fix. Actual Windows execution confirmed the owned child's marker and pipe EOF before a bounded dump, quota rejection and subsequent independent Native consumer execution. All 25 required CI jobs and retained byte, package and runtime evidence for 46 logical artifacts were accepted on its tested head, followed by verified main ancestry and tree equality. This adds no public Kotlin API. Remote Maven publication remains separate from CI and independent consumer acceptance.

The current transpose workstream adds `MDArray.transpose(IntArray)` as a parent-scoped, lazy aliased view. It includes a narrow patch to GDAL 3.12.4 for signed CRS axis remapping, preserving unmapped zero entries and avoiding signed overflow for invalid stored mappings. Fresh patched engine binaries, exact typed values, scalar/inserted axes, alias writes, metadata, ownership, cancellation, NC4 fixtures and independent consumers require current-head verification. General string slice views (`getView`), unscaled views and masks remain separate implementation gaps. Neither this workstream nor its eventual acceptance completes the remaining GDAL matrix.

The goal is complete only when the applicable capabilities have both an implementation and the required platform evidence. A passing PR, a count of generated methods or completion of a fixed workstream list does not satisfy that acceptance condition.
