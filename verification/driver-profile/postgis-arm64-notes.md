# Owned Linux ARM64 PostGIS fixture

The provider starts with the official PostgreSQL 17.11 Bookworm ARM64 manifest
`sha256:f97ca6038b7641359fef23126949d824ac68edd8a7081b424d6a78e1f7076f8e`,
from [docker-library/postgres commit 2603e26e](https://github.com/docker-library/postgres/tree/2603e26e245e558218728ee14e0a42dcb020dc7f/17/bookworm).
All 14 compressed layers and uncompressed config diff IDs were independently
hashed in RAM. Ordered tar entries, including whiteouts, yielded the actual base
144-package dpkg status and AArch64 PostgreSQL binary hashes without executing them.

The lock adds 83 exact packages (43,467,628 compressed bytes): 81 Debian Bookworm
packages and the official PGDG PostGIS 3.6.4 extension/scripts for PostgreSQL 17.
The [PGDG repository](https://wiki.postgresql.org/wiki/Apt) InRelease signature was
verified with official key fingerprint `B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8`;
Debian InRelease signatures were verified using the archive keyring read from the
pinned base. Both index hashes and every package version, architecture, size and
SHA256 are recorded. The dependency metadata keeps every existing base version,
includes required Depends/Pre-Depends, checks Conflicts/Breaks, and excludes
Recommends. This is a deterministic required closure, not a global package-count
optimization or an installation claim. Retained signed metadata and full proofs
are under `verification/results/linuxarm64-provider-metadata-20261023/`.

The manager downloads exact hash-checked DEBs only on a matching Linux ARM64 host.
The Docker build has no network during RUN and compares the complete installed
package inventory to the lock. Runtime acceptance also hashes all four PostgreSQL
provider binaries against the bytes independently read from that base. Package
versions reject whitespace, including accidental Deb822 continuation content.
It uses a uniquely owned idle container and volume,
a password written through stdin into a private file, a host-only PGPASSFILE and
a loopback ephemeral published port. PostgreSQL listens on `*` inside its bridge
container; host exposure is independently restricted to `127.0.0.1`. Provider
GDAL/GEOS/PROJ remain inside this separate database container and are never engine
build/runtime dependencies. Existing Mac and isolated Linux fixtures are unchanged.

Acceptance requires an actual ARM build and server: version/extension identity,
ELF AArch64 provider bytes, new-connection POINT/value readback and exact table
deletion, source/published GDAL consumers, then graceful owned server/container
shutdown, closed port and exact owned volume removal. Only sanitized receipts,
commands and public logs enter results; credentials, PGDATA, DEBs and image layers
stay outside artifact globs. Host credential files and any private unsanitized
command output are removed on exit. A graceful-stop failure still attempts
identity-checked container cleanup and remains a failed acceptance result.
Metadata checks and unit tests alone do not establish
an ARM provider or combined-driver runtime pass.
