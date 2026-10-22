# c-ares deployment-target availability backport

This overlay retains c-ares 1.34.8 and its original archive SHA512 from vcpkg
commit `3cd21e48a75f67acfef289da52893ce79144aaa1`, port tree
`028f0b728dc1149d00a1633c95ff1cc21efd795e`. The original `avoid-docs.patch`
and `usage` retain their contents; imported patch line endings follow this repository's LF policy. Port revision 1 adds only the patch below.

`apple-deployment-availability.patch` is the CMakeLists.txt portion of official
[c-ares commit eb1c63f7197c2b9c04e5f55b585663eb2e89db50](https://github.com/c-ares/c-ares/commit/eb1c63f7197c2b9c04e5f55b585663eb2e89db50)
([PR 1328](https://github.com/c-ares/c-ares/pull/1328)). The complete upstream
patch SHA256 is `f9a7289a298016b51a2d798bc977fa7155e2956f5d3e61f5485be99cd9dc2d2c`.
The Autotools change is omitted because this port builds with CMake. Version
1.34.8 already includes `CheckCCompilerFlag` through `cmake/EnableWarnings.cmake`.

The actual local macOS combined producer failed before tests: SDK 27 declares
`pipe2`, CMake detected `HAVE_PIPE2=1`, and compilation rejected the call because
`pipe2` is unavailable for the existing macOS 11 deployment target. The backport
checks compiler support for `-Werror=partial-availability` and applies it only to
Apple symbol detection, saving and restoring `CMAKE_REQUIRED_FLAGS`. Unavailable
functions then select the existing `pipe`/`fcntl` implementation. It does not
change the deployment target, threading, runtime source, or dependency version.

Patch applicability is checked against an owned copy of the retained source.
Fresh normal producer and runtime verification are required before acceptance;
the failed producer and its cached availability results are historical evidence.
