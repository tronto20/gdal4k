# Producer build tools

These root-project plugins are implemented in the independent `build-logic` included build.
They provision tools; GDAL's existing CMake/vcpkg recipes still own compilation.
They do not depend on Komple and do not require a Gradle upgrade.

| Plugin ID | Extension | Task | Default store |
| --- | --- | --- | --- |
| `dev.tronto.buildtools.cmake` | `cmakeTool` | `prepareCmake` | `<Gradle User Home>/build-tools/cmake` |
| `dev.tronto.buildtools.ninja` | `ninjaTool` | `prepareNinja` | `<Gradle User Home>/build-tools/ninja` |
| `dev.tronto.buildtools.emscripten` | `emscriptenTool` | `prepareEmscripten` | `<Gradle User Home>/build-tools/emsdk` |
| `dev.tronto.buildtools.vcpkg` | `vcpkgTool` | `prepareVcpkg` | `<Gradle User Home>/build-tools/vcpkg` |

The default uses Gradle's actual User Home (`~/.gradle` unless `GRADLE_USER_HOME` or
`--gradle-user-home` selects another directory), shared across projects and worktrees.
Explicit relative directory overrides remain relative to the project root.
Each store contains `<version>/<host>` installations,
so versions and host architectures do not overwrite each other. CMake/Ninja archives are kept
in the tool's `downloads/` directory under their SHA-256. `clean` preserves these stores.
Applying a plugin or running `help` does not install anything.

Installation receipts check the configured revision, URLs and hashes. A conflicting tool
identity at the same version/host path is rejected without replacing it; select a separate
store for custom builds with the same version label. Installation/download file locks protect
concurrent preparation from separate projects. Gradle's task cache does not restore or clean
these tool stores.

GDAL dependency outputs remain project-local: each target owns `installed`,
`vcpkg-buildtrees`, `vcpkg-packages` and `cmake` below `gdal4k-engine/build/engine/<target>`.
Both the vcpkg command and CMake's manifest installation use those same private paths;
symlinked workspace directories are rejected. vcpkg's download and ABI-keyed binary caches
may be shared. Emscripten's version/host-specific sysroot cache stays with its SDK and uses
the pinned SDK's own multiprocess cache lock; project objects and link outputs stay local.

Existing project-local stores are not moved or deleted automatically. To migrate an idle
installation, move its complete verified store to the selected User Home and rerun preparation.
Older checkouts can use their existing directory override or a compatibility link to that store.
Do not relocate tools while a producer using them is running.

## Use

```sh
./gradlew prepareNativeBuildTools  # CMake + Ninja + vcpkg
./gradlew prepareWebBuildTools     # those three + Emscripten
./gradlew :gdal4k-engine:buildEngineMacosArm64  # prepares native tools automatically
./gradlew :gdal4k-engine:buildEngineWeb         # prepares web tools automatically
```

The aggregate tasks write `build/build-tools/native.json` / `web.json` containing explicit
child-process tool paths, resolving symlinks to avoid mixed logical/physical Emscripten paths. Engine Gradle tasks pass these to the existing Python producer.
Direct Python entry points / existing CI provisioning remain available; to use the prepared
environment explicitly, run:

```sh
python3 gdal4k-engine/tools/build-engine.py web --build-tools build/build-tools/web.json
```

Preparation is a dependency of a Gradle engine task, including when its engine archive can be
reused. It does not rebuild GDAL by itself. Consuming published gdal4k libraries does not run
these plugins or install producer tools.

## Kotlin/Native first build

Linux x64, Linux ARM64 and Windows MinGW engine tasks depend on the corresponding
`:gdal4k:compileKotlinLinuxX64`, `compileKotlinLinuxArm64` or `compileKotlinMingwX64` API task.
The API module has no engine/cinterop dependency, so Kotlin's Gradle plugin can initialize
its pinned compiler and target dependencies before the Python producer uses them.
Calling an engine task directly therefore does not require a separate manual Native compilation.
Web, Android and Apple engine tasks do not acquire this Linux/MinGW prerequisite.

Kotlin/Native remains managed by KGP, not by a duplicate installer. Its default storage remains
`~/.konan`. `-Pkonan.data.dir=/absolute/path` is also passed to the Python producer;
`-Pkotlin.native.home=/absolute/compiler/home` is passed when no data-dir property overrides it.
An explicit data-dir property takes priority over inherited `KONAN_HOME`/`KONAN_DATA_DIR`.
The four producer-tool directory settings do not relocate Kotlin/Native.
Direct Python builds continue to require an initialized Native toolchain; use the Gradle engine
task to get automatic initialization.

`python verification/check-toolchain-bootstrap.py` verifies task ordering and absence of a
cinterop cycle without building GDAL. Adding `--initialize-target linuxX64 --require-empty`
executes the actual prerequisites and compiler dependency check in a dedicated empty directory.
For this test only, an init script replaces the expensive engine action with a real toolchain
probe. No GDAL engine or runtime test result is fabricated.

## Change storage locations

Each tool can have an independent store, including a shared absolute path:

```kotlin
cmakeTool { directory.set(layout.projectDirectory.dir(".tools/cmake")) }
ninjaTool { directory.set(file("/my/shared/tools/ninja")) }
emscriptenTool { directory.set(layout.projectDirectory.dir(".tools/emsdk")) }
vcpkgTool { directory.set(layout.projectDirectory.dir(".tools/vcpkg")) }
```

For command-line or user `gradle.properties` configuration:

```properties
buildTools.cmake.directory=/my/shared/tools/cmake
buildTools.ninja.directory=/my/shared/tools/ninja
buildTools.emscripten.directory=/my/shared/tools/emsdk
buildTools.vcpkg.directory=/my/shared/tools/vcpkg
```

`installationDirectory` can override the final directory instead of the store. For an existing
installation use **localDirectory**, as below, so it is validated without being modified.

## Reuse an explicitly selected existing installation

No arbitrary compiler or tool is selected from PATH. Set the existing tool root explicitly:

```kotlin
cmakeTool { localDirectory.set(file("/opt/homebrew/opt/cmake")) }
ninjaTool { localDirectory.set(file("/opt/homebrew/bin")) }
emscriptenTool { localDirectory.set(file("/my/existing/emsdk")) }
vcpkgTool { localDirectory.set(file("/my/existing/vcpkg")) }
```

The equivalent properties are `buildTools.<tool>.localDirectory`. `buildTools.python` and
`buildTools.git` select existing Python and Git executables; the plugins do not install them.

CMake/Ninja must report the requested exact version. CMake supports the downloaded macOS
app layout and the usual `bin/cmake` local prefix. For other layouts set `executablePath`
(relative to the installation root), e.g. `buildTools.cmake.executablePath=bin/cmake`.
Existing vcpkg/emsdk checkouts must match the full pinned revision and have no modified
tracked files. Emscripten must already be installed/activated at the requested version.
Local mode does not bootstrap, activate, create receipts, download or remove files in that root.

Without localDirectory, tools are downloaded into their managed stores. These stores should
be dedicated to the plugins. An unrecognized, incomplete or modified existing installation
fails with an actionable error rather than being erased. A download/installation is staged,
validated and renamed into place under an OS file lock. A failed task removes only its own
new staging directory. Explicit local directories and SDKs owned by other builds are preserved.

## Pins, offline use and platform support

Pins and CMake/Ninja/vcpkg executable SHA-256 values are in
`build-logic/src/main/resources/dev/tronto/buildtools/versions.properties`.
CMake 4.4.4 and Ninja 1.13.2 are fetched from their official GitHub release assets.
vcpkg uses commit `3cd21e48a75f67acfef289da52893ce79144aaa1`, emsdk uses commit
`c0bb220cb6e6f4e0fabb6f6db9efd53390ef5e56` and installs Emscripten 4.0.23.
vcpkg's executable release is separately pinned to `2026-09-26`. Gradle downloads the official
host executable directly, verifies SHA-256, checks the checkout's expected release and the
executable's reported version, and installs it alongside the pinned ports. No vcpkg bootstrap
script or system curl/zip/unzip/tar command is invoked by preparation. This covers all six hosts,
including native Windows ARM64. A previous managed installation is reused without rewriting it
only if its original receipt, source revision and newly pinned executable checksum all match.
Emsdk's pinned install program fetches its own SDK payloads; the plugins do not claim to
independently hash every transitive Emscripten download.
Emsdk includes its private Node/Python runtimes; this does not install a global Node/npm/Python environment.

Versions, archive URLs, SHA-256 and Git revisions are configurable through the extension or
`buildTools.<tool>.version`, `.url`, `.sha256`, `.revision`. A new CMake/Ninja version needs
an explicit archive checksum; a new emsdk/vcpkg version needs an explicit full Git revision.
For a different vcpkg checkout also set `binaryVersion`, `binaryDownloadUrl`, `binarySha256`
(or properties `.binaryVersion`, `.binaryUrl`, `.binarySha256`). `.url` continues to select the
Git source/mirror; `.binaryUrl` selects the executable/mirror. Custom binaries must match
both the configured SHA-256 and the checkout's `VCPKG_TOOL_RELEASE_TAG`.
The GDAL recipe separately enforces its vcpkg pin. Changing a generic tool plugin's version
is not a GDAL version upgrade.

`--offline` reuses a complete verified installation or an explicit compatible local tool.
For CMake/Ninja it can also extract a previously downloaded verified archive. A missing
vcpkg/emsdk installation fails offline; Git fetch and emsdk install are never invoked offline.
Receipts validate identity and key executable hashes; Git installations additionally validate
HEAD and tracked sources. This is not a full hash audit of every transitive SDK file.

| Host | CMake | Ninja | vcpkg | Emscripten 4.0.23 |
| --- | --- | --- | --- | --- |
| macOS x64 / ARM64 | managed/local | managed/local | managed/local | managed/local |
| Linux glibc x64 / ARM64 | managed/local | managed/local | managed/local | managed/local |
| Windows x64 | managed/local | managed/local | managed/local | managed/local |
| Windows ARM64 | managed/local | managed/local | managed/local | unsupported upstream native payload |

The pinned emsdk manifest has no native Windows ARM64 release; preparation fails explicitly,
without substituting an x64 SDK. Using an x64 host process under emulation is a separate choice,
not native ARM64 validation. Unsupported OS/architecture combinations are rejected. Linux
musl/Alpine is outside the existing producer support policy.

## Developer prerequisites

Common source-build prerequisites are JDK (21 for the GDAL project; 17+ for the standalone
plugins), Git, Python 3.9+, network access for the first preparation and OS certificate trust.
Use the checked-in Gradle wrapper; a system Gradle is unnecessary.

| Host / target | What the developer still supplies |
| --- | --- |
| macOS | Xcode Command Line Tools / macOS SDK |
| iOS builds | Full Xcode and the corresponding Apple SDK; a simulator only for execution tests |
| Linux glibc x64/ARM64 | A normal glibc environment, `/bin/sh`, OS CA certificates and a libcurl runtime (`libcurl.so.4` or upstream's supported alternatives) |
| Windows x64/ARM64 | Supported Windows with its standard command environment and `tar.exe`; no Git Bash/MSYS2 installation is required by tool provisioning |
| Android builds | Android SDK/NDK for the requested target; an emulator/device only for execution tests |

CMake, Ninja, vcpkg and Emscripten are prepared by Gradle. Kotlin/Native downloads its own
compiler and target dependencies as described above. Generic native compiler smoke tests and
vcpkg **host tool** builds can still require a host C/C++ compiler: the GDAL manifest builds
host `pkgconf` and `sqlite3` tools, so having a managed *target* compiler alone does not prove
that all host compiler requirements disappear. No claim of a compiler-free full GDAL build
is made. The plugins do not install system packages, Xcode or Android SDKs.

For the Gradle producer environment, Linux/macOS vcpkg archive calls use private adapters in
`build/build-tools/vcpkg-archive-bin` (next to the generated environment JSON). They implement
only the pinned vcpkg `tar xzf`, ZIP extraction and binary-cache ZIP write/read calls using
managed CMake and Python. They preserve executable modes, symlinks, empty directories and
hidden children, handle pre-1980 source timestamps, and reject unsupported options. These are
not general-purpose system utilities. No system curl/zip/unzip/tar installation is required
for those paths; Linux libcurl is a shared-library runtime, not the `curl` command.
Windows vcpkg acquires its own 7-Zip and uses the OS `tar.exe`.
The generated child environment selects prepared CMake/Ninja via `VCPKG_FORCE_SYSTEM_BINARIES`;
it never changes the user's shell profile, system PATH or local vcpkg checkout.
Direct Python builds without `--build-tools` retain their existing upstream provisioning path.

Node/npm/Playwright/Chromium and Android/iOS execution environments are separate test
requirements, not prerequisites for the four preparation plugins. Available host tools do
not imply that every target can be cross-compiled from that host.

## Verify

```sh
./gradlew -p build-logic check
```

The tests exercise task-time installation, explicit local reuse, configurable stores,
configuration-cache reuse, checksums, interrupted/invalid installations, offline behavior,
archive path safety and concurrent processes. Installer fixtures are not GDAL runtime tests.
Cross-host smoke tests are defined separately in `.github/workflows/build-tools.yml`;
configured CI coverage is not a claim that those jobs have already passed.

The six-host CI also runs `build-logic/vcpkg-smoke.py`: a real pinned vcpkg downloads and
extracts ZIP/TAR inputs from an isolated local HTTP server, installs a small header-only
fixture package, writes a binary cache and restores it into a second install root. Trap
executables catch accidental system curl/zip/unzip/tar/Bash calls (Windows retains OS tar).
This is a provisioning/cache integration check, not a GDAL runtime test. Windows steps use
PowerShell and `gradlew.bat`, not Git Bash.
