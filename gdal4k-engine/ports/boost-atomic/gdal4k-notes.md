# Boost.Atomic Clang-MinGW configuration

This overlay retains Boost.Atomic 1.92.0, its archive SHA512, dependencies and
existing include patch from vcpkg baseline
`3cd21e48a75f67acfef289da52893ce79144aaa1`.

The additional patch is selected only for MinGW targets. It extends the existing
GNU Windows API-version definition to Clang targeting MinGW. The upstream
`_WIN32_WINNT=0x0A00` definition stays PUBLIC so downstream Boost.Thread sees the
required WaitOnAddress declarations. The existing Windows synchronization link
and all other compiler/platform branches remain unchanged. No global producer
compiler flag, SDK version or default engine dependency is added.

The producer's separate private MinGW header overlay keeps three legacy SSE3
declarations out of Clang's already-defined intrinsic surface. It does not enable
SSE3 globally or change the consumer's Kotlin/Native SDK.
