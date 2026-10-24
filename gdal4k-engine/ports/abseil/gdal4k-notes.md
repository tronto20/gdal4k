# Preserve MinGW toolchain options

This overlay retains Abseil 20260107.1, its archive SHA512, dependencies and four
patches from vcpkg baseline `3cd21e48a75f67acfef289da52893ce79144aaa1`.
Imported text line endings follow this repository's LF policy.

The upstream MinGW workaround assigns `CMAKE_CXX_FLAGS` to one FIReference
definition. That replaces the selected toolchain's initialized flags, losing
emulated TLS and the private compiler-header overlay with this pinned SDK.

The same MinGW-only workaround is applied through an `absl` project include
using `add_compile_definitions`. It preserves compiler flags and common CMake
project hooks. Library version, CPU requirements and SDK inputs remain unchanged.
