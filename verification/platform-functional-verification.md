## Functional verification on current platforms

Each executable row must run the shared public functional fixtures in the source project and an unrelated consumer of the published artifacts. The table describes the verification paths; an individual row passes only when a current CI run provides its actual case results. Compile-only targets and missing physical-device runs remain verification gaps. Overall GDAL coverage is tracked in [GDAL support](../GDAL_SUPPORT.md).

The shared suite includes dataset/band and raster utilities, vector/schema/layer operations, callbacks and transfers, and the multidimensional regression fixtures. For the current MDArray changes, run `MDArrayPlanningApiTest` and `MDArraySettersApiTest` on every execution row: exact counts and GDAL's processing-chunk choices for requested byte budgets; scalar, string, view and UInt64 boundary shapes; typed scale/offset without sample conversion; copied/cleared CRS and signed axes; captured unscaled transforms; and VRT close/reopen persistence. Web size_t boundary expectations reflect the actual wasm32 ABI. They do not omit the boundary fixture. Published consumers copy these current common fixtures, rather than substituting a startup probe.

| Platform / engine | Source functional suite | Published consumer | Execution label / remaining gap |
| --- | --- | --- | --- |
| JVM Linux x64 glibc | Native Linux job runs both JVM suites; assemble repeats runtime suite | Assemble runs explicit host payload consumer | Actual Linux x64 JVM/JNI |
| JVM Linux ARM64 glibc | Both JVM suites run against the verified staged engine on the actual ARM64 host; combined runs the same suites | Actual ARM64 runtime job | Actual ARM64 JVM/JNI; cross-build is separate |
| JVM macOS ARM64 | Native macOS job runs both JVM suites | Dedicated macOS published-JVM job | Actual Apple Silicon JVM/JNI |
| JVM Windows x64 | Native Windows job runs both JVM suites | Dedicated Windows published-JVM job | Actual Windows JVM/JNI |
| Native Linux x64 | `linuxX64Test` | Independent `linuxX64Test` | Actual x64 host |
| Native Linux ARM64 | x64 links, actual ARM64 job executes source binary | Same ARM64 job executes independently linked consumer | Actual ARM64; retain executable SHA and completed case stdout separately from cross-link receipt |
| Native macOS ARM64 | `macosArm64Test` | Independent `macosArm64Test` | Actual Apple Silicon host |
| Native Windows x64 | `mingwX64Test` | Independent `mingwX64Test` | Actual Windows host |
| Native iOS ARM64 | Compile/link only | Compile/link only | **Physical-device functional execution missing**; `--ios-device` does not change this branch |
| Native iOS simulator ARM64 | `iosSimulatorArm64Test` | Independent simulator test | Apple Silicon simulator; not physical iPhone/iPad |
| Android ARM64 | Instrumentation common fixtures | Independent published instrumentation consumer | ARM64 JNI through x86_64 emulator native bridge; **physical ARM64 device execution missing** |
| JS / Node | `jsNodeTest` | Independent `jsNodeTest` | Actual Node process |
| JS / browser | `jsBrowserTest` | Independent browser test | Chromium/ChromeHeadless |
| wasmJs / Node | `wasmJsNodeTest` | Independent `wasmJsNodeTest` | Actual Node process + Wasm engine |
| wasmJs / browser | `wasmJsBrowserTest` | Independent browser test | Chromium/ChromeHeadless + Wasm engine |

NC4 storage and persistence require the actual scientific capability. Scientific/combined profiles run the same rendered `DriverProfileApiTest` in source and published consumers on their configured desktop hosts; combined also has actual Linux ARM64 execution. Its existing `scientificArraySetters` checks Float32 scale/offset attributes across reopen, existing attribute type preservation, rejected half-type hints, unchanged samples, read-only behavior and CRS association clear. The existing `scientificArrayResampled` fixture independently reopens a Float64[4,4] NC4 array twice after writer closure and verifies total16, block[2,2], and processing chunks for requested 1-byte and 128-byte budgets. iOS, Android and web have no configured NC4 profile: their NC4 capability is unverified, and cannot be marked passed by the MEM/VRT fixtures.

Every accepted cell needs current source/test-merge identity, actual host or device identity, tested engine SHA/fingerprint, exact source and rendered-consumer fixture hashes, and the meaningful testcase names with successful execution and no unexpected skips. Native consumers must hide the producer engine/cinterop paths; JVM consumers must resolve only the requested published classifier in a fresh unrelated project. Compile-only, simulator, native-bridge and physical-device results retain separate labels. Configuration and source review establish no runtime acceptance.
