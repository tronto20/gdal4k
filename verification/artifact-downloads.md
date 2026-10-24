# Bounded audit downloads

The primary and optional-profile jobs retain their artifact names and portable
payloads for normal CI consumers. Large public evidence also has a
separate `audit-parts--` transport for clients with a 32 MiB download limit.
This changes verification-file delivery, not Maven coordinates or engine content.

The local upload wrapper passes the same public path patterns to the original
uploader and the audit collector. The collector removes duplicate paths, rejects
links and private provider/credential paths, and compresses the selected files
once into a ZIP with workspace-relative paths. Every selected file retains its
size, SHA256 and CRC. This is a new container; its bytes are distinct from the
original GitHub artifact ZIP. An inner `.tar.gz` or `.tar.xz` retains its exact
compressed bytes; a profile archive can still be checked against the report's
`archiveSha256`.

Primary engine and assembled Maven transfers use XZ compression to reduce total
bytes. The artifact names (`engine-*`, `gdal4k-maven`, `gdal4k-jvm-maven`) stay the
same; their inner filenames now end in `.tar.xz`. This changes the CI transport
container, including its compressed hash and the containing service ZIP hash.
Maven coordinates, JAR/AAR/KLIB files, libraries, licenses and test executables
are carried without modification. Optional-profile public Maven archives and
the combined ARM64 transfer remain gzip in this rollout.

`verification/ci.py` keeps gzip as its command-line default; the primary workflow
selects `--archive-format xz`. Each producer and reader checks its Python XZ
support before long builds. Staging records the actual compressed and decoded
TAR SHA256 and byte lengths in the job output. The writer serializes the TAR
once through a streaming compressor, without an intermediate raw TAR file.
Historical artifacts and their recorded hashes are preserved.

Use the shared reader for either format. Give it the exact expected logical
archive names; it rejects missing, unexpected or duplicate gzip/XZ candidates,
invalid codec data and unsafe member paths. For example:

```sh
python verification/archive_transport.py restore \
  --directory verification/build/downloads --destination . \
  --expected complete-maven
```

For an external tool that previously opened `complete-maven.tar.gz` directly,
accept `.tar.xz` as well and continue checking the selected compressed file's
actual digest. Legacy gzip downloads remain readable. Never substitute an XZ
digest for an existing gzip digest or relabel a reconstructed ZIP as the original
service ZIP. Compression savings are measured on the same decoded TAR stream;
the download part size alone does not measure total storage or transfer savings.

An audit ZIP below 24 MiB needs no additional export. Larger ZIPs are split into
parts of at most 24 MiB minus 64 KiB, leaving room for the artifact service's ZIP
wrapper. The uploader checks that every actual download ZIP is at most 24 MiB.
It uses the official artifact SDK with runner-provided authentication and a
locked dependency installation with package scripts disabled. Each source has at
most 100 parts and each job at most four sources, including the preserved
originals and final indices: at most 408 artifacts within the service's 500 limit.
The current workflows use at most three original uploads per job.

Each transport index records the workflow run/attempt/job, PR head, actual
checkout commit and parents, original artifact ID/ZIP digest, collector source
hash, member inventory and whole/part byte identities. Raw-part SHA256 values
and the service's outer ZIP digests are different fields. The index is uploaded
last; missing parts or an absent index cannot establish a complete export.
Names start with `audit-parts--`, so existing `engine-*` and exact-name CI
downloads continue to select their original artifacts.

For an external audit, first verify that the index belongs to the intended run,
attempt and tested head. Download the index and its listed part artifacts using
their IDs, checking each service ZIP digest before unwrapping. Put only its
`manifest.json` and the numbered raw parts in a new directory. Use the manifest
SHA256 from the index when reassembling:

```sh
python verification/package-audit-parts.py reassemble \
  --directory /ABS/PARTS \
  --output /ABS/NEW-audit-content.zip \
  --manifest-sha256 SHA256_FROM_VERIFIED_INDEX
```

Reassembly verifies the exact part inventory, order, offsets, sizes and hashes,
the whole ZIP identity, and every member's bytes/CRC. It rejects existing output
files and unsafe paths. Inspect the reconstructed contents against the current
source, profile report and raw runtime receipts before accepting a capability.
Splitting files does not establish a successful engine build or runtime test.
