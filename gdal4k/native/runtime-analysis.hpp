// Included after runtime-files.hpp utilities. No input handles are transferred.
inline void analysisBand(GDALRasterBandH band) {
    auto type = GDALGetRasterDataType(band);
    require(type != GDT_Unknown && !GDALDataTypeIsComplex(type), "Analysis requires a real numeric band");
}
inline void analysisSameSize(GDALRasterBandH first, GDALRasterBandH second) {
    require(GDALGetRasterBandXSize(first) == GDALGetRasterBandXSize(second) &&
            GDALGetRasterBandYSize(first) == GDALGetRasterBandYSize(second), "Analysis band dimensions differ");
}
inline GDALRasterBandH analysisMask(Runtime& runtime, GDALRasterBandH source, const CPLJSONObject& args, bool useDefault) {
    auto supplied = args["mask"];
    auto mask = supplied.IsValid() && supplied.GetType() != CPLJSONObject::Type::Null ? runtime.arg(supplied) :
        useDefault ? GDALGetMaskBand(source) : nullptr;
    if (mask) analysisSameSize(source, mask);
    return mask;
}
inline double analysisNumber(const std::string& text) {
    char* end = nullptr;
    const double result = CPLStrtod(text.c_str(), &end);
    require(!text.empty() && end == text.c_str() + text.size() && std::isfinite(result), "Invalid finite analysis number");
    return result;
}
inline std::pair<std::string, std::string> analysisOption(const std::string& text) {
    auto split = text.find('=');
    require(text.find('\0') == std::string::npos && split != std::string::npos && split > 0 && split + 1 < text.size(), "Analysis option requires KEY=VALUE");
    return {text.substr(0, split), text.substr(split + 1)};
}
inline CPLJSONObject bandAnalysis(Runtime& runtime, GDALRasterBandH source, const CPLJSONObject& args, const std::string& operation) {
    analysisBand(source);
    if (operation == "sieveTo") {
        const auto threshold = args.GetInteger("threshold"), connectedness = args.GetInteger("connectedness");
        require(threshold > 0 && (connectedness == 4 || connectedness == 8), "Invalid Sieve threshold or connectedness");
        require(GDALGetRasterDataType(source) != GDT_UInt64, "Sieve cannot represent UInt64 values");
        auto target = runtime.arg(args["target"]);
        analysisBand(target); analysisSameSize(source, target);
        require(GDALGetRasterAccess(target) == GA_Update, "Sieve target is read-only");
        auto mask = analysisMask(runtime, source, args, args.GetBool("useDefaultMask"));
        require(!mask || mask != target, "Sieve mask cannot be the target band");
        return value(GDALSieveFilter(source, mask, target, threshold, connectedness, nullptr, progress, activeProgress));
    }
    FileOptions options(CPLJSONArray{});
    std::vector<std::string> seen;
    for (auto entry : args.GetArray("arguments")) {
        auto text = entry.ToString(); auto [key, val] = analysisOption(text);
        require(std::find(seen.begin(), seen.end(), key) == seen.end(), "Duplicate analysis option"); seen.push_back(key);
        if (operation == "proximityTo") {
            require(key == "VALUES" || key == "DISTUNITS" || key == "MAXDIST" || key == "NODATA" || key == "USE_INPUT_NODATA" || key == "FIXED_BUF_VAL", "Unsupported Proximity option");
            if (key == "DISTUNITS") require(val == "PIXEL" || val == "GEO", "Invalid proximity distance units");
            else if (key == "USE_INPUT_NODATA") require(val == "YES" || val == "NO", "Invalid proximity NoData policy");
            else if (key == "VALUES") {
                size_t begin = 0;
                do {
                    auto end = val.find(',', begin); auto number = analysisNumber(val.substr(begin, end - begin));
                    require(number >= INT_MIN && number <= INT_MAX && std::floor(number) == number, "Proximity VALUES require Int32 integers");
                    if (end == std::string::npos) break;
                    begin = end + 1;
                } while (true);
            } else { auto number = analysisNumber(val); require(key != "MAXDIST" || number >= 0, "Negative maximum distance"); }
        } else {
            require(key == "TEMP_FILE_DRIVER" || key == "NODATA" || key == "INTERPOLATION", "Unsupported FillNoData option");
            if (key == "NODATA") analysisNumber(val);
            if (key == "INTERPOLATION") require(val == "INV_DIST" || val == "NEAREST", "Invalid FillNoData interpolation");
            if (key == "TEMP_FILE_DRIVER") require(val == "MEM" || val == "GTiff", "FillNoData scratch driver must be MEM or GTiff");
        }
        options.add(text);
    }
    if (operation == "proximityTo") {
        auto target = runtime.arg(args["target"]);
        require(source != target, "Proximity source and target must differ");
        analysisBand(target); analysisSameSize(source, target);
        require(GDALGetRasterAccess(target) == GA_Update, "Proximity target is read-only");
        // GDAL uses a signed temporary GTiff for unsigned proximity targets.
        // Android's cwd may be read-only. Preserve an explicit CPL_TMPDIR;
        // otherwise use the real output filesystem, or a scoped /vsimem workspace
        // for an in-memory target. RAII restores thread-local config and removes
        // any scratch files on success, error or cooperative cancellation.
        auto type = GDALGetRasterDataType(target);
        const bool scratchNeeded = type == GDT_Byte || type == GDT_UInt16 || type == GDT_UInt32;
        std::unique_ptr<Directory> scratch;
        std::string temporaryPath;
        if (scratchNeeded && !CPLGetConfigOption("CPL_TMPDIR", nullptr)) {
            auto dataset = GDALGetBandDataset(target);
            char** files = dataset ? GDALGetFileList(dataset) : nullptr;
            if (files && files[0]) temporaryPath = files[0];
            CSLDestroy(files);
            if (temporaryPath.empty()) {
                scratch = std::make_unique<Directory>();
                temporaryPath = scratch->path + "/proximity.tif";
            }
        }
        OutputTemporaryDirectory temporary(temporaryPath, !temporaryPath.empty());
        return value(GDALComputeProximity(source, target, options.list, progress, activeProgress));
    }
    auto distance = analysisNumber(args["maxSearchDistance"].ToString());
    auto smoothing = args.GetInteger("smoothingIterations");
    require(distance >= 0 && distance < INT_MAX && smoothing >= 0 && smoothing <= INT_MAX - 2, "Invalid FillNoData search distance or smoothing iterations");
    require(GDALGetRasterAccess(source) == GA_Update, "FillNoData band is read-only");
    auto mask = analysisMask(runtime, source, args, true);
    require(mask != source, "FillNoData mask cannot be its target band");
    if (!CSLFetchNameValue(options.list, "TEMP_FILE_DRIVER")) options.add("TEMP_FILE_DRIVER=MEM");
    return value(GDALFillNodata(source, mask, distance, 0, smoothing, options.list, progress, activeProgress));
}
inline void contourDatasetPath(GDALDatasetH source, const CPLJSONObject& args, const std::string& path) {
    auto bandIndex = args.GetInteger("band");
    require(bandIndex >= 1 && bandIndex <= GDALGetRasterCount(source), "Contour band is outside the raster");
    auto band = GDALGetRasterBand(source, bandIndex); analysisBand(band);
    auto interval = analysisNumber(args["interval"].ToString());
    auto base = analysisNumber(args["base"].ToString());
    require(interval > 0, "Contour interval must be positive");
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid Contour output path");
    auto driverName = args.GetString("driver"), layerName = args.GetString("layerName");
    require(!driverName.empty() && driverName.find('\0') == std::string::npos && !EQUAL(driverName.c_str(), "MEM") && !EQUAL(driverName.c_str(), "Memory") && !EQUAL(driverName.c_str(), "VRT"), "Contour requires a concrete vector format");
    auto driver = GDALGetDriverByName(driverName.c_str());
    require(driver && GDALGetMetadataItem(driver, GDAL_DCAP_VECTOR, nullptr) && GDALGetMetadataItem(driver, GDAL_DCAP_CREATE, nullptr), "Contour driver cannot create vector files");
    require(!layerName.empty() && layerName.find('\0') == std::string::npos, "Invalid Contour layer name");
    FileOptions options(CPLJSONArray{}), datasetOptions(CPLJSONArray{}), layerOptions(CPLJSONArray{});
    std::vector<std::string> seen;
    auto arguments = args.GetArray("arguments");
    for (int i = 0; i < arguments.Size(); ++i) {
        auto text = arguments[i].ToString();
        if (text == "-dsco" || text == "-lco") {
            require(++i < arguments.Size(), "Missing Contour creation option");
            auto creation = arguments[i].ToString(); analysisOption(creation);
            (text == "-dsco" ? datasetOptions : layerOptions).add(creation);
        } else {
            auto [key, val] = analysisOption(text);
            require(key == "FIXED_LEVELS" || key == "LEVEL_EXP_BASE" || key == "NODATA", "Unsupported Contour option");
            require(std::find(seen.begin(), seen.end(), key) == seen.end(), "Duplicate Contour option"); seen.push_back(key);
            if (key == "FIXED_LEVELS") {
                size_t begin = 0;
                do {
                    auto end = val.find(',', begin); analysisNumber(val.substr(begin, end - begin));
                    if (end == std::string::npos) break;
                    begin = end + 1;
                } while (true);
            } else { auto number = analysisNumber(val); require(key != "LEVEL_EXP_BASE" || number > 1, "Exponential contour base must exceed one"); }
            options.add(text);
        }
    }
    options.add("LEVEL_INTERVAL=" + args["interval"].ToString());
    options.add("LEVEL_BASE=" + args["base"].ToString());
    options.add("ID_FIELD=0"); options.add("ELEV_FIELD=1"); options.add("COMMIT_INTERVAL=0");
    int hasNoData = FALSE; auto noData = GDALGetRasterNoDataValue(band, &hasNoData);
    if (hasNoData && !CSLFetchNameValue(options.list, "NODATA")) options.add("NODATA=" + std::string(CPLSPrintf("%.17g", noData)));
    VSIStatBufL existing; require(VSIStatL(path.c_str(), &existing) != 0, "Contour output already exists");
    OutputTemporaryDirectory temporary(path);
    PolygonizeOutput output{GDALCreate(driver, path.c_str(), 0, 0, 0, GDT_Unknown, datasetOptions.list)};
    fileEnsure(output.handle != nullptr, "Cannot create Contour output");
    auto layer = GDALDatasetCreateLayer(output.handle, layerName.c_str(), GDALGetSpatialRef(source), wkbLineString, layerOptions.list);
    fileEnsure(layer != nullptr, "Cannot create Contour layer");
    for (auto pair : {std::pair<const char*, OGRFieldType>{"id", OFTInteger64}, {"elevation", OFTReal}}) {
        auto field = OGR_Fld_Create(pair.first, pair.second); fileEnsure(field != nullptr, "Cannot allocate Contour field");
        auto code = OGR_L_CreateField(layer, field, FALSE); OGR_Fld_Destroy(field);
        fileEnsure(code == OGRERR_NONE, "Cannot create Contour field");
    }
    fileEnsure(GDALFlushCache(output.handle) == CE_None, "Cannot flush Contour schema");
    const bool transactional = OGR_L_TestCapability(layer, OLCTransactions);
    if (transactional) fileEnsure(OGR_L_StartTransaction(layer) == OGRERR_NONE, "Cannot start Contour transaction");
    auto code = GDALContourGenerateEx(band, layer, options.list, progress, activeProgress);
    auto transactionCode = transactional ? (code == CE_None ? OGR_L_CommitTransaction(layer) : OGR_L_RollbackTransaction(layer)) : OGRERR_NONE;
    auto closeCode = output.close();
    fileEnsure(code == CE_None && transactionCode == OGRERR_NONE && closeCode == CE_None, "GDAL Contour failed");
}
inline CPLJSONObject contourDataset(GDALDatasetH source, const CPLJSONObject& args, bool direct) {
    if (direct) { contourDatasetPath(source, args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name); Directory output;
    contourDatasetPath(source, args, output.path + "/" + name);
    return outputFiles(output, name);
}
