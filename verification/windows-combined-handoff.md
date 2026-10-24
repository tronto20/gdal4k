# Windows combined profile continuation

The owned branch is `codex/windows-combined-drivers`, based on PR41's merge
`50018b23ff6b765958ef2397e664085870454fed`. The current work adds Windows x64
verification of the combined HDF5/netCDF, Parquet/Arrow, HTTP/TLS and PostgreSQL
profile. Windows runtime acceptance is still pending. This does not complete the
[JVM-first GDAL coverage goal](../GDAL_SUPPORT.md).

## Reproduce from versioned inputs

Use the current branch's `.github/workflows/driver-profiles.yml` Windows job and
the versioned fixture locks. Download and verify their exact official inputs;
local ignored reports, old producer caches and local absolute paths are not
required inputs. The PostgreSQL/PostGIS/Microsoft packages are extracted into
the fixture's private workspace without installation. Keep provider DLL paths
out of the Gradle and GDAL environment.

The Windows job uses Java 21 and CPython 3.12, including hash-pinned certificate
generator wheels. Its early process and TLS fixture checks must run on actual
Windows. A Linux source test or a mocked Win32 receipt cannot replace them.
Use normal Gradle tasks and the versioned verification scripts for all producers
and consumers. Inspect available disk space before builds or large downloads.

Windows profile snapshots use `build/driver-profiles/w/<24-hex-key>/source` to
leave room for vcpkg's versioned-port Git index locks and other native build
paths. The full canonical source/fixture identity remains in `inputs.json`;
existing-directory identity mismatches and prepared-source changes are rejected.
Install, buildtrees and package directories remain inside that isolated source
checkout. Reports and evidence paths still derive from the snapshot directory.

The generated Windows Gradle project selects its own `source/v` vcpkg clone.
Normal Gradle tool preparation verifies that installation before the profile
enables repository-local Git long paths. The pinned vcpkg tool starts versioned
port checkouts with an explicit minimal environment, so their long-path setting
must be in that private repository. The profile verifies the actual tool path,
pinned revision and private Git metadata before mutation. A public configuration
receipt is rechecked against the physical clone during final reporting; shared
tool stores and user/global Git configuration are not modified.

The Windows-only libpq overlay canonicalizes source paths in PostgreSQL's shared
storage, utility, catalog and node header generators. Meson's `File` objects can
retain parent segments, leaving a Perl script's raw Windows path longer than its
canonical path. The patch preserves the pinned scripts, inputs, targets and
dependency tracking. Offline fixtures check all four Meson files and execute the
lock-name and wait-event generators; actual Windows build/runtime acceptance
still requires the current CI evidence.

The pinned Boost.Atomic overlay also carries its existing PUBLIC Windows API
definition through Clang-MinGW builds so Boost.Thread can see WaitOnAddress.
The private compiler header overlay avoids three legacy MinGW declarations that
conflict with Clang's constexpr intrinsic definitions. Compiler/SDK versions,
global CPU flags and the default engine dependency policy remain unchanged.

Abseil's MinGW FIReference workaround uses a project-local compile definition
instead of replacing `CMAKE_CXX_FLAGS`, preserving the selected toolchain's
emulated TLS and private header overlay.

Windows publication verification keeps the JVM classifier `windows-x64` separate
from the Native engine module `gdal4k-engine-native-mingw-x64`. The reporter uses
the shared publication coordinate policy and checks the actual JAR/KLIB members
against the current producer classes, DLL and static archive.

For clients with small download limits, the primary and profile upload wrappers
also retain bounded audit parts and a final run/source/hash index. Follow
[bounded audit downloads](artifact-downloads.md) to reconstruct and verify all
selected bytes. The original artifacts remain available to normal CI consumers.

## Remaining acceptance

- Actual Windows kernel peak working-set collection, protected private ACLs,
  process identity and owned descendant timeout cleanup.
- Fresh normal default and combined producers for the current source, including
  the pinned GDAL/JXL engine, current DLL/static archive hashes and license texts.
- Source and independent published JVM and mingwX64 Native consumers with real
  scientific, Parquet IPC, HTTP/TLS/cache and PostGIS data operations. Independent
  Native consumers must hide and restore all three producer paths.
- Exact portable provider input/member/PE/dependency identities, actual server
  and extension versions, new-connection POINT readback, exact owned table
  deletion and terminal server/process/port cleanup. Package signature presence
  alone does not establish signature trust or successful Windows loading.
- Static-runtime standalone probes and their current executable/import receipts.
  Preserve the existing engine system-DLL policy.
- Three fresh combined JVMs and six controlled HTTP JVMs, sequentially, with
  current source/class/native/package bindings and complete command, stdout,
  kernel peak and request logs. Allocation, sampled heap and OS peak remain
  separate. Functional diagnostics do not establish a performance improvement.
- All current primary CI jobs and all applicable optional-profile jobs, their
  actual fixtures and assembled publication evidence. Recompute triggered
  workflows from the exact diff and current definitions. Verify real test
  execution and packaged bytes; green checks alone do not establish acceptance.

Inspect the actual failed command, log, annotation, XML or artifact before a fix
or a justified targeted retry. Do not blindly rerun CI, dispatch workflows,
increase timeouts, weaken TLS verification or substitute a system GDAL engine.

Ready and merge require a fresh fetched-main integration check, exact local and
remote HEAD/base, all current checks and workflow steps, paginated reviews and
threads, repository rules, a clean conflict state and the allowed merge method.
Any HEAD/base/main change invalidates prior approval. Merge only with the verified
expected head SHA, then fetch main and prove tested-head and merge ancestry.

The user's authorization covers implementation, separate PRs, full verification
and sequential merges toward the broader goal. The scheduled `gdal4k` automation
must remain **PAUSED** until the user explicitly resumes it. Progress JSON files
are optional notes; reconstruct state from Git, source, CI and actual evidence if
they are absent. Preserve all prior worktrees, branches, baselines and raw proof.

One existing follow-up is outside this Windows change: the POSIX certificate
generator still creates its self-signed untrusted endpoint with CA-only key
usage. Its rejection alone cannot isolate issuer trust from server usage. The
Windows generator now uses a valid self-signed server leaf and positive controls;
the corresponding POSIX control/fix needs a separate current-source assessment.
