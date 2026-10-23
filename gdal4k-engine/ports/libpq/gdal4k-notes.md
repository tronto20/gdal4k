# libpq ARM SVE hardware-capability compatibility

This overlay retains PostgreSQL/libpq 18.4, its original archive SHA512,
features, dependencies, and all original port patches from vcpkg commit
`3cd21e48a75f67acfef289da52893ce79144aaa1`, port tree
`43e09f94e4ba985d3bdb29e258f9eca12e75c78c`. Port revision 2 adds only
`popcount-sve-hwcap-compat.patch` to original revision 1. This is a local
compatibility fix, not an upstream backport.

The actual Linux ARM64 cross producer failed in `pg_popcount_aarch64.c`:
Kotlin/Native clang 21 can compile SVE intrinsics and the pinned glibc provides
`getauxval`, while the pinned kernel 4.9 target headers do not define
`HWCAP_SVE`. Meson's successful intrinsic/function checks therefore do not prove
that the runtime hardware-capability macro is available.

The patch requires `HWCAP_SVE` alongside `HAVE_ELF_AUX_INFO` or `HAVE_GETAUXVAL`
inside `pg_popcount_sve_available`. When the target headers lack that macro,
the existing `false` branch leaves the existing NEON popcount implementation
selected. When the macro is available, the original hardware detection and SVE
selection are unchanged. SVE code remains compiled when upstream enables it.
No capability bit is invented, and no compiler, sysroot, dependency version,
system header, or global SVE setting is changed.

Patch applicability and isolated macro-branch regression checks precede fresh
normal producer and actual Linux ARM64 runtime verification. Those checks alone
are not engine or runtime acceptance; the failed producer remains evidence of
the original defect.
