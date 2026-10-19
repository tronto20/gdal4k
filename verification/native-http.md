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
