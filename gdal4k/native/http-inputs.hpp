#pragma once
// Optional native HTTP backend. No CURL headers or dependencies in default engines.
#include <cpl_vsi.h>
#include <cpl_json.h>
#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <climits>
#include <cctype>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <cstring>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "progress.hpp"
#ifdef GDAL4K_ENABLE_HTTP
#include <curl/curl.h>
#endif

namespace g4k_http {
inline void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
inline bool isUrl(const std::string& source) {
    auto prefix = source.substr(0, 8);
    std::transform(prefix.begin(), prefix.end(), prefix.begin(), [](unsigned char c) { return std::tolower(c); });
    return prefix.rfind("http://", 0) == 0 || prefix.rfind("https://", 0) == 0;
}
inline bool available() {
#ifdef GDAL4K_ENABLE_HTTP
    return true;
#else
    return false;
#endif
}

#ifdef GDAL4K_ENABLE_HTTP
// Only exact pinned static failures become public enum labels. Compare the whole
// freshly zeroed buffer, including the terminator/tail; never expose its contents.
inline const char* schannelTrustReason(CURLcode code, const char (&error)[CURL_ERROR_SIZE]) {
    if (code != CURLE_PEER_FAILED_VERIFICATION) return "unavailable";
    static constexpr char known0[CURL_ERROR_SIZE] = "schannel: trust for this certificate or one of the certificates in the certificate chain has been revoked";
    static constexpr char known1[CURL_ERROR_SIZE] = "schannel: the certificate chain is incomplete";
    static constexpr char known2[CURL_ERROR_SIZE] = "schannel: the certificate or certificate chain is based on an untrusted root";
    static constexpr char known3[CURL_ERROR_SIZE] = "schannel: this certificate or one of the certificates in the certificate chain is not time valid";
    static constexpr char known4[CURL_ERROR_SIZE] = "schannel: the revocation status is unknown";
    if (std::memcmp(error, known0, CURL_ERROR_SIZE) == 0) return "revoked";
    if (std::memcmp(error, known1, CURL_ERROR_SIZE) == 0) return "incomplete-chain";
    if (std::memcmp(error, known2, CURL_ERROR_SIZE) == 0) return "untrusted-root";
    if (std::memcmp(error, known3, CURL_ERROR_SIZE) == 0) return "not-time-valid";
    if (std::memcmp(error, known4, CURL_ERROR_SIZE) == 0) return "revocation-status-unknown";
    return "unavailable";
}

constexpr uint64_t maxOffset = 9007199254740991ULL;
constexpr size_t chunkSize = 64 * 1024, cacheLimit = 8 * 1024 * 1024;
using Clock = std::chrono::steady_clock;
struct Missing {};
struct Policy {
    int timeout = 30000, retries = 0, delay = 200, maxDelay = 3000;
    bool strong = false;
    explicit Policy(const CPLJSONObject& object) {
        if (!object.IsValid() || object.GetType() == CPLJSONObject::Type::Null) return;
        require(object.GetType() == CPLJSONObject::Type::Object, "Invalid HTTP input policy");
        const std::set<std::string> known{"requestTimeoutMillis", "maxRetries", "retryDelayMillis", "maxRetryDelayMillis", "requireStrongETag"};
        for (const auto& item : object.GetChildren()) require(known.count(item.GetName()) != 0, "Unknown HTTP input policy option");
        auto integer = [&](const char* key, int fallback) {
            auto item = object[key]; if (!item.IsValid()) return fallback;
            require(item.GetType() == CPLJSONObject::Type::Integer || item.GetType() == CPLJSONObject::Type::Long, "Invalid HTTP input policy integer");
            const auto number = item.ToLong(); require(number >= 0 && number <= INT_MAX, "Invalid HTTP input policy integer");
            return static_cast<int>(number);
        };
        timeout = integer("requestTimeoutMillis", timeout); retries = integer("maxRetries", retries);
        delay = integer("retryDelayMillis", delay); maxDelay = integer("maxRetryDelayMillis", maxDelay);
        require(timeout > 0 && maxDelay >= delay, "Invalid HTTP retry policy");
        if (object["requireStrongETag"].IsValid()) {
            require(object["requireStrongETag"].GetType() == CPLJSONObject::Type::Boolean, "Invalid HTTP validator policy");
            strong = object.GetBool("requireStrongETag");
        }
    }
};
inline std::string trim(std::string text) {
    const auto begin = text.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    return text.substr(begin, text.find_last_not_of(" \t\r\n") - begin + 1);
}
inline uint64_t number(const std::string& text) {
    uint64_t result = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    require(!text.empty() && parsed.ec == std::errc() && parsed.ptr == text.data() + text.size() && result <= maxOffset, "Invalid HTTP range or length");
    return result;
}
inline bool validName(const std::string& name) {
    return !name.empty() && name != "." && name != ".." && name.find_first_of("/\\:") == std::string::npos &&
        std::none_of(name.begin(), name.end(), [](unsigned char c) { return c < 32 || c == 127; });
}
inline bool strongTag(const std::string& value) {
    return value.size() >= 2 && value.front() == '"' && value.back() == '"' &&
        std::none_of(value.begin() + 1, value.end() - 1, [](unsigned char c) { return c < 33 || c == 127 || c == '"'; });
}
struct Url {
    std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> handle{curl_url(), curl_url_cleanup};
    explicit Url(const std::string& url) {
        require(handle != nullptr, "Unable to allocate HTTP URL");
        if (url.find('\0') != std::string::npos || curl_url_set(handle.get(), CURLUPART_URL, url.c_str(), CURLU_DISALLOW_USER) != CURLUE_OK) {
            throw std::runtime_error("Invalid HTTP(S) input URL (userinfo is unsupported)");
        }
        const auto scheme = part(CURLUPART_SCHEME);
        if (scheme != "http" && scheme != "https") {
            throw std::runtime_error("HTTP input requires HTTP(S)");
        }
    }
    std::string part(CURLUPart part, unsigned flags = 0) const {
        char* value = nullptr;
        if (curl_url_get(handle.get(), part, &value, flags) != CURLUE_OK) return {};
        std::unique_ptr<char, decltype(&curl_free)> owner(value, curl_free);
        return value;
    }
};
struct Source {
    std::string url, validatorKey, validator;
    uint64_t size = 0;
    bool initialized = false, poisoned = false;
};
struct Response {
    long status = 0;
    uint64_t first, last, total = 0;
    size_t headerBytes = 0;
    bool finishedHeaders = false, invalid = false, identity = false;
    std::string message;
    std::map<std::string, std::string> headers;
    std::vector<unsigned char> body;
    const Source& source;
    const Policy& policy;
    Response(uint64_t first, uint64_t last, const Source& source, const Policy& policy)
        : first(first), last(last), source(source), policy(policy) { body.reserve(static_cast<size_t>(last - first + 1)); }
    void validate() {
        require(status == 206, "HTTP input requires a partial 206 response");
        auto range = headers["content-range"];
        require(range.rfind("bytes ", 0) == 0, "Invalid HTTP Content-Range");
        const auto dash = range.find('-', 6), slash = range.find('/', 6);
        require(dash != std::string::npos && slash != std::string::npos && dash < slash, "Invalid HTTP Content-Range");
        const auto a = number(range.substr(6, dash - 6)), b = number(range.substr(dash + 1, slash - dash - 1));
        total = number(range.substr(slash + 1));
        require(a == first && b == last && total > b, "HTTP range or size changed; close and reopen input");
        if (source.initialized) {
            require(total == source.size, "HTTP input size changed; close and reopen input");
            require(source.validatorKey.empty() || headers[source.validatorKey] == source.validator, "HTTP input validator changed; close and reopen input");
        }
        // Validators are checked before accepting any body bytes.
        auto etag = headers["etag"];
        require(!policy.strong || strongTag(etag), "HTTP input requires a strong ETag");
        auto encoding = headers["content-encoding"];
        std::transform(encoding.begin(), encoding.end(), encoding.begin(), [](unsigned char c) { return std::tolower(c); });
        require(encoding.empty() || encoding == "identity", "HTTP ranges require identity encoding");
        if (headers.count("content-length")) require(number(headers["content-length"]) == last - first + 1, "Invalid HTTP Content-Length");
    }
};
inline size_t receiveHeader(char* data, size_t size, size_t count, void* value) noexcept {
    auto& response = *static_cast<Response*>(value);
    try {
        if (size && count > SIZE_MAX / size) return 0;
        const size_t length = size * count;
        response.headerBytes += length;
        require(response.headerBytes <= 65536, "HTTP response headers exceed limit");
        std::string line(data, length);
        if (line.rfind("HTTP/", 0) == 0) {
            response.headers.clear(); response.finishedHeaders = false;
            auto space = line.find(' ');
            require(space != std::string::npos && space + 4 <= line.size(), "Invalid HTTP status");
            response.status = static_cast<long>(number(line.substr(space + 1, 3)));
        } else if (line == "\r\n" || line == "\n") {
            if (response.status < 200) return length;
            response.finishedHeaders = true;
            if (response.status != 206) {
                response.identity = response.source.initialized && (response.status == 200 || response.status == 412 || response.status == 416);
                return 0; // Do not download error/redirect/full-file bodies.
            }
            try { response.validate(); }
            catch (...) { response.identity = response.source.initialized; throw; }
        } else {
            require(!response.finishedHeaders, "HTTP response trailers are unsupported");
            const auto colon = line.find(':'); require(colon != std::string::npos, "Invalid HTTP response header");
            auto key = line.substr(0, colon);
            std::transform(key.begin(), key.end(), key.begin(), [](unsigned char c) { return std::tolower(c); });
            if (key == "content-range" || key == "content-length" || key == "content-encoding" || key == "etag" || key == "last-modified" || key == "retry-after") {
                auto text = trim(line.substr(colon + 1));
                require(std::none_of(text.begin(), text.end(), [](unsigned char c) { return c < 32 || c == 127; }), "Invalid HTTP identity/range header");
                require(response.headers.emplace(key, std::move(text)).second, "Duplicate HTTP identity/range header");
            }
        }
        return length;
    } catch (const std::exception& error) {
        response.invalid = true;
        try { response.message = error.what(); } catch (...) {}
        return 0;
    } catch (...) { response.invalid = true; return 0; }
}
inline size_t receiveBody(char* data, size_t size, size_t count, void* value) noexcept {
    auto& response = *static_cast<Response*>(value);
    try {
        if (size && count > SIZE_MAX / size) return 0;
        const size_t length = size * count;
        require(response.finishedHeaders && response.status == 206, "Invalid HTTP body");
        require(length <= response.last - response.first + 1 - response.body.size(), "HTTP range body exceeds requested length");
        response.body.insert(response.body.end(), data, data + length);
        return length;
    } catch (...) { response.invalid = true; return 0; }
}
inline bool transientStatus(long status) { return status == 408 || status == 425 || status == 429 || status == 500 || status == 502 || status == 503 || status == 504; }
inline bool transientCurl(CURLcode code) {
    return code == CURLE_COULDNT_RESOLVE_PROXY || code == CURLE_COULDNT_RESOLVE_HOST || code == CURLE_COULDNT_CONNECT ||
        code == CURLE_RECV_ERROR || code == CURLE_SEND_ERROR || code == CURLE_GOT_NOTHING || code == CURLE_OPERATION_TIMEDOUT;
}
struct Mount;
inline std::mutex registryMutex;
inline std::map<std::string, std::weak_ptr<Mount>> registry;
inline std::atomic<unsigned long long> sequence{0};
struct Mount {
    Policy policy;
    std::string prefix, mainName, mainUrl;
    std::recursive_mutex mutex;
    std::map<std::string, std::shared_ptr<Source>> sources;
    std::set<std::string> missing;
    struct Cached { std::string name; uint64_t offset; std::vector<unsigned char> bytes; };
    std::list<Cached> cache;
    size_t cachedBytes = 0;
    std::string failure;
    std::atomic<gdal4k_control*> control{nullptr};
    Mount(const std::string& url, const CPLJSONObject& options) : policy(options), mainUrl(url) {
        static std::once_flag initialized;
        std::call_once(initialized, [] { require(curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK, "Unable to initialize HTTP backend"); });
        require((curl_version_info(CURLVERSION_NOW)->features & CURL_VERSION_ASYNCHDNS) != 0, "HTTP backend requires cancellable asynchronous DNS");
        Url parsed(url);
        auto path = parsed.part(CURLUPART_PATH);
        auto component = path.substr(path.find_last_of('/') + 1);
        require(component.size() <= INT_MAX, "HTTP input file name is too long");
        int length = 0;
        char* decoded = curl_easy_unescape(nullptr, component.c_str(), static_cast<int>(component.size()), &length);
        require(decoded != nullptr, "Unable to decode HTTP file name");
        std::unique_ptr<char, decltype(&curl_free)> owner(decoded, curl_free);
        mainName.assign(decoded, static_cast<size_t>(length));
        if (mainName.empty()) mainName = "input";
        require(validName(mainName), "Invalid HTTP input file name");
        prefix = "/vsigdal4khttp/" + std::to_string(++sequence) + "/";
        control.store(g4k::activeProgress);
    }
    ~Mount() { std::lock_guard<std::mutex> lock(registryMutex); registry.erase(prefix); }
    void checkCancelled() const {
        const auto token = control.load();
        require(!token || !token->cancelled.load(), "GDAL operation cancelled");
    }
    void record(const char* message) {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (failure.empty()) failure = message;
    }
    void begin(gdal4k_control* token) { std::lock_guard<std::recursive_mutex> lock(mutex); failure.clear(); control.store(token); }
    void check() { std::lock_guard<std::recursive_mutex> lock(mutex); if (!failure.empty()) throw std::runtime_error(failure); }
    std::vector<unsigned char> fetch(Source& source, uint64_t first, uint64_t last) {
        require(!source.poisoned, "HTTP input changed; close and reopen input");
        uint64_t delay = policy.delay;
        for (uint64_t attempt = 0;; ++attempt) {
            checkCancelled();
            Response response(first, last, source, policy);
            char errorBuffer[CURL_ERROR_SIZE] = {}; // Outlives easy/multi cleanup.
            std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> easy(curl_easy_init(), curl_easy_cleanup);
            std::unique_ptr<CURLM, decltype(&curl_multi_cleanup)> multi(curl_multi_init(), curl_multi_cleanup);
            require(easy && multi, "Unable to allocate HTTP request");
            auto set = [&](CURLoption option, auto value) { require(curl_easy_setopt(easy.get(), option, value) == CURLE_OK, "Unable to configure HTTP request"); };
            set(CURLOPT_ERRORBUFFER, errorBuffer); // Raw contents stay in memory only.
            set(CURLOPT_URL, source.url.c_str()); set(CURLOPT_PROTOCOLS_STR, "http,https");
            set(CURLOPT_FOLLOWLOCATION, 0L); set(CURLOPT_NETRC, static_cast<long>(CURL_NETRC_IGNORED));
            set(CURLOPT_SUPPRESS_CONNECT_HEADERS, 1L);
            set(CURLOPT_SSL_VERIFYPEER, 1L); set(CURLOPT_SSL_VERIFYHOST, 2L);
#ifdef __APPLE__
            set(CURLOPT_SSL_OPTIONS, static_cast<long>(CURLSSLOPT_NATIVE_CA));
#endif
            // Explicit child-process trust configuration, never a peer-verification bypass.
            const char* ca = std::getenv("CURL_CA_BUNDLE");
            if (!ca || !*ca) ca = std::getenv("SSL_CERT_FILE");
            if (ca && *ca) set(CURLOPT_CAINFO, ca);
            set(CURLOPT_TIMEOUT_MS, static_cast<long>(policy.timeout)); set(CURLOPT_CONNECTTIMEOUT_MS, static_cast<long>(policy.timeout));
            set(CURLOPT_NOSIGNAL, 1L); set(CURLOPT_HTTP_CONTENT_DECODING, 0L);
            set(CURLOPT_WRITEFUNCTION, receiveBody); set(CURLOPT_WRITEDATA, &response);
            set(CURLOPT_HEADERFUNCTION, receiveHeader); set(CURLOPT_HEADERDATA, &response);
            std::string range = "Range: bytes=" + std::to_string(first) + "-" + std::to_string(last);
            curl_slist* headers = nullptr;
            auto append = [&](const std::string& text) {
                auto result = curl_slist_append(headers, text.c_str());
                if (!result) { curl_slist_free_all(headers); headers = nullptr; throw std::bad_alloc(); }
                headers = result;
            };
            append("Accept-Encoding: identity"); append(range);
            if (source.initialized && !source.validator.empty()) append("If-Range: " + source.validator);
            std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headerOwner(headers, curl_slist_free_all);
            set(CURLOPT_HTTPHEADER, headers);
            require(curl_multi_add_handle(multi.get(), easy.get()) == CURLM_OK, "Unable to start HTTP request");
            struct Remove { CURLM* multi; CURL* easy; ~Remove() { curl_multi_remove_handle(multi, easy); } } remove{multi.get(), easy.get()};
            const auto deadline = Clock::now() + std::chrono::milliseconds(policy.timeout);
            int running = 1;
            bool timeout = false;
            while (running) {
                checkCancelled();
                if (Clock::now() >= deadline) { timeout = true; break; }
                require(curl_multi_perform(multi.get(), &running) == CURLM_OK, "HTTP transport failed");
                if (running) require(curl_multi_poll(multi.get(), nullptr, 0, 25, nullptr) == CURLM_OK, "HTTP transport wait failed");
            }
            CURLcode code = timeout ? CURLE_OPERATION_TIMEDOUT : CURLE_FAILED_INIT;
            if (!timeout) {
                int remaining = 0;
                while (auto* message = curl_multi_info_read(multi.get(), &remaining))
                    if (message->msg == CURLMSG_DONE && message->easy_handle == easy.get()) code = message->data.result;
            }
            checkCancelled();
            if (response.identity) source.poisoned = true;
            require(!response.identity, "HTTP input identity or range changed; close and reopen input");
            if (response.status == 404) throw Missing{};
            require(!response.invalid, response.message.empty() ? "Invalid HTTP range response" : response.message.c_str());
            if (code == CURLE_OK && response.status == 206) {
                require(response.finishedHeaders && response.body.size() == last - first + 1, "HTTP range body is truncated");
                if (!source.initialized) {
                    source.size = response.total;
                    auto etag = response.headers["etag"];
                    if (strongTag(etag)) { source.validatorKey = "etag"; source.validator = etag; }
                    else {
                        auto modified = response.headers["last-modified"];
                        if (curl_getdate(modified.c_str(), nullptr) >= 0) { source.validatorKey = "last-modified"; source.validator = modified; }
                    }
                    source.initialized = true;
                }
                return std::move(response.body);
            }
            const bool retry = transientStatus(response.status) || (response.status == 0 && transientCurl(code)) ||
                (response.status == 206 && transientCurl(code));
            if (!(retry && attempt < static_cast<uint64_t>(policy.retries))) {
                long tlsVerify = 0;
                // Schannel does not populate this field; zero is not verification evidence.
                const CURLcode tlsQuery = curl_easy_getinfo(easy.get(), CURLINFO_SSL_VERIFYRESULT, &tlsVerify);
                const std::string tls = tlsQuery == CURLE_OK ? std::to_string(tlsVerify) :
                    "unavailable(getinfo=" + std::to_string(static_cast<int>(tlsQuery)) + ")";
                throw std::runtime_error("HTTP range request failed (status/transport/timeout); no full-file fallback [curl=" +
                    std::to_string(static_cast<int>(code)) + ", http=" + std::to_string(response.status) +
                    ", tlsVerify=" + tls + ", error=" + curl_easy_strerror(code) +
                    ", schannelReason=" + schannelTrustReason(code, errorBuffer) + "]");
            }
            uint64_t wait = delay;
            const auto retryAfter = response.headers["retry-after"];
            if (!retryAfter.empty()) {
                uint64_t seconds = 0;
                const auto parsed = std::from_chars(retryAfter.data(), retryAfter.data() + retryAfter.size(), seconds);
                if (parsed.ec == std::errc() && parsed.ptr == retryAfter.data() + retryAfter.size())
                    wait = seconds > static_cast<uint64_t>(policy.maxDelay) / 1000 ? policy.maxDelay : seconds * 1000;
                else {
                    const auto date = curl_getdate(retryAfter.c_str(), nullptr), now = std::time(nullptr);
                    if (date >= now) wait = static_cast<uint64_t>(std::min<double>(policy.maxDelay, std::difftime(date, now) * 1000));
                }
            }
            wait = std::min<uint64_t>(policy.maxDelay, wait);
            const auto until = Clock::now() + std::chrono::milliseconds(wait);
            while (Clock::now() < until) { checkCancelled(); std::this_thread::sleep_for(std::chrono::milliseconds(5)); }
            delay = std::min<uint64_t>(policy.maxDelay, delay * 2);
        }
    }
    std::shared_ptr<Source> source(const std::string& name) {
        // Drivers can reopen/stat the same path after a failed callback. Preserve
        // the enclosing operation's failure so those probes cannot restart retries.
        check();
        require(validName(name), "Invalid related HTTP file name");
        auto found = sources.find(name); if (found != sources.end()) return found->second;
        if (missing.count(name)) return nullptr;
        auto value = std::make_shared<Source>();
        if (name == mainName) value->url = mainUrl;
        else {
            Url url(mainUrl);
            char* encoded = curl_easy_escape(nullptr, name.c_str(), static_cast<int>(name.size()));
            require(encoded != nullptr, "Unable to encode HTTP file name");
            std::unique_ptr<char, decltype(&curl_free)> owner(encoded, curl_free);
            require(curl_url_set(url.handle.get(), CURLUPART_URL, encoded, 0) == CURLUE_OK, "Invalid related HTTP URL");
            value->url = url.part(CURLUPART_URL);
        }
        try { fetch(*value, 0, 0); }
        catch (const Missing&) { missing.insert(name); return nullptr; }
        sources.emplace(name, value); return value;
    }
    size_t read(const std::string& name, uint64_t offset, unsigned char* output, size_t requested) {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        checkCancelled();
        auto input = source(name); require(input != nullptr, "HTTP input is missing");
        require(!input->poisoned, "HTTP input changed; close and reopen input");
        const size_t count = offset >= input->size ? 0 : static_cast<size_t>(std::min<uint64_t>(requested, input->size - offset));
        size_t copied = 0;
        while (copied < count) {
            checkCancelled();
            const auto start = ((offset + copied) / chunkSize) * chunkSize;
            auto found = std::find_if(cache.begin(), cache.end(), [&](const Cached& item) { return item.name == name && item.offset == start; });
            if (found == cache.end()) {
                auto bytes = fetch(*input, start, std::min<uint64_t>(input->size, start + chunkSize) - 1);
                while (cachedBytes + bytes.size() > cacheLimit) { cachedBytes -= cache.back().bytes.size(); cache.pop_back(); }
                cachedBytes += bytes.size(); cache.push_front({name, start, std::move(bytes)}); found = cache.begin();
            } else cache.splice(cache.begin(), cache, found);
            const size_t local = static_cast<size_t>(offset + copied - start);
            const auto bytes = std::min(count - copied, cache.front().bytes.size() - local);
            std::memcpy(output + copied, cache.front().bytes.data() + local, bytes); copied += bytes;
        }
        return count;
    }
};
struct Input { std::shared_ptr<Mount> mount; std::string name; uint64_t position = 0; bool eof = false, error = false; };
inline std::pair<std::shared_ptr<Mount>, std::string> locate(const char* path) {
    std::string text = path ? path : "";
    const std::string handler = "/vsigdal4khttp/";
    // GDAL's plugin dispatcher strips the handler prefix before callbacks.
    if (text.compare(0, handler.size(), handler) == 0) text.erase(0, handler.size());
    auto slash = text.find('/');
    if (slash == std::string::npos) return {};
    const auto prefix = handler + text.substr(0, slash + 1);
    std::lock_guard<std::mutex> lock(registryMutex);
    auto found = registry.find(prefix);
    if (found == registry.end()) return {};
    return {found->second.lock(), text.substr(slash + 1)};
}
inline void* open(void*, const char* path, const char* mode) noexcept {
    std::shared_ptr<Mount> mount;
    try {
        auto located = locate(path); mount = located.first;
        if (!mount || !validName(located.second) || !mode || mode[0] != 'r' || std::strchr(mode, '+')) { errno = EACCES; return nullptr; }
        std::lock_guard<std::recursive_mutex> lock(mount->mutex);
        if (!mount->source(located.second)) { errno = ENOENT; return nullptr; }
        return new Input{mount, located.second};
    } catch (const std::exception& error) { if (mount) try { mount->record(error.what()); } catch (...) {} }
    catch (...) { if (mount) try { mount->record("HTTP input failed"); } catch (...) {} }
    errno = EIO; return nullptr;
}
inline int stat(void*, const char* path, VSIStatBufL* value, int) noexcept {
    std::shared_ptr<Mount> mount;
    try {
        auto located = locate(path); mount = located.first;
        if (!mount) { errno = ENOENT; return -1; }
        std::memset(value, 0, sizeof(*value));
        if (located.second.empty()) { value->st_mode = S_IFDIR | 0555; return 0; }
        std::lock_guard<std::recursive_mutex> lock(mount->mutex);
        if (!validName(located.second)) { errno = ENOENT; return -1; }
        auto source = mount->source(located.second); if (!source) { errno = ENOENT; return -1; }
        value->st_mode = S_IFREG | 0444; value->st_size = static_cast<vsi_l_offset>(source->size); return 0;
    } catch (const std::exception& error) { if (mount) try { mount->record(error.what()); } catch (...) {} }
    catch (...) { if (mount) try { mount->record("HTTP input stat failed"); } catch (...) {} }
    errno = EIO; return -1;
}
inline void install() {
    static std::once_flag once;
    std::call_once(once, [] {
        auto* callbacks = VSIAllocFilesystemPluginCallbacksStruct();
        callbacks->open = open; callbacks->stat = stat;
        callbacks->tell = [](void* p) { return static_cast<vsi_l_offset>(static_cast<Input*>(p)->position); };
        callbacks->seek = [](void* p, vsi_l_offset offset, int whence) {
            auto& input = *static_cast<Input*>(p);
            try {
                std::lock_guard<std::recursive_mutex> lock(input.mount->mutex);
                const auto delta = static_cast<int64_t>(offset);
                const int64_t base = whence == SEEK_SET ? 0 : whence == SEEK_CUR ? input.position :
                    whence == SEEK_END ? input.mount->source(input.name)->size : maxOffset + 1;
                require(base <= static_cast<int64_t>(maxOffset) && delta >= -base && (delta <= 0 || delta <= static_cast<int64_t>(maxOffset) - base), "Invalid HTTP seek");
                input.position = static_cast<uint64_t>(base + delta); input.eof = false; return 0;
            } catch (...) { errno = EINVAL; input.error = true; return -1; }
        };
        callbacks->read = [](void* p, void* output, size_t size, size_t count) -> size_t {
            auto& input = *static_cast<Input*>(p);
            if (!size || !count) return 0;
            try {
                require(count <= SIZE_MAX / size, "HTTP read size overflow");
                const auto bytes = input.mount->read(input.name, input.position, static_cast<unsigned char*>(output), size * count);
                input.position += bytes; input.eof = bytes < size * count; return bytes / size;
            } catch (const std::exception& error) { try { input.mount->record(error.what()); } catch (...) {} }
            catch (...) { try { input.mount->record("HTTP read failed"); } catch (...) {} }
            errno = EIO; input.error = true; return 0;
        };
        callbacks->eof = [](void* p) { return static_cast<Input*>(p)->eof ? 1 : 0; };
        callbacks->error = [](void* p) { return static_cast<Input*>(p)->error ? 1 : 0; };
        callbacks->clear_err = [](void* p) { auto& input = *static_cast<Input*>(p); input.eof = input.error = false; };
        callbacks->close = [](void* p) { delete static_cast<Input*>(p); return 0; };
        callbacks->flush = [](void*) { return 0; };
        // Unknown sibling list: drivers may probe related names, never enumerate remote directories.
        callbacks->read_dir = [](void*, const char*, int) -> char** { return nullptr; };
        callbacks->sibling_files = [](void*, const char*) -> char** { return nullptr; };
        const auto result = VSIInstallPluginHandler("/vsigdal4khttp/", callbacks);
        VSIFreeFilesystemPluginCallbacksStruct(callbacks);
        require(result == 0, "Unable to install HTTP input handler");
    });
}
inline std::shared_ptr<Mount> mount(const std::string& source, const CPLJSONObject& policy) {
    install();
    auto result = std::make_shared<Mount>(source, policy);
    { std::lock_guard<std::mutex> lock(registryMutex); registry.emplace(result->prefix, result); }
    return result;
}
#else
struct Mount {
    std::atomic<gdal4k_control*> control{nullptr};
    void begin(gdal4k_control*) {}
    void check() {}
};
#endif
} // namespace g4k_http
