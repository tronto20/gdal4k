# Native HTTP investigation

JS/wasmJs already support HTTP input with a bounded 8 MiB cache, 64 KiB chunks,
per-attempt deadline, bounded retries, validators, cancellation and related
sidecars. JNI/Native default engines exclude CURL and reject explicit HTTP policy.
The existing public `HttpInputPolicy` and dataset APIs remain the contract.

The JNI Android directory provider resolves read-only related names into local
descriptor paths and retains them through dependent VRT leases. Pure Native has
no equivalent byte provider. Replacing these with eager HTTP downloads would
discard random access and bounded transfer. An isolated optional CURL profile
can supply a common native backend without adding CURL to every default payload.
Its availability must be negotiated from the actual engine, with explicit policy
rejection on engines lacking the backend. Supported hosts require actual profile
runtime/independent-consumer verification; a source flag establishes no support.

Stock GDAL HTTP options alone do not establish the existing Web contract. The
pinned GDAL 3.12.4 `CPLHTTPFetchEx` exposes bounded write/progress callbacks and
fractional-second deadlines, but its public result exposes CURL status, not the
HTTP response status. A custom range adapter must validate actual status, ranges,
body length, identity, encoding and failures, retain leases, bound its cache, and
stop retries/backoff/in-flight requests on cancellation. TLS verification must
remain enabled; tests must cover trusted/untrusted certificates, sidecars,
redirect policy and sensitive-URL error handling before claiming support.

Before runtime changes, `HttpMemoryProbe` measures a real download-then-local-open
control against an owned deterministic uncompressed tiled Byte GTiff served over
loopback HTTP. It streams the complete download through 64 KiB to a temporary file
and validates every pixel in three non-overlapping 256-square windows, including
the last tile. Fixture generation runs outside the measured JVM. Its local warmup
is outside elapsed/allocation and inside process RSS; download/open/read/checksum/close/removal are measured. Request
count and server response-body bytes are independent from latency and memory.
The fixture is not claimed to be COG; loopback does not model WAN/TLS latency.

Commit and freeze the versioned harness and actual engine/class identities before
the original baseline. Keep it immutable and separately labelled. Final controlled
download/range comparisons must use identical final compiled probes/production
classes, fixtures/settings and actual producer payloads; an old engine can measure
the original baseline but cannot validate changed C++. No improvement is claimed
until both sides and their raw logs/hashes have been checked.

## Original download control

The committed harness `66ef488` compiled with normal Gradle and its 1,024-square
smoke passed. The original baseline then ran three fresh JVMs sequentially with
JDK 21.0.2 and the frozen, actual PR33 Mac ARM64 normal Gradle engine (fingerprint
`1b6da019...`). No runtime implementation changed. The 8,192-square fixture is
67,117,214 bytes; each process made one complete HTTP GET and validated 196,608
pixels in three windows. A 64 KiB streaming download buffer avoids an artificial
whole-file heap allocation in the control.

Median download/open/read/close/checksum/removal time was 366.59 ms, cumulative
JVM allocation 0.902 MB and whole-process OS peak RSS 95.22 MB. Server response
body bytes were 67.12 MB per process. These are baseline diagnostics on loopback,
without added latency/throttling, and establish no speed or memory improvement.
JVM allocation excludes native allocations; sampled heap and OS RSS are separate.
The elapsed samples ranged from 235.63 to 386.11 ms, so later comparisons must
preserve raw values and alternate modes rather than extrapolate WAN performance.

The 889 production classes, seven probe classes, source/harness hashes, six raw
stdout/OS logs, three request logs, fixture and independently recomputed pixel
checksum were verified. Original class/source snapshots are preserved under
`verification/build/http-baseline`; the native library remains read-only in the
separate frozen MD checkout. Its name/inode must not be rebuilt or mutated. The
report is `verification/results/http-memory-before-original-macos-arm64.json`
and its verification proof is
`verification/results/native-http-stage1/before-baseline-verified.json`.

The next implementation uses an isolated optional HTTP/CURL profile with a
dedicated native range handler, rather than treating stock `/vsicurl/` options as
proof of strict identity/range semantics. Default engines retain explicit policy
rejection. Actual profile engines, platform support, TLS, failure paths, leases,
independent consumers and final controlled download/range measurements remain to
be implemented and verified. This decision adds no CURL dependency to defaults.

## Implementation under verification

The optional profile adds GDAL's `curl` feature and explicit CURL `ssl`/`c-ares`
features in its own manifest/prefix/version. Normal producer configuration turns
the adapter on only for that manifest. Default Native/JNI engines return HTTP
capability zero; old JNI engines without the new symbol also reject explicit
selection. Web mounting and its policy checks remain in the existing Worker.

The native plugin performs strict ranged requests through libcurl's multi API,
checks cancellation during requests and backoff, and bounds headers, accepted
response bodies and a per-mount LRU. It validates response status, range, size,
encoding, length and selected validator before accepting bytes; identity errors
poison subsequent provider reads, including cached chunks. GDAL's separate block
cache can still return previously read pixels. Errors contain fixed diagnostics,
not source URLs. Redirects and URL userinfo are rejected. Peer/hostname TLS
verification remains enabled; child-process CA configuration is explicit.

HTTP mounts follow actual native dataset destruction and transitive VRT source
leases. Callback failures ignored by a driver still fail the enclosing operation
and close newly allocated handles. Existing caller-owned direct output partials
are not rolled back. Sources are retained until native work finishes; cancellation
does not kill threads or free an in-flight CURL handle from another thread.

Ten profile-only public cases are compiled into source JVM/Native tests and both
independent consumers. They use an owned HTTP/TLS server with recorded actual
requests and cover bounded windows/VRT leases, PAM/missing sidecars, retry/auth,
full/truncated/misencoded/mis-sized responses, redirects, poisoned identity/size,
strong/weak/missing validators, trusted/untrusted/hostname TLS, deadlines, actual
in-flight/backoff cancellation and read-only/userinfo rejection. The profile also
runs three fresh download/range JVM pairs with identical final classes and the
same actual profile engine. Test definitions are not evidence of execution.
An additional CMake-built, source-fingerprinted native probe reads three related
files through the real range handler, checks their complete bytes/checksum,
observes cache eviction/reload under the 8 MiB bound and confirms registry release.
It supplements the public consumers rather than establishing packaged API support.

Local compile-only JVM template validation and the 38 offline checks passed.
The new header passes syntax checking with the local SDK CURL headers; a full
syntax-only check against non-pinned system GDAL 3.13 stops on that version's
existing `GDALGetMetadata` const-signature difference. No system GDAL was linked
or executed, and no production source was changed to accommodate that mismatch.
These checks establish neither pinned GDAL 3.12.4 runtime behavior nor performance.
Fresh normal Gradle current CI producers and full actual verification remain
mandatory; the preserved MD engine is used only by the frozen original baseline.
