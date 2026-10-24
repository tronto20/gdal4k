# libpq compatibility patches

This overlay retains PostgreSQL/libpq 18.4, its original archive SHA512,
features, dependencies, and all original port patches from vcpkg commit
`3cd21e48a75f67acfef289da52893ce79144aaa1`, port tree
`43e09f94e4ba985d3bdb29e258f9eca12e75c78c`. Port revision 2 adds
`popcount-sve-hwcap-compat.patch` to original revision 1. Revision 3 additionally
applies `windows/meson-canonical-generator-paths.patch` only when
`VCPKG_TARGET_IS_WINDOWS`. These are local compatibility fixes, not upstream
backports.

## Windows Perl generator paths

The combined Windows producer reached libpq's `lwlocknames_h` generator, then
Strawberry Perl could not open `generate-lwlocknames.pl`. From the logged Ninja
working directory, its unnormalized path contained 269 characters (excluding
NUL); the canonical path contained 219. Other Perl generator scripts used paths
of 227–257 characters and completed. The pinned PostgreSQL archive contains the
file, and Meson already registers it with `files(...)` and checks its existence
during configuration. Meson 1.9.0 retains the `src/include/storage/../../`
segments when emitting that File as a command argument. This evidence points to
the Windows Perl path boundary rather than a missing upstream generator; it is
not a Windows API trace or a complete inventory of the failed runner's files.

The Windows-only patch constructs the generator and its two data inputs from
`meson.project_source_root()` and paths without parent segments. All three stay
Meson File objects, retaining source existence checks, input/dependency tracking,
the same output directory and install target. It does not change the generator,
its data, compiler, Perl executable, dependencies, TLS settings, global Git or
Windows registry configuration. POSIX source selection retains the original
Meson file.

`verification/test_libpq_windows_paths.py` applies the patch to small fixtures
extracted from the exact SHA512-pinned PostgreSQL 18.4 archive, checks Windows
selection and File path behavior against pinned Meson 1.9.0 source, and executes
the original Perl generator with original and canonical paths on the test host.
The resulting header must be byte-identical. Those checks do not execute
Windows Perl or establish Windows engine/runtime acceptance; a fresh Windows
producer and the combined independent-consumer tests remain required.

## ARM SVE hardware-capability compatibility

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
