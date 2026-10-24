// White-box cache/registry check built and fingerprinted by the optional producer.
// Published API/runtime consumers remain separate mandatory gates.
#include "http-inputs.hpp"
#include <iostream>

int main(int argc, char** argv) {
    try {
        const bool tlsOnly = argc == 3 && std::string(argv[2]) == "--tls-only";
        g4k_http::require(argc == 2 || tlsOnly, "Expected owned fixture URL and optional --tls-only");
        auto input = g4k_http::mount(argv[1], CPLJSONObject());
        if (tlsOnly) g4k_http::require(g4k_http::Url(argv[1]).part(CURLUPART_SCHEME) == "https",
            "TLS control requires HTTPS");
        // Exercise actual GDAL plugin dispatch, including prefix removal, before
        // the direct cache checks below. Public consumers verify dataset use.
        const auto path = input->prefix + input->mainName;
        VSIStatBufL info{};
        const auto statResult = VSIStatL(path.c_str(), &info);
        input->check(); // Preserve the redacted native TLS failure from the VSI callback.
        g4k_http::require(statResult == 0 && info.st_size > 4 * 1024 * 1024,
            "HTTP VSI stat callback failed");
        auto* vsiFile = VSIFOpenL(path.c_str(), "rb");
        g4k_http::require(vsiFile != nullptr, "HTTP VSI open callback failed");
        std::vector<unsigned char> header(16);
        const auto headerBytes = VSIFReadL(header.data(), 1, header.size(), vsiFile);
        const auto closeResult = VSIFCloseL(vsiFile);
        input->check();
        g4k_http::require(headerBytes == header.size() && closeResult == 0, "HTTP VSI read or close callback failed");
        g4k_http::require(VSIFOpenL(path.c_str(), "r+b") == nullptr, "HTTP VSI accepted update access");
        if (tlsOnly) {
            g4k_http::require(header[0] == 'I' && header[1] == 'I' && header[2] == 42 && header[3] == 0,
                "Expected actual little-endian TIFF header");
            const auto prefix = input->prefix;
            input.reset();
            g4k_http::require(g4k_http::registry.count(prefix) == 0, "Closed HTTP mount retained in registry");
            std::cout << "{\"result\":\"passed\",\"mode\":\"tls-control\",\"fileBytes\":" << info.st_size <<
                ",\"headerBytes\":" << headerBytes <<
                ",\"tiffHeaderVerified\":true,\"vsiDispatchVerified\":true,\"registryReleaseVerified\":true}\n";
            return 0;
        }
        std::vector<unsigned char> original;
        uint64_t checksum = 0;
        size_t peak = 0;
        for (int file = 0; file < 3; ++file) {
            const auto name = "cache" + std::to_string(file) + ".tif";
            auto source = input->source(name);
            g4k_http::require(source && source->size > 4 * 1024 * 1024, "Expected actual multi-chunk fixture");
            std::vector<unsigned char> bytes(static_cast<size_t>(source->size));
            g4k_http::require(input->read(name, 0, bytes.data(), bytes.size()) == bytes.size(), "Incomplete HTTP file read");
            uint64_t current = 1125899906842597ULL;
            for (auto byte : bytes) current = current * 31 + byte;
            if (!file) { original = bytes; checksum = current; }
            else g4k_http::require(bytes == original && checksum == current, "Related HTTP bytes differ");
            size_t sum = 0;
            for (const auto& chunk : input->cache) {
                g4k_http::require(chunk.bytes.size() <= g4k_http::chunkSize, "Oversized HTTP cache chunk");
                sum += chunk.bytes.size();
            }
            g4k_http::require(sum == input->cachedBytes && sum <= g4k_http::cacheLimit, "HTTP cache exceeds bound");
            peak = std::max(peak, sum);
        }
        g4k_http::require(std::equal(header.begin(), header.end(), original.begin()), "HTTP VSI header differs from fixture");
        const bool evicted = std::none_of(input->cache.begin(), input->cache.end(), [](const auto& chunk) { return chunk.name == "cache0.tif" && chunk.offset == 0; });
        g4k_http::require(evicted, "Expected actual LRU eviction after reading three files");
        std::vector<unsigned char> first(g4k_http::chunkSize);
        g4k_http::require(input->read("cache0.tif", 0, first.data(), first.size()) == first.size() &&
            std::equal(first.begin(), first.end(), original.begin()), "Evicted chunk reload differs");
        const auto prefix = input->prefix;
        input.reset();
        g4k_http::require(g4k_http::registry.count(prefix) == 0, "Closed HTTP mount retained in registry");
        std::cout << "{\"result\":\"passed\",\"files\":3,\"peakCachedBytes\":" << peak <<
            ",\"cacheLimitBytes\":" << g4k_http::cacheLimit << ",\"chunkLimitBytes\":" << g4k_http::chunkSize <<
            ",\"fileBytes\":" << original.size() << ",\"checksum\":\"" << checksum <<
            "\",\"vsiDispatchVerified\":true,\"evictionReloadAndRegistryReleaseVerified\":true}\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
