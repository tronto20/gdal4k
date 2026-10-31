# SDK cache identities

Optional profiles keep independent source, install, CMake and native output
directories. Their snapshot names include source and live fixture identities.
Those names must not become a vcpkg ABI input when SDK contents are unchanged.

The generated triplet includes its pinned base through `VCPKG_ROOT_DIR` and
finds its generated chainload file through `CMAKE_CURRENT_LIST_DIR`. Native
toolchains also locate their private renamed ranlib and Windows header overlay
through their own directory. Compiler, sysroot, target and existing flags stay
in the generated toolchain. No install prefix is shared.

`sdk-toolchain-inputs.json` binds the included SDK framework/triplets, actual
generated toolchain/header contents, selected native archiver contents,
bootstrap tool pins, generator sources and target settings. The triplet appends
this file to `VCPKG_HASH_ADDITIONAL_FILES`; existing additional inputs remain.
The pinned vcpkg hashes the file contents, not its producer-local absolute path.
Tracked vcpkg sources must match their pinned clean revision before reuse.

`sdk-source-inputs.json` separately records overlay ports, feature manifests,
builder/bootstrap sources and the vcpkg source revision. It is evidence, not a
global port ABI stamp. The pinned vcpkg continues to hash each port's files,
resolved feature names and dependency package ABIs. A GDAL patch therefore
changes the relevant port input without forcing every unrelated dependency ABI
to change. Framework, included base, toolchain, header or compiler changes still
invalidate their corresponding configuration/ABI inputs.

Source/configuration reads and manifests have explicit size/count limits. Native
archiver contents are streamed under a separate 128 MiB per-tool bound; a stale
private renamed copy is rejected before installation. No compiler tracking or
public ABI override is disabled or introduced.

Metadata counterfactuals exercise different roots and fixture identities,
selective port/features/dependency changes, real configuration changes and
failure boundaries without executing an SDK installer or compiler. Real CMake
parser checks use exact pinned base-triplet text and generated CMake/JSON, with
no native build. Linux-host syntax/path checks do not establish Windows,
macOS or ARM SDK ABI or cache-hit behavior. Those require actual later platform
CI. This change makes no measured speedup claim.
