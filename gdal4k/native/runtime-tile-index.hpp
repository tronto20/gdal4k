// Included inside namespace g4k after the shared file utilities.
inline void tileIndexPath(const CPLJSONObject& args, const std::string& path) {
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid tile index output path");
    auto driverName = args.GetString("driver"), layerName = args.GetString("layerName"), field = args.GetString("locationField");
    require(!driverName.empty() && driverName.find('\0') == std::string::npos &&
            !EQUAL(driverName.c_str(), "MEM") && !EQUAL(driverName.c_str(), "Memory") && !EQUAL(driverName.c_str(), "VRT"),
            "Tile index requires a concrete vector file format");
    auto driver = GDALGetDriverByName(driverName.c_str());
    require(driver && CPLTestBool(CSLFetchNameValueDef(GDALGetMetadata(driver, nullptr), GDAL_DCAP_VECTOR, "NO")) &&
            CPLTestBool(CSLFetchNameValueDef(GDALGetMetadata(driver, nullptr), GDAL_DCAP_CREATE, "NO")),
            "Tile index driver cannot create vector files");
    require(!layerName.empty() && layerName.find('\0') == std::string::npos &&
            !field.empty() && field.find('\0') == std::string::npos, "Invalid tile index layer or field name");
    FileOptions options(CPLJSONArray{}), sources(CPLJSONArray{});
    bool targetSrs = false;
    auto arguments = args.GetArray("arguments");
    const std::set<std::string> pairs = {"-t_srs", "-src_srs_name", "-src_srs_format", "-lco"};
    // No positional arguments, directory enumeration, silent filtering, output overrides,
    // metadata-derived fields or GTI side effects. The pinned parser validates values.
    for (int i = 0; i < arguments.Size(); ++i) {
        auto option = arguments[i].ToString();
        if (!pairs.count(option)) throw std::runtime_error("Unsupported tile index option: " + option);
        require(i + 1 < arguments.Size(), "Missing tile index option value");
        options.add(option); options.add(arguments[++i].ToString());
        if (option == "-t_srs") targetSrs = true;
        if (option == "-src_srs_name") require(arguments[i].ToString() != field, "Source CRS and location fields must differ");
    }
    options.add("-of"); options.add(driverName);
    options.add("-lyr_name"); options.add(layerName);
    options.add("-tileindex"); options.add(field);
    if (args.GetBool("absolutePaths")) options.add("-write_absolute_path");
    auto opts = std::unique_ptr<GDALTileIndexOptions, decltype(&GDALTileIndexOptionsFree)>(
        GDALTileIndexOptionsNew(options.list, nullptr), GDALTileIndexOptionsFree);
    fileEnsure(opts != nullptr, "Invalid tile index options");
    auto inputs = args.GetArray("sources");
    require(inputs.Size() > 0, "Tile index requires explicit raster files");
    std::set<std::string> unique;
    std::vector<std::string> expectedLocations;
    std::string cwd;
    if (args.GetBool("absolutePaths")) {
        auto current = CPLGetCurrentDir();
        require(current != nullptr, "Cannot resolve absolute tile index locations");
        cwd = current; CPLFree(current);
    }
    auto firstSrs = std::unique_ptr<void, decltype(&OSRDestroySpatialReference)>(nullptr, OSRDestroySpatialReference);
    bool first = true, firstHasSrs = false;
    for (auto entry : inputs) {
        binaryCheckpoint();
        auto source = entry.ToString();
        require(!source.empty() && source.find('\0') == std::string::npos && source.find_first_of("*?\n\r") == std::string::npos,
                "Tile index requires explicit file paths without wildcards");
        // Do not bypass the HTTP input provider's credentials, deadlines or leases by
        // letting GDAL open a connection string / network VSI path behind its back.
        auto colon = source.find(':');
        require(colon == std::string::npos || (colon == 1 && std::isalpha(static_cast<unsigned char>(source[0])) &&
                source.size() > 2 && (source[2] == '/' || source[2] == '\\')), "Tile index does not accept connection strings or URLs");
        require(source.rfind("/vsi", 0) != 0 || source.rfind("/vsimem/", 0) == 0, "Tile index source provider is not supported");
#ifdef __EMSCRIPTEN__
        require(source.rfind("/vsimem/", 0) == 0, "Web tile index requires explicit /vsimem/ files");
#endif
        require(unique.insert(source).second, "Duplicate tile index source");
        VSIStatBufL info;
        require(VSIStatL(source.c_str(), &info) == 0 && !VSI_ISDIR(info.st_mode), "Tile index source must be an existing file, not a directory");
        auto dataset = std::unique_ptr<void, decltype(&GDALClose)>(
            GDALOpenEx(source.c_str(), GDAL_OF_RASTER | GDAL_OF_VERBOSE_ERROR, nullptr, nullptr, nullptr), GDALClose);
        fileEnsure(dataset != nullptr && GDALGetRasterCount(dataset.get()) > 0, "Cannot open tile index raster");
        double gt[6];
        require(GDALGetRasterXSize(dataset.get()) > 0 && GDALGetRasterYSize(dataset.get()) > 0 &&
                GDALGetGeoTransform(dataset.get(), gt) == CE_None, "Tile index raster requires a geotransform and nonzero size");
        for (double value : gt) require(std::isfinite(value), "Tile index geotransform must be finite");
        require(std::isfinite(gt[1] * gt[5] - gt[2] * gt[4]) && gt[1] * gt[5] != gt[2] * gt[4], "Tile index geotransform is singular");
        auto srs = GDALGetSpatialRef(dataset.get());
        if (targetSrs) require(srs != nullptr, "Reprojected tile index requires a source CRS");
        if (first) {
            firstHasSrs = srs != nullptr;
            if (srs) firstSrs.reset(OSRClone(srs));
            first = false;
        } else if (!targetSrs) {
            require(firstHasSrs == (srs != nullptr) && (!srs || OSRIsSame(firstSrs.get(), srs)),
                    "Mixed tile index CRS requires explicit -t_srs");
        }
        sources.add(source);
        expectedLocations.push_back(args.GetBool("absolutePaths") && CPLIsFilenameRelative(source.c_str())
            ? CPLProjectRelativeFilenameSafe(cwd.c_str(), source.c_str()) : source);
    }
    VSIStatBufL existing;
    require(VSIStatL(path.c_str(), &existing) != 0, "Tile index output already exists");
    if (EQUAL(driverName.c_str(), "ESRI Shapefile")) {
        for (const auto* extension : {"shp", "shx", "dbf", "prj", "cpg", "qix", "sbn", "sbx"}) {
            auto sidecar = CPLResetExtensionSafe(path.c_str(), extension);
            require(VSIStatL(sidecar.c_str(), &existing) != 0, "Tile index output sidecar already exists");
        }
    } else if (EQUAL(driverName.c_str(), "GPKG")) {
        for (const auto* suffix : {"-wal", "-shm", "-journal"})
            require(VSIStatL((path + suffix).c_str(), &existing) != 0, "Tile index output journal already exists");
    }
    GDALTileIndexOptionsSetProgress(opts.get(), progress, activeProgress);
    OutputTemporaryDirectory temporary(path);
    CPLErrorReset(); int usageError = 0;
    // Pinned GDAL closes its newly created dataset on failure. The success handle is ours.
    auto output = std::unique_ptr<void, decltype(&GDALClose)>(GDALTileIndex(path.c_str(), inputs.Size(), sources.list, opts.get(), &usageError), GDALClose);
    fileEnsure(output != nullptr && !usageError, "GDAL TileIndex failed");
    auto closing = output.release();
    fileEnsure(GDALClose(closing) == CE_None, "Cannot close tile index output");
    // GeoJSON and other creation layers cannot be read while writing. Validate the
    // actual completed file, including location truncation, after closing its writer.
    auto verified = std::unique_ptr<void, decltype(&GDALClose)>(
        GDALOpenEx(path.c_str(), GDAL_OF_VECTOR | GDAL_OF_VERBOSE_ERROR, nullptr, nullptr, nullptr), GDALClose);
    fileEnsure(verified != nullptr, "Cannot reopen tile index output");
    auto layer = GDALDatasetGetLayer(verified.get(), 0);
    require(layer && OGR_L_GetFeatureCount(layer, TRUE) == inputs.Size(), "Tile index skipped an input");
    OGR_L_ResetReading(layer);
    for (const auto& expected : expectedLocations) {
        auto feature = std::unique_ptr<void, decltype(&OGR_F_Destroy)>(OGR_L_GetNextFeature(layer), OGR_F_Destroy);
        auto index = feature ? OGR_F_GetFieldIndex(feature.get(), field.c_str()) : -1;
        require(index >= 0 && expected == OGR_F_GetFieldAsString(feature.get(), index),
                "Tile index driver changed or truncated a source location");
    }
}
inline CPLJSONObject tileIndex(const CPLJSONObject& args, bool direct) {
    if (direct) { tileIndexPath(args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name); Directory output;
    tileIndexPath(args, output.path + "/" + name);
    return outputFiles(output, name);
}
