// Included inside namespace g4k, after Runtime. Every operation uses its handle table.
inline void fileEnsure(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message + ": " + CPLGetLastErrorMsg());
}
inline void fileBasename(const std::string& name) {
    require(!name.empty() && name != "." && name != ".." && name.find_first_of("/\\:") == std::string::npos && name.find('\0') == std::string::npos, "Invalid file basename");
}
struct Directory {
    inline static std::atomic<unsigned long long> sequence{0};
    std::string path = "/vsimem/gdal4k-" + std::to_string(++sequence);
    Directory() { fileEnsure(VSIMkdir(path.c_str(), 0700) == 0, "Cannot create GDAL workspace"); }
    ~Directory() { if (!path.empty()) VSIRmdirRecursive(path.c_str()); }
};
struct FileOptions {
    char** list = nullptr;
    explicit FileOptions(const CPLJSONArray& array) { for (const auto& s : array) add(s.ToString()); }
    void add(const std::string& text) {
        require(text.find('\0') == std::string::npos, "NUL in GDAL argument");
        list = CSLAddString(list, text.c_str());
    }
    ~FileOptions() { CSLDestroy(list); }
};
struct OpenFile {
    VSILFILE* file;
    explicit OpenFile(const std::string& path): file(VSIFOpenL(path.c_str(), "wb")) {
        fileEnsure(file != nullptr, "Cannot create file");
    }
    ~OpenFile() { if (file) VSIFCloseL(file); }
    void finish() { auto closing = file; file = nullptr; fileEnsure(VSIFCloseL(closing) == 0, "Cannot close file"); }
};
inline void writeFileBytes(const std::string& path, const unsigned char* data, size_t size) {
    OpenFile output(path);
    for (size_t offset = 0; offset < size;) {
        binaryCheckpoint();
        auto count = std::min<size_t>(262144, size - offset);
        fileEnsure(VSIFWriteL(data + offset, 1, count, output.file) == count, "Cannot write file");
        offset += count;
    }
    output.finish();
}
inline GIntBig fileByteSize(const CPLJSONObject& object, const char* field) {
    const auto type = object[field].GetType();
    require(type == CPLJSONObject::Type::Integer || type == CPLJSONObject::Type::Long, "File byte size must be an integer");
    const auto size = object.GetLong(field, -1);
    require(size >= 0, "Invalid file byte size"); return size;
}
inline CPLJSONObject writeMemoryFile(const CPLJSONObject& args) {
    const auto path = args.GetString("path");
    require(path.rfind("/vsimem/", 0) == 0 && path.find('\0') == std::string::npos, "Memory file path must start with /vsimem/");
    if (activeBinary) {
        auto size = fileByteSize(args, "byteSize");
        require(size >= 0 && static_cast<uint64_t>(size) == activeBinary->length, "Invalid memory file input length");
        VSIMkdirRecursive(path.substr(0, path.find_last_of('/')).c_str(), 0700);
        writeFileBytes(path, activeBinary->input, activeBinary->length);
    } else {
        auto input = args.GetArray("bytes"); std::vector<GByte> data;
        data.reserve(input.Size());
        for (auto value : input) data.push_back(static_cast<GByte>(value.ToInteger()));
        VSIMkdirRecursive(path.substr(0, path.find_last_of('/')).c_str(), 0700);
        writeFileBytes(path, data.data(), data.size());
    }
    return nullValue();
}
inline CPLJSONObject readMemoryFile(const CPLJSONObject& args) {
    const auto path = args.GetString("path");
    require(path.rfind("/vsimem/", 0) == 0 && path.find('\0') == std::string::npos, "Memory file path must start with /vsimem/");
    if (activeBinary) require(activeBinary->length == 0, "Memory file reads cannot have an input payload");
    VSIStatBufL info;
    require(VSIStatL(path.c_str(), &info) == 0 && !VSI_ISDIR(info.st_mode), "Memory file does not exist");
    vsi_l_offset size = 0; auto data = VSIGetMemFileBuffer(path.c_str(), &size, FALSE);
    require((data || size == 0) && size <= static_cast<vsi_l_offset>(INT_MAX), "Invalid or oversized memory file");
    if (activeBinary) { binaryOutput(data, static_cast<size_t>(size)); return value(static_cast<int>(size)); }
    return numbers(data, size);
}
// GDALWarp's CreateCopy path (including COG) materializes a temporary GTiff.
// Without CPL_TMPDIR it can use the process cwd, which is read-only on Android.
// Use the output filesystem rather than staging a direct output in memory.
struct OutputTemporaryDirectory {
    char** previous = nullptr;
    bool changed = false;
    explicit OutputTemporaryDirectory(const std::string& path, bool enabled = true) {
        if (enabled && !CPLGetConfigOption("CPL_TMPDIR", nullptr)) {
            previous = CPLGetThreadLocalConfigOptions(); changed = true;
            auto directory = CPLGetPathSafe(path.c_str());
            CPLSetThreadLocalConfigOption("CPL_TMPDIR", directory.empty() ? "." : directory.c_str());
        }
    }
    ~OutputTemporaryDirectory() {
        if (changed) { CPLSetThreadLocalConfigOptions(previous); CSLDestroy(previous); }
    }
};
inline CPLJSONObject objectValue(const CPLJSONObject& object) {
    CPLJSONObject result; result.Add("value", object); return result;
}
inline CPLJSONObject driverList() {
    CPLJSONArray result;
    for (int i = 0; i < GDALGetDriverCount(); ++i) {
        auto driver = GDALGetDriver(i);
        CPLJSONObject item;
        item.Add("name", GDALGetDriverShortName(driver)); item.Add("longName", GDALGetDriverLongName(driver));
        item.Add("raster", GDALGetMetadataItem(driver, GDAL_DCAP_RASTER, nullptr) != nullptr);
        item.Add("vector", GDALGetMetadataItem(driver, GDAL_DCAP_VECTOR, nullptr) != nullptr);
        result.Add(item);
    }
    return value(result);
}
inline CPLJSONObject openMemoryDataset(Runtime& runtime, const CPLJSONObject& args) {
    Directory workspace;
    auto files = args.GetObj("files");
    auto main = files.GetString("main"); fileBasename(main);
    const auto entries = files.GetArray("files");
    std::set<std::string> names; size_t total = 0;
    for (const auto& file : entries) {
        auto name = file.GetString("name"); fileBasename(name);
        require(names.insert(name).second, "Duplicate input file name");
        if (activeBinary) {
            const auto count = fileByteSize(file, "size");
            require(count >= 0 && static_cast<uint64_t>(count) <= activeBinary->length - total, "Invalid file input length");
            total += static_cast<size_t>(count);
        }
    }
    require(names.count(main) != 0, "Missing main input file");
    if (activeBinary) require(total == activeBinary->length, "Trailing file input payload");
    size_t offset = 0;
    for (const auto& file : entries) {
        auto name = file.GetString("name"); fileBasename(name);
        auto path = workspace.path + "/" + name;
        if (activeBinary) {
            const auto count = static_cast<size_t>(file.GetLong("size"));
            writeFileBytes(path, count ? activeBinary->input + offset : nullptr, count);
            offset += count;
            continue;
        }
        auto base64 = file.GetString("data");
        std::vector<GByte> data(base64.begin(), base64.end()); data.push_back(0);
        int count = CPLBase64DecodeInPlace(data.data());
        require(count >= 0, "Invalid Base64 input");
        writeFileBytes(path, data.data(), static_cast<size_t>(count));
    }
    auto options = args.GetObj("options");
    FileOptions list(options.GetArray("options"));
    auto flags = GDAL_OF_VERBOSE_ERROR | (options.GetBool("update") ? GDAL_OF_UPDATE : GDAL_OF_READONLY) | options.GetInteger("kind");
    auto dataset = GDALOpenEx((workspace.path + "/" + main).c_str(), flags, nullptr, list.list, nullptr);
    fileEnsure(dataset != nullptr, "Cannot open dataset");
    auto result = runtime.result(dataset, "Dataset", true);
    auto id = std::stoull(result.GetString("value"));
    runtime.workspaces[id] = workspace.path;
    workspace.path.clear(); // Ownership transfers to the dataset handle, including runtime shutdown.
    return result;
}
inline std::string datasetInfo(GDALDatasetH dataset, const CPLJSONArray& arguments, bool json) {
    FileOptions options(arguments);
    if (json) options.add("-json");
    char* text = nullptr;
    if (GDALGetRasterCount(dataset) > 0) {
        auto opts = GDALInfoOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid raster info options");
        text = GDALInfo(dataset, opts); GDALInfoOptionsFree(opts);
    } else {
        if (json) { options.add("-so"); options.add("-al"); }
        auto opts = GDALVectorInfoOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid vector info options");
        text = GDALVectorInfo(dataset, opts); GDALVectorInfoOptionsFree(opts);
    }
    fileEnsure(text != nullptr, "Cannot inspect dataset");
    std::string result(text); CPLFree(text); return result;
}
inline CPLJSONObject outputFiles(const Directory& directory, const std::string& main) {
    CPLJSONArray files;
    char** names = VSIReadDir(directory.path.c_str());
    try {
        struct File { std::string name; const GByte* bytes; size_t size; };
        std::vector<File> entries; uint64_t total = 0;
        if (activeBinary) require(activeBinary->length == 0, "File output cannot have an input payload");
        bool found = false;
        for (int i = 0; names && names[i]; ++i) {
            const std::string name(names[i]);
            if (name == "." || name == "..") continue;
            fileBasename(name);
            vsi_l_offset size = 0;
            auto data = VSIGetMemFileBuffer((directory.path + "/" + name).c_str(), &size, FALSE);
            VSIStatBufL info;
            fileEnsure(VSIStatL((directory.path + "/" + name).c_str(), &info) == 0 && !VSI_ISDIR(info.st_mode) && (data || size == 0), "Output must be a flat set of files");
            require(size <= static_cast<vsi_l_offset>(INT_MAX), "Output file exceeds bridge size limit");
            entries.push_back({name, data, static_cast<size_t>(size)}); total += size;
            found = found || name == main;
        }
        require(found, "GDAL did not create the main output file");
        // Keep the legacy per-file limit when an aggregate exceeds one addressable frame.
        // The fallback is selected from the completed output; processing is never rerun.
        const bool binary = activeBinary && total <= static_cast<uint64_t>(INT_MAX);
        if (binary) activeBinary->output.resize(static_cast<size_t>(total));
        size_t offset = 0;
        for (const auto& entry : entries) {
            CPLJSONObject file;
            file.Add("name", entry.name);
            if (binary) {
                file.Add("size", static_cast<int>(entry.size));
                for (size_t position = 0; position < entry.size;) {
                    binaryCheckpoint();
                    const auto count = std::min<size_t>(262144, entry.size - position);
                    std::memcpy(activeBinary->output.data() + offset + position, entry.bytes + position, count);
                    position += count;
                }
                offset += entry.size;
            } else {
                char* encoded = CPLBase64Encode(static_cast<int>(entry.size), entry.bytes);
                fileEnsure(encoded != nullptr, "Cannot encode output file");
                file.Add("data", encoded); CPLFree(encoded);
            }
            files.Add(file);
        }
        CPLJSONObject result; result.Add("main", main); result.Add("files", files);
        if (binary) result.Add("binaryFiles", true);
        CSLDestroy(names); names = nullptr; return objectValue(result);
    } catch (...) { CSLDestroy(names); throw; }
}
inline void convertDatasetPath(GDALDatasetH dataset, const CPLJSONObject& args, const std::string& operation, const std::string& path) {
    require(!path.empty() && path.find('\0')==std::string::npos,"Invalid output path");
    FileOptions options(args.GetArray("arguments"));
    if (operation == "warp") { options.add("-t_srs"); options.add(args.GetString("target")); }
    options.add(operation == "vector" ? "-f" : "-of"); options.add(args.GetString("driver"));
    GDALDatasetH result = nullptr;
    int usageError = 0;
    OutputTemporaryDirectory temporary(path, operation == "warp");
    if (operation == "translate") {
        auto opts = GDALTranslateOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid translate options");
    GDALTranslateOptionsSetProgress(opts, progress, activeProgress);
        result = GDALTranslate(path.c_str(), dataset, opts, &usageError); GDALTranslateOptionsFree(opts);
    } else if (operation == "warp") {
        auto opts = GDALWarpAppOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid warp options");
    GDALWarpAppOptionsSetProgress(opts, progress, activeProgress);
        result = GDALWarp(path.c_str(), nullptr, 1, &dataset, opts, &usageError); GDALWarpAppOptionsFree(opts);
    } else {
        auto opts = GDALVectorTranslateOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid vector translate options");
    GDALVectorTranslateOptionsSetProgress(opts, progress, activeProgress);
        result = GDALVectorTranslate(path.c_str(), nullptr, 1, &dataset, opts, &usageError); GDALVectorTranslateOptionsFree(opts);
    }
    auto closeCode = result ? GDALClose(result) : CE_Failure;
    fileEnsure(result != nullptr && closeCode == CE_None && !usageError, "GDAL conversion failed");

}

inline void convertDatasetTo(GDALDatasetH dataset,const CPLJSONObject& args,const std::string& operation) {
    convertDatasetPath(dataset,args,operation,args.GetString("path"));
}
inline CPLJSONObject convertDataset(GDALDatasetH dataset,const CPLJSONObject& args,const std::string& operation) {
    Directory output;auto name=args.GetString("name");fileBasename(name);
    convertDatasetPath(dataset,args,operation,output.path+"/"+name);
    return outputFiles(output,name);
}

inline void demProcessingPath(GDALDatasetH source, const CPLJSONObject& args, const std::string& path) {
    require(GDALGetRasterCount(source) > 0, "DEM processing requires a raster dataset");
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid output path");
    auto processing = args.GetString("processing");
    const char* mode = nullptr;
    for (const char* candidate : {"hillshade", "slope", "aspect", "TRI", "TPI", "roughness"})
        if (EQUAL(processing.c_str(), candidate)) mode = candidate;
    require(mode != nullptr && processing.find('\0') == std::string::npos, "Unsupported DEM processing mode");
    auto driver = args.GetString("driver");
    require(!driver.empty() && !EQUAL(driver.c_str(), "MEM") && !EQUAL(driver.c_str(), "VRT") && !EQUAL(driver.c_str(), "stream"), "DEM processing requires a concrete output file format");
    FileOptions options(CPLJSONArray{});
    // Parsing with the actual mode rejects options meant for a different algorithm.
    options.add(mode);
    for (const auto& option : args.GetArray("arguments")) options.add(option.ToString());
    options.add("-of"); options.add(driver);
    auto opts = GDALDEMProcessingOptionsNew(options.list, nullptr);
    fileEnsure(opts != nullptr, "Invalid DEM processing options");
    GDALDEMProcessingOptionsSetProgress(opts, progress, activeProgress);
    int usageError = 0;
    auto result = GDALDEMProcessing(path.c_str(), source, mode, nullptr, opts, &usageError);
    GDALDEMProcessingOptionsFree(opts);
    auto closeCode = result ? GDALClose(result) : CE_Failure;
    fileEnsure(result != nullptr && closeCode == CE_None && !usageError, "GDAL DEM processing failed");
}
inline CPLJSONObject demProcessing(GDALDatasetH source, const CPLJSONObject& args, bool direct) {
    if (direct) { demProcessingPath(source, args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name);
    Directory output;
    demProcessingPath(source, args, output.path + "/" + name);
    return outputFiles(output, name);
}

inline void rasterizeDatasetPath(GDALDatasetH source, const CPLJSONObject& args, const std::string& path) {
    require(GDALDatasetGetLayerCount(source) > 0, "Rasterize requires a vector dataset");
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid output path");
    auto driver = args.GetString("driver");
    require(!driver.empty() && !EQUAL(driver.c_str(), "MEM") && !EQUAL(driver.c_str(), "VRT"), "Rasterize requires a concrete output file format");
    FileOptions options(args.GetArray("arguments"));
    options.add("-of"); options.add(driver);
    auto opts = GDALRasterizeOptionsNew(options.list, nullptr);
    fileEnsure(opts != nullptr, "Invalid rasterize options");
    GDALRasterizeOptionsSetProgress(opts, progress, activeProgress);
    int usageError = 0;
    auto result = GDALRasterize(path.c_str(), nullptr, source, opts, &usageError);
    GDALRasterizeOptionsFree(opts);
    auto closeCode = result ? GDALClose(result) : CE_Failure;
    fileEnsure(result != nullptr && closeCode == CE_None && !usageError, "GDAL rasterize failed");
}
inline CPLJSONObject rasterizeDataset(GDALDatasetH source, const CPLJSONObject& args, bool direct) {
    if (direct) { rasterizeDatasetPath(source, args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name);
    Directory output;
    rasterizeDatasetPath(source, args, output.path + "/" + name);
    return outputFiles(output, name);
}

#include "runtime-polygonize.hpp"
#include "runtime-footprint.hpp"
#include "runtime-tile-index.hpp"
#include "runtime-grid-viewshed.hpp"

#include "runtime-analysis.hpp"

struct RasterSources {
    std::vector<GDALDatasetH> datasets;
    std::vector<unsigned long long> ids;
    RasterSources(Runtime& r,const CPLJSONArray& array) {
        require(array.Size()>0,"At least one source dataset is required");
        for(auto source:array) {
            auto id=std::stoull(source.GetString("handle"));auto p=r.get(id,"Dataset");
            require(GDALGetRasterCount(p)>0,"Source dataset has no raster bands");
            datasets.push_back(p);ids.push_back(id);
        }
    }
};
inline CPLJSONObject buildVirtualDataset(Runtime& r,const CPLJSONObject& a) {
    RasterSources sources(r,a.GetArray("sources"));auto arguments=a.GetArray("arguments");bool permissive=false;
    for(auto arg:arguments)if(arg.ToString()=="-non_strict")permissive=true;
    FileOptions options(arguments);if(!permissive)options.add("-strict");
    auto opts=GDALBuildVRTOptionsNew(options.list,nullptr);fileEnsure(opts!=nullptr,"Invalid VRT options");
    GDALBuildVRTOptionsSetProgress(opts, progress, activeProgress);
    int usage=0;auto p=GDALBuildVRT("",static_cast<int>(sources.datasets.size()),sources.datasets.data(),nullptr,opts,&usage);
    GDALBuildVRTOptionsFree(opts);
    if(!p||usage){if(p)GDALClose(p);fileEnsure(false,"Cannot build VRT");}
    auto result=r.result(p,"Dataset",true);auto id=std::stoull(result.GetString("value"));r.retainSources(id,sources.ids);return result;
}
inline void warpDatasetPath(Runtime& r,const CPLJSONObject& a,const std::string& path) {
    RasterSources sources(r,a.GetArray("sources"));
    require(!path.empty()&&path.find('\0')==std::string::npos,"Invalid output path");
    auto driver=a.GetString("driver"),target=a.GetString("target");
    require(!target.empty()&&target.find('\0')==std::string::npos,"Target SRS is required");
    require(!EQUAL(driver.c_str(),"VRT")&&!EQUAL(driver.c_str(),"MEM"),"Warp file output requires a concrete format, not VRT or MEM");
    FileOptions options(a.GetArray("arguments"));options.add("-t_srs");options.add(target);options.add("-of");options.add(driver);
    auto opts=GDALWarpAppOptionsNew(options.list,nullptr);fileEnsure(opts!=nullptr,"Invalid warp options");
    GDALWarpAppOptionsSetProgress(opts, progress, activeProgress);
    OutputTemporaryDirectory temporary(path);
    int usage=0;auto p=GDALWarp(path.c_str(),nullptr,static_cast<int>(sources.datasets.size()),sources.datasets.data(),opts,&usage);
    GDALWarpAppOptionsFree(opts);auto code=p?GDALClose(p):CE_Failure;
    fileEnsure(p!=nullptr&&code==CE_None&&!usage,"Cannot warp datasets");
}
inline CPLJSONObject warpDatasets(Runtime& r,const CPLJSONObject& a,bool direct) {
    if(direct){warpDatasetPath(r,a,a.GetString("path"));return nullValue();}
    Directory output;auto name=a.GetString("name");fileBasename(name);
    warpDatasetPath(r,a,output.path+"/"+name);return outputFiles(output,name);
}

#include "runtime-md-translate.hpp"
