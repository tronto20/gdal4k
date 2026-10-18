#include "gdal4k.h"
#include <gdal.h>
#include <gdal_utils.h>
#include <ogr_srs_api.h>
#include <cpl_conv.h>
#include <cpl_json.h>
#include <cpl_string.h>
#include <cpl_vsi.h>
#include <atomic>
#include <cmath>
#include <climits>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include "resources.h"
#include "progress.hpp"
#include "runtime.hpp"

#ifdef __EMSCRIPTEN__
void gdal4k_install_web_inputs();
void gdal4k_install_web_outputs();
#endif
namespace {
std::once_flag registered;
// GDAL keeps PROJ search paths globally. Serialize path changes and restore the host state.
std::mutex configurationMutex;
CPLJSONObject parse(const char *value) {
    CPLJSONDocument doc;
    g4k::require(value && doc.LoadMemory(value), "Invalid GDAL JSON request");
    return doc.GetRoot();
}
}

struct gdal4k_context {
    g4k::Runtime runtime;
    std::mutex mutex;
    std::string projData, gdalData;
};

static CPLJSONObject execute(gdal4k_context &ctx, const CPLJSONObject &request) {
    g4k::require(request.GetString("op") == "runtime", "Unknown GDAL operation");
    return ctx.runtime.execute(request.GetObj("args"));
}

extern "C" gdal4k_context *gdal4k_create(const char *proj, const char *data) {
    try {
        std::call_once(registered, [] {
            gdal4k_install_resources();
#ifdef __EMSCRIPTEN__
            gdal4k_install_web_inputs();
            gdal4k_install_web_outputs();
#endif
            // CPL's finder snapshots GDAL_DATA on its first use. Register
            // drivers with the bundled location already visible to the finder.
            CPLPushFinderLocation(gdal4k_data_directory());
            GDALAllRegister();
            CPLPopFinderLocation();
        });
        auto ctx = new gdal4k_context;
        if (proj) ctx->projData = proj;
        ctx->gdalData = data && *data ? data : gdal4k_data_directory();
        return ctx;
    } catch (...) { return nullptr; }
}
extern "C" char *gdal4k_execute(gdal4k_context *ctx, const char *request) {
    return gdal4k_execute_controlled(ctx, request, nullptr);
}
extern "C" char *gdal4k_execute_controlled(gdal4k_context *ctx, const char *request, gdal4k_control* control) {
    if (!ctx) return CPLStrdup("{\"error\":\"GDAL session is closed\"}");
    std::lock_guard<std::mutex> configLock(configurationMutex);
    std::lock_guard<std::mutex> lock(ctx->mutex);
    // Most options are thread-local, but GDAL PROJ search paths require the explicit OSR API.
    char **previousPaths = ctx->projData.empty() ? nullptr : OSRGetPROJSearchPaths();
    if (!ctx->projData.empty()) {
        const char *paths[] = {ctx->projData.c_str(), nullptr};
        OSRSetPROJSearchPaths(paths);
    }
    char **previous = CPLGetThreadLocalConfigOptions();
    if (!ctx->projData.empty()) CPLSetThreadLocalConfigOption("PROJ_DATA", ctx->projData.c_str());
    if (!ctx->gdalData.empty()) CPLSetThreadLocalConfigOption("GDAL_DATA", ctx->gdalData.c_str());
    if (!ctx->gdalData.empty()) CPLPushFinderLocation(ctx->gdalData.c_str());
    CPLErrorReset();
    CPLJSONObject response;
    ctx->runtime.releasedDatasets.clear();
    const auto firstHandle = ctx->runtime.next;
    struct ProgressScope {
        gdal4k_control* previous;
        explicit ProgressScope(gdal4k_control* value) : previous(g4k::activeProgress) { g4k::activeProgress = value; }
        ~ProgressScope() { g4k::activeProgress = previous; }
    } progressScope(control);
    try {
        g4k::require(g4k::progress(0, "", control), "GDAL operation cancelled");
        response.Add("result", execute(*ctx, parse(request)));
        g4k::progress(1, "", control);
    }
    catch (const std::exception &e) { response.Add("error", e.what()); }
    catch (...) { response.Add("error", "Unknown GDAL error"); }
    CPLJSONArray released;
    for(auto id:ctx->runtime.releasedDatasets)released.Add(std::to_string(id));
    response.Add("releasedDatasets",released);
    if (control) {
        // A late cancellation/callback error can arrive after native work completed. Let the
        // caller close every newly registered handle before propagating that error.
        CPLJSONArray allocated;
        for (const auto& entry : ctx->runtime.handles) if (entry.first > firstHandle) allocated.Add(std::to_string(entry.first));
        response.Add("allocatedHandles", allocated);
    }
    if (!ctx->gdalData.empty()) CPLPopFinderLocation();
    if (previousPaths) { OSRSetPROJSearchPaths(previousPaths); CSLDestroy(previousPaths); }
    CPLSetThreadLocalConfigOptions(previous);
    CSLDestroy(previous);
    return CPLStrdup(response.Format(CPLJSONObject::PrettyFormat::Plain).c_str());
}
extern "C" void gdal4k_free(char *s) { CPLFree(s); }
extern "C" gdal4k_control* gdal4k_control_create() { try { return new gdal4k_control; } catch (...) { return nullptr; } }
extern "C" void gdal4k_control_cancel(gdal4k_control* control) { if (control) control->cancelled.store(true); }
extern "C" char* gdal4k_control_snapshot(gdal4k_control* control) {
    if (!control) return nullptr;
    std::lock_guard<std::mutex> lock(control->mutex);
    CPLJSONObject value;
    value.Add("sequence", static_cast<GIntBig>(control->sequence)); value.Add("fraction", control->fraction); value.Add("message", control->message);
    return CPLStrdup(value.Format(CPLJSONObject::PrettyFormat::Plain).c_str());
}
extern "C" void gdal4k_control_destroy(gdal4k_control* control) { delete control; }
extern "C" void gdal4k_destroy(gdal4k_context *ctx) {
    std::lock_guard<std::mutex> lock(configurationMutex);
    delete ctx;
}

struct gdal4k_binary_result {
    char* json = nullptr;
    g4k::BinaryRaster buffers{};
    ~gdal4k_binary_result() { gdal4k_free(json); }
};
extern "C" int gdal4k_binary_version() { return 1; }
extern "C" gdal4k_binary_result* gdal4k_execute_binary(gdal4k_context* context,const char* request,const unsigned char* input,int size,gdal4k_control* control) {
    try {
        auto result=std::make_unique<gdal4k_binary_result>();
        auto args=parse(request).GetObj("args"); auto name=args.GetString("method"),type=args.GetString("type");
        g4k::require(size>=0 && (!size || input),"Invalid binary raster input");
        g4k::require((type=="Band"||type=="Dataset") && (name.rfind("readRaster_",0)==0 || name.rfind("writeRaster_",0)==0 || (type=="Band"&&(name.rfind("readBlock_",0)==0||name.rfind("writeBlock_",0)==0))),"Binary transport requires a raster I/O operation");
        result->buffers.input=input; result->buffers.length=static_cast<size_t>(size);
        struct Scope {g4k::BinaryRaster* previous; Scope(g4k::BinaryRaster* value):previous(g4k::activeBinary){g4k::activeBinary=value;}~Scope(){g4k::activeBinary=previous;}} scope(&result->buffers);
        result->json=gdal4k_execute_controlled(context,request,control);
        if(!result->json) return nullptr;
        g4k::require(result->buffers.output.size()<=INT_MAX,"Binary raster response exceeds addressable array size");
        return result.release();
    } catch(...) { return nullptr; }
}
extern "C" const char* gdal4k_binary_json(const gdal4k_binary_result* result) {return result?result->json:nullptr;}
extern "C" const unsigned char* gdal4k_binary_data(const gdal4k_binary_result* result) {return result&&!result->buffers.output.empty()?result->buffers.output.data():nullptr;}
extern "C" int gdal4k_binary_size(const gdal4k_binary_result* result) {return result?static_cast<int>(result->buffers.output.size()):0;}
extern "C" void gdal4k_binary_destroy(gdal4k_binary_result* result) {delete result;}
