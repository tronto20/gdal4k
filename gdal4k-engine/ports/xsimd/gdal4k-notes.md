# xsimd old ARM target-header compatibility

This overlay retains xsimd 14.3.0, its original archive SHA512, xcomplex feature,
dependencies, and `fix-pkgconfig-xcomplex.patch` from vcpkg commit
`3cd21e48a75f67acfef289da52893ce79144aaa1`, port tree
`9e51eb747cfce6c337d5452e5ddaf3ca88e8cd50`. Port revision 1 adds only
`sve-hwcap-compat.patch`. This is a local compatibility fix, not an upstream
backport. The upstream master header inspected on 2026-10-23 (Git blob
`57f1c56e31d43e498d5494d87e373bc48ac5edb2`) still has the same unguarded check.

The actual Linux ARM64 combined cross producer compiled Arrow 21.0.0 with
Kotlin/Native clang 21 and the pinned kernel 4.9 target headers. xsimd's
`arm_cpu_features::sve()` unconditionally referenced `HWCAP_SVE` whenever the
target was ARM64 with Linux getauxval support, although those old headers lack
the macro. Arrow's `util/utf8.cc` and `util/bpacking_neon.cc` then failed while
including this header.

The one-condition patch requires `HWCAP_SVE` before using that runtime feature
mask. When absent, the existing false branch reports SVE unavailable. When
present, the original auxiliary-vector check is unchanged. NEON/NEON64,
i8mm, the SVE vector-length helper, non-ARM and non-Linux branches are unchanged.
No capability bit is invented, and no SIMD implementation, global SIMD option,
compiler, target headers, sysroot, dependency version or Arrow source is changed.

The regression retains the complete pinned header and its license, applies the
actual overlay patch to an owned temporary copy, and compiles the actual SVE
selector under old, modern and non-Linux/non-ARM macro environments. These are
host compile-only checks, not ARM CPU detection or GDAL/Arrow execution. A fresh
normal producer and actual matching-host tests remain mandatory.
