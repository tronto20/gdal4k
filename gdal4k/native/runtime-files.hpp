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
    for (const auto& file : files.GetArray("files")) {
        auto name = file.GetString("name"); fileBasename(name);
        auto base64 = file.GetString("data");
        std::vector<GByte> data(base64.begin(), base64.end()); data.push_back(0);
        int count = CPLBase64DecodeInPlace(data.data());
        auto path = workspace.path + "/" + name;
        auto output = VSIFOpenL(path.c_str(), "wb"); fileEnsure(output != nullptr, "Cannot create input file");
        auto written = VSIFWriteL(data.data(), 1, count, output);
        auto code = VSIFCloseL(output);
        fileEnsure(written == static_cast<size_t>(count) && code == 0, "Cannot write input file");
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
        bool found = false;
        for (int i = 0; names && names[i]; ++i) {
            const std::string name(names[i]);
            if (name == "." || name == "..") continue;
            fileBasename(name);
            vsi_l_offset size = 0;
            auto data = VSIGetMemFileBuffer((directory.path + "/" + name).c_str(), &size, FALSE);
            fileEnsure(data != nullptr, "Output must be a flat set of files");
            require(size <= static_cast<vsi_l_offset>(INT_MAX), "Output file exceeds bridge size limit");
            char* encoded = CPLBase64Encode(static_cast<int>(size), data);
            fileEnsure(encoded != nullptr, "Cannot encode output file");
            CPLJSONObject file;
            file.Add("name", name); file.Add("data", encoded); CPLFree(encoded);
            files.Add(file); found = found || name == main;
        }
        require(found, "GDAL did not create the main output file");
    } catch (...) { CSLDestroy(names); throw; }
    CSLDestroy(names);
    CPLJSONObject result; result.Add("main", main); result.Add("files", files);
    return objectValue(result);
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
        result = GDALTranslate(path.c_str(), dataset, opts, &usageError); GDALTranslateOptionsFree(opts);
    } else if (operation == "warp") {
        auto opts = GDALWarpAppOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid warp options");
        result = GDALWarp(path.c_str(), nullptr, 1, &dataset, opts, &usageError); GDALWarpAppOptionsFree(opts);
    } else {
        auto opts = GDALVectorTranslateOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid vector translate options");
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
