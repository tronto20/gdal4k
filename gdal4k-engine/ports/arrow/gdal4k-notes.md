# Arrow compatibility overlay

Arrow21.0.0 vcpkg port2 is pinned from vcpkg port tree73ec101affdc45cc2932cedf85bcd4bd4ed90c31. Archive SHA512 is retained. Arrow25.0.1 failed actual Linux CI because its C++20 std::span header is absent in Kotlin/Native's GCC8.3/glibc2.19 sysroot. This overlay retains the default toolchain/ABI and uses Arrow's C++17 release; no fake span header or alternate C++ runtime is introduced. Parquet's explicit default-features=false keeps mimalloc/Flight/cloud components disabled. Other profiles/default engines do not depend on Arrow.

Arrow21 falls in the affected range of CVE-2026-25087. The official fix from apache/arrow PR48925, commit8010794116fa9fb1ed020c649053411a7bd7f1bf, is backported in cve-2026-25087-cpp17.patch, including upstream reader and overflow tests. Production fixes retain variadic-buffer allocation before asynchronous reads, file/buffer bounds checks and dictionary-load fixes. Compatibility adaptations use existing21 integer-overflow overloads, C++17 static_assert instead of a concept, and existing21 default fuzz read options. The upstream CI-only script is not imported.

Primary references:
- https://security.apache.org/projects/arrow/#cve-2026-25087
- https://github.com/apache/arrow/pull/48925
- https://raw.githubusercontent.com/apache/arrow/apache-arrow-21.0.0/cpp/cmake_modules/SetupCxxFlags.cmake

The Parquet profile additionally executes variadic StringView IPC prebuffer/generator regression cases against its packaged archive. Passing source/consumer/IPC checks is mandatory; a source patch alone is not a completed target-support claim.
