#pragma once
#include <atomic>
#include <algorithm>
#include <cmath>
#include <mutex>
#include <string>
#include <cpl_json.h>
#include <cpl_conv.h>
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
EM_ASYNC_JS(void, gdal4k_progress_yield, (double fraction, const char* message, double sequence), {
    Module.gdal4kProgress?.({fraction, message: UTF8ToString(message), sequence});
    await new Promise(resolve => setTimeout(resolve, 0));
});
#endif

struct gdal4k_control {
    std::atomic<bool> cancelled{false};
    std::mutex mutex;
    double fraction = 0;
    std::string message;
    unsigned long long sequence = 0;
    // Counts callbacks originating in GDALMultiDimTranslate, independently of
    // wrapper checkpoints and binary output transfers.
    unsigned long long multiDimTranslateProgressCalls = 0;
#ifdef __EMSCRIPTEN__
    double lastYield = -100;
#endif
};
namespace g4k {
inline thread_local gdal4k_control* activeProgress = nullptr;
inline int progress(double fraction, const char* message, void* data) {
    auto control = static_cast<gdal4k_control*>(data);
    if (!control) return TRUE;
    if (control->cancelled.load()) return FALSE;
    if (!std::isfinite(fraction)) fraction = 0;
    fraction = std::clamp(fraction, 0.0, 1.0);
    std::string text = message ? std::string(message).substr(0, 1024) : "";
    unsigned long long sequence;
    {
        std::lock_guard<std::mutex> lock(control->mutex);
        control->fraction = fraction; control->message = text;
        sequence = ++control->sequence;
    }
#ifdef __EMSCRIPTEN__
    const auto now = emscripten_get_now();
    if (fraction == 0 || fraction == 1 || now - control->lastYield >= 20) {
        control->lastYield = now;
        gdal4k_progress_yield(fraction, text.c_str(), static_cast<double>(sequence));
    }
#endif
    return !control->cancelled.load();
}
inline int multiDimTranslateProgress(double fraction, const char* message, void* data) {
    auto control = static_cast<gdal4k_control*>(data);
    if (control) {
        std::lock_guard<std::mutex> lock(control->mutex);
        ++control->multiDimTranslateProgressCalls;
    }
    return progress(fraction, message, data);
}
}
