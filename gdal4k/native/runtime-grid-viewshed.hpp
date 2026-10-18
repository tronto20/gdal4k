inline double viewshedNumber(const CPLJSONObject& args, const char* key) {
    auto text = args[key].ToString(); char* end = nullptr; auto number = CPLStrtod(text.c_str(), &end);
    require(!text.empty() && end == text.c_str() + text.size() && std::isfinite(number), "Viewshed requires finite numbers");
    return number;
}
inline void gridViewshedPath(GDALDatasetH source, const CPLJSONObject& args, const std::string& operation, const std::string& path) {
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid raster analysis output path");
    auto driverName = args.GetString("driver");
    require(!driverName.empty() && driverName.find('\0') == std::string::npos && !EQUAL(driverName.c_str(), "MEM") && !EQUAL(driverName.c_str(), "VRT") && !EQUAL(driverName.c_str(), "stream"), "Analysis requires a concrete raster file format");
    auto driver = GDALGetDriverByName(driverName.c_str());
    require(driver && GDALGetMetadataItem(driver, GDAL_DCAP_RASTER, nullptr) && GDALGetMetadataItem(driver, GDAL_DCAP_CREATE, nullptr), "Analysis driver must support raster creation");
    VSIStatBufL existing; require(VSIStatL(path.c_str(), &existing) != 0, "Analysis output already exists");
    OutputTemporaryDirectory temporary(path);
    GDALDatasetH result = nullptr;
    if (operation == "grid") {
        require(GDALDatasetGetLayerCount(source) > 0, "Grid requires a vector dataset");
        FileOptions options(args.GetArray("arguments")); options.add("-of"); options.add(driverName);
        auto opts = GDALGridOptionsNew(options.list, nullptr); fileEnsure(opts != nullptr, "Invalid Grid options");
        int usage = 0; result = GDALGrid(path.c_str(), source, opts, &usage); GDALGridOptionsFree(opts);
        auto closeCode = result ? GDALClose(result) : CE_Failure;
        fileEnsure(result != nullptr && closeCode == CE_None && !usage, "GDAL Grid failed"); return;
    }
    auto index = args.GetInteger("band"); require(index >= 1 && index <= GDALGetRasterCount(source), "Viewshed band is outside the raster");
    auto band = GDALGetRasterBand(source, index); auto type = GDALGetRasterDataType(band);
    require(type != GDT_Unknown && !GDALDataTypeIsComplex(type), "Viewshed requires a real numeric DEM");
    auto srs = GDALGetSpatialRef(source); require(srs && OSRIsProjected(srs), "Viewshed requires a projected CRS");
    double transform[6]; require(GDALGetGeoTransform(source, transform) == CE_None && transform[1] > 0 && transform[5] < 0 && transform[2] == 0 && transform[4] == 0, "Viewshed requires a north-up geotransform");
    for (auto v : transform) require(std::isfinite(v), "Viewshed requires a finite geotransform");
    auto options = args.GetObj("options");
    auto x = viewshedNumber(options, "observerX"), y = viewshedNumber(options, "observerY");
    auto observerHeight = viewshedNumber(options, "observerHeight"), targetHeight = viewshedNumber(options, "targetHeight");
    auto distance = viewshedNumber(options, "maxDistance"), curvature = viewshedNumber(options, "curvatureCoefficient");
    auto noData = viewshedNumber(options, "noDataValue");
    auto mode = options.GetInteger("mode"), output = options.GetInteger("output");
    auto visible = viewshedNumber(options, "visibleValue"), invisible = viewshedNumber(options, "invisibleValue"), outOfRange = viewshedNumber(options, "outOfRangeValue");
    require(distance >= 0 && mode >= 1 && mode <= 4 && output >= 1 && output <= 3, "Invalid Viewshed mode or distance");
    if (output == GVOT_NORMAL) {
        for (auto v : {visible, invisible, outOfRange}) require(v >= 0 && v <= 255 && std::floor(v) == v, "Viewshed visibility values must fit Byte");
    }
    require(output != GVOT_NORMAL || noData < 0 || (noData <= 255 && std::floor(noData) == noData), "Viewshed Byte NoData must be an integer between 0 and 255");
    FileOptions creation(args.GetArray("creationOptions"));
    for (auto entry : args.GetArray("creationOptions")) {
        auto text = entry.ToString(); auto equal = text.find('='); require(equal != std::string::npos && equal > 0, "Viewshed creation option requires KEY=VALUE");
    }
    result = GDALViewshedGenerate(band, driverName.c_str(), path.c_str(), creation.list, x, y, observerHeight, targetHeight,
        visible, invisible, outOfRange, noData, curvature, static_cast<GDALViewshedMode>(mode), distance, nullptr, nullptr,
        static_cast<GDALViewshedOutputType>(output), nullptr);
    auto closeCode = result ? GDALClose(result) : CE_Failure;
    fileEnsure(result != nullptr && closeCode == CE_None, "GDAL Viewshed failed");
}
inline CPLJSONObject gridViewshedDataset(GDALDatasetH source, const CPLJSONObject& args, const std::string& operation, bool direct) {
    if (direct) { gridViewshedPath(source, args, operation, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name); Directory output;
    gridViewshedPath(source, args, operation, output.path + "/" + name);
    return outputFiles(output, name);
}
