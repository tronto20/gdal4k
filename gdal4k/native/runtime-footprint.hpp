// Included inside namespace g4k after the shared file utilities.
inline void footprintDatasetPath(GDALDatasetH source, const CPLJSONObject& args, const std::string& path) {
    require(GDALGetRasterCount(source) > 0, "Footprint requires a raster dataset");
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid Footprint output path");
    auto driverName = args.GetString("driver"), layerName = args.GetString("layerName");
    require(!driverName.empty() && driverName.find('\0') == std::string::npos &&
            !EQUAL(driverName.c_str(), "MEM") && !EQUAL(driverName.c_str(), "Memory") && !EQUAL(driverName.c_str(), "VRT"),
            "Footprint requires a concrete vector file format");
    auto driver = GDALGetDriverByName(driverName.c_str());
    require(driver && CPLTestBool(CSLFetchNameValueDef(GDALGetMetadata(driver, nullptr), GDAL_DCAP_VECTOR, "NO")) &&
            CPLTestBool(CSLFetchNameValueDef(GDALGetMetadata(driver, nullptr), GDAL_DCAP_CREATE, "NO")),
            "Footprint driver cannot create vector files");
    require(!layerName.empty() && layerName.find('\0') == std::string::npos, "Invalid Footprint layer name");
    const auto arguments = args.GetArray("arguments");
    FileOptions options(CPLJSONArray{});
    bool maxPoints = false;
    const std::set<std::string> flags = {"-split_polys", "-convex_hull"};
    const std::set<std::string> pairs = {"-b", "-combine_bands", "-srcnodata", "-ovr", "-t_cs", "-t_srs",
        "-densify", "-simplify", "-min_ring_area", "-max_points", "-dsco", "-lco"};
    // Do not accept positional paths or options overriding ownership/output settings.
    // Let the pinned GDAL parser validate values and incompatible combinations.
    for (int i = 0; i < arguments.Size(); ++i) {
        auto option = arguments[i].ToString();
        if (flags.count(option)) { options.add(option); continue; }
        if (!pairs.count(option)) throw std::runtime_error("Unsupported Footprint option: " + option);
        require(i + 1 < arguments.Size(), "Missing Footprint option value");
        options.add(option); options.add(arguments[++i].ToString());
        if (option == "-max_points") maxPoints = true;
    }
    if (!maxPoints) { options.add("-max_points"); options.add("unlimited"); }
    options.add("-of"); options.add(driverName);
    options.add("-lyr_name"); options.add(layerName);
    // Dataset descriptions can contain signed URLs or private VSI paths. A geometric
    // result must not silently persist those descriptions in a location field.
    options.add("-no_location");
    auto opts = std::unique_ptr<GDALFootprintOptions, decltype(&GDALFootprintOptionsFree)>(
        GDALFootprintOptionsNew(options.list, nullptr), GDALFootprintOptionsFree);
    fileEnsure(opts != nullptr, "Invalid Footprint options");
    VSIStatBufL existing;
    require(VSIStatL(path.c_str(), &existing) != 0, "Footprint output already exists");
    GDALFootprintOptionsSetProgress(opts.get(), progress, activeProgress);
    OutputTemporaryDirectory temporary(path);
    CPLErrorReset();
    int usageError = 0;
    // With no destination handle GDAL owns/closes its newly created handle on
    // failure; on success the returned handle transfers to us and must be closed.
    auto output = GDALFootprint(path.c_str(), nullptr, source, opts.get(), &usageError);
    auto closeCode = output ? GDALClose(output) : CE_Failure;
    fileEnsure(output != nullptr && !usageError && closeCode == CE_None, "GDAL Footprint failed");
}
inline CPLJSONObject footprintDataset(GDALDatasetH source, const CPLJSONObject& args, bool direct) {
    if (direct) { footprintDatasetPath(source, args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name);
    Directory output;
    footprintDatasetPath(source, args, output.path + "/" + name);
    return outputFiles(output, name);
}
