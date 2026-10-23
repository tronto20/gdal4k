# Owned portable Windows PostGIS fixture

This provider is test infrastructure for the combined Windows JVM driver profile.
It is not part of the distributed GDAL engine. The source and pure tests are
implemented; actual Windows archive-parser bootstrap, DLL loading, database,
private DACL/process-tree cleanup and combined GDAL runtime acceptance must run
on the Windows CI host before platform support is approved.

The lock pins three complete official inputs: EDB PostgreSQL 17.11-3 portable
binaries, the PostgreSQL project's PostGIS 3.6.2-1 NSIS bundle for PG17 x64, and
Microsoft Desktop VCLibs x64 14.0.33321.0. PostGIS and Microsoft SHA256/SHA512 are
first-observed official HTTPS bytes, not publisher-published strong checksums or
signature validation. The separate official catalog PostGIS MD5 is retained as a
corruption cross-check. The previously denied OSGeo ZIP is not an input and is
not retried. The NSIS EXE is opened only as archive data; its installer is never
executed. No service, host installation, registry, trust store or global PATH is
changed.

All archive files are streamed and hashed before selected extraction. To keep
the lock reviewable, it stores the canonical SHA256/count/total-byte identity of
each complete file inventory, plus each selected original member's size and
SHA256. Canonical inventory JSON is a sorted object of member names to
`{bytes,sha256}`, serialized with separators `(',', ':')` and UTF-8. Every member
is rehashed at preparation time; selected files are checked again after copying.
Windows path escapes, ADS/device names, case collisions, links/reparse points,
parent-file shadowing and unsupported member attributes are rejected.

NSIS parsing uses the official 7-Zip 26.05 full `7z.exe` and `7z.dll`. An exact
release-pinned `7zr.exe` extracts only these two files and `License.txt` from the
release's setup archive, without running that setup archive. The lock includes
both parser input identities, extracted image hashes, architecture/import
metadata and license hash. Parser payloads, license, actual command output and
PID identities are retained in public evidence. `7zr` alone is not assumed to
support NSIS. The parser is fixture infrastructure and is not shipped with GDAL.

The selected layout contains 2,937 files (282,510,235 bytes), including 38 AMD64
PE images reachable from the four PostgreSQL CLIs, PL/pgSQL and the PostGIS
geometry library. Normal and delay-load import names are inspected. Every
non-OS dependency is an exact private selected image. Seven differing shared
DLL paths intentionally select the matching-major PostGIS bundle's bytes over
the EDB bytes; both hashes and the winner are recorded in the lock. Actual
Windows loader/export compatibility remains a runtime gate. The full source
archives also contain other architectures and GUI/optional extension programs;
those are excluded from this fixture's runtime claim.

The private Microsoft `msvcp140.dll`, `vcruntime140.dll` and
`vcruntime140_1.dll` come from one exact x64 Desktop VCLibs appx. Its original
manifest, block map, signature and catalog are retained as provenance data.
Their presence is not a certificate/signature trust claim. The fixture never
silently uses an arbitrary system-installed MSVC runtime, and does not install
the appx. Windows OS/API-set imports are an explicit host boundary.

`fixture(output, target='windows-amd64', archives=None)` acquires the exact
locked inputs under its own private work directory. An explicit override must
provide all three keys `{postgres,postgis,microsoft}`; partial overrides fail.
The yielded descriptor contains only a loopback DSN, private `PGPASSFILE` and
`PGCONNECT_TIMEOUT`, aggregate input identity, and public evidence directory.
The aggregate identity is not a single archive's checksum. Provider DLL/data
PATH settings are limited to server/client subprocesses and are not passed to
the GDAL JVM process.

A fresh cluster uses UTF-8/C, a random database/user and a password supplied
through an owned private file. Win32 owner/protected current-user+SYSTEM DACLs
are verified. Only 127.0.0.1 is accepted. The provider verifies the actual PID,
creation time, image path, data directory, listener, server/PostGIS versions,
then commits Point(3,4)/value42 and reads it from a new connection. Only its exact
random table is dropped. Short commands use an owned Job Object; the `pg_ctl
start` job stays open for the fixture's lifetime and captures output in files
rather than waiting for pipe EOF inherited by a surviving Windows shell.
Finally, exact `pg_ctl -D <owned PGDATA> ... stop` must succeed, the original
process/listener must disappear; its retained job has a bounded ten-second
graceful drain for the exiting cmd.exe ancestor, then must close without forced
termination. Failures preserve sanitized command output and original errors.
Provider/PGDATA/private work remain outside public artifacts; only owned secret
files are removed after verified successful cleanup.

`postgis-windows-report.py` verifies intended profile/DSN/input identity, current
source/lock/helper bytes, all physical provider files, retained four actual
provider EXEs/parser bytes, full input-inventory receipts, command/output/PID
bindings, actual SQL results, verified DACLs and terminal cleanup. It rejects
unexpected extra provider/public files. PostgreSQL's normalized Windows slash
spelling is compared as a canonical Windows path without discarding raw output.

Official provenance:

- https://www.postgresql.org/ftp/postgis/pg17/v3.6.2/win64/
- https://postgis.net/documentation/getting_started/install_windows/enabling_postgis/
- https://www.enterprisedb.com/download-postgresql-binaries
- https://learn.microsoft.com/en-us/windows/msix/msix-troubleshooting-guide
- https://learn.microsoft.com/en-us/cpp/windows/deployment-in-visual-cpp
- https://www.7-zip.org/download.html
- https://github.com/ip7z/7zip/releases/tag/26.05
