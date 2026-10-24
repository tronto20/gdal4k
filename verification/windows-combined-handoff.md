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

The pinned Windows x64 chainload toolchain explicitly identifies its CPU as
`AMD64`, matching the existing `x86_64-w64-windows-gnu` compiler target. CMake does
not infer a target processor when this toolchain sets `CMAKE_SYSTEM_NAME`;
Arrow 21 rejects the resulting empty processor before compiling its sources.

Native archive collection reads Ninja's expanded compilation database and selects
the exact shared-library output in its build directory. Long link commands can
use response files that Ninja removes after linking; reading unexpanded command
text loses the static dependency closure. The existing installed-prefix filter,
mandatory GDAL/PROJ/JXL archive checks and system-linker policy still apply.

Independent JVM consumers retain periodic full worker thread snapshots, including
lock ownership, alongside the agent source/JAR hashes and build receipt. The
vector-column roundtrip prints driver and operation boundaries so an unfinished
native read or close can be identified even when Gradle cannot finish its XML.
It repeats the complete GeoJSON/MEM/GPKG lifecycle ten times, including retained
column assertions and owned stream/layer/dataset/file cleanup on every trial.
The existing coroutine and eight-minute test-task limits remain unchanged. These
diagnostics address an observed Windows consumer hang; they do not establish its
cause or a functional fix without fresh execution evidence.

Bounded Windows source-test commands also attach the worker agent. If a traced
column read stops progressing for 60 seconds, the command observer collects
minimal native thread/module diagnostics from that worker in its owned Job
Object, bound to its kernel process identity and produced DLL hash. A read still
stalled at 180 seconds fails the owned command, allowing evidence collection when
the existing coroutine cancellation cannot interrupt a native call. The overall
command deadline remains in force. These diagnostics do not establish a fix for
the observed second GeoPackage batch read.

The GDAL overlay separates GeoPackage Arrow batch-ready and producer-resume
condition variables. Both keep the original mutex, predicate loops, helper
replacement, terminal publication and cancellation/join ordering. The source
archive, compiler and SDK stay pinned. This avoids one participant waiting on
the queue it has just signalled; it does not establish that the original GDAL
handshake violates the C++ condition-variable contract.

The mandatory Windows handoff probe copies the existing SDK's exact condition
object and changes only its external mutex-unlock symbol to a private hook.
Win32 events park the producer after unlock and before queue entry, forcing the
consumer's next read into that window. The shared-queue control must observe
the stalled state and then cancel and join both threads; separate queues must
deliver three rows, one row, repeated EOF and early cancellation. Original SDK
bytes stay untouched. Retain its build, object identity and owned execution
receipts under `verification/results/ci-commands-mingwX64/winpthread-handoff`.
The probe isolates the SDK mechanism. Acceptance still requires fresh actual
GDAL source and published consumers on all applicable platforms.

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
