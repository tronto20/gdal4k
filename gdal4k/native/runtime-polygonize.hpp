// Included inside namespace g4k after the shared file utilities.
struct PolygonizeOptions {
    bool ignoreMask = false;
    FileOptions algorithm{CPLJSONArray{}}, dataset{CPLJSONArray{}}, layer{CPLJSONArray{}};
    explicit PolygonizeOptions(const CPLJSONArray& arguments) {
        bool diagonal = false;
        for (int i = 0; i < arguments.Size(); ++i) {
            auto option = arguments[i].ToString();
            require(option.find('\0') == std::string::npos, "NUL in Polygonize argument");
            if (option == "-8") {
                require(!diagonal, "Duplicate Polygonize -8 option");
                diagonal = true; algorithm.add("8CONNECTED=8");
            } else if (option == "-nomask") {
                require(!ignoreMask, "Duplicate Polygonize -nomask option"); ignoreMask = true;
            } else if (option == "-dsco" || option == "-lco") {
                require(++i < arguments.Size(), "Missing Polygonize creation option");
                auto text = arguments[i].ToString();
                auto equal = text.find('=');
                require(equal != std::string::npos && equal > 0, "Polygonize creation option requires KEY=VALUE");
                (option == "-dsco" ? dataset : layer).add(text);
            } else throw std::runtime_error("Unsupported Polygonize option: " + option);
        }
    }
};
struct PolygonizeOutput {
    GDALDatasetH handle;
    ~PolygonizeOutput() { if (handle) GDALClose(handle); }
    CPLErr close() { auto output = handle; handle = nullptr; return GDALClose(output); }
};
// GDAL 3.12.4's polygon writer converts even Int64 values through double. For
// Int64 sources, sample a point inside each finished polygon and write the exact
// source value before forwarding the feature to its real output layer.
class ExactIntegerPolygonLayer final : public OGRLayer {
    OGRLayer* output;
    GDALRasterBandH source;
    int fieldIndex;
    double inverse[6];
  protected:
    OGRErr ICreateFeature(OGRFeature* feature) override {
        auto geometry = feature->GetGeometryRef();
        auto point = geometry ? OGR_G_PointOnSurface(OGRGeometry::ToHandle(geometry)) : nullptr;
        if (!point) return OGRERR_FAILURE;
        if (OGR_G_IsEmpty(point)) { OGR_G_DestroyGeometry(point); return OGRERR_FAILURE; }
        auto x = OGR_G_GetX(point, 0), y = OGR_G_GetY(point, 0);
        OGR_G_DestroyGeometry(point);
        auto pixel = std::floor(inverse[0] + inverse[1] * x + inverse[2] * y);
        auto line = std::floor(inverse[3] + inverse[4] * x + inverse[5] * y);
        if (!std::isfinite(pixel) || !std::isfinite(line) || pixel < 0 || line < 0 ||
            pixel >= GDALGetRasterBandXSize(source) || line >= GDALGetRasterBandYSize(source)) {
            CPLError(CE_Failure, CPLE_AppDefined, "Cannot sample an interior Polygonize Int64 value");
            return OGRERR_FAILURE;
        }
        GIntBig value = 0;
        if (GDALRasterIO(source, GF_Read, static_cast<int>(pixel), static_cast<int>(line), 1, 1,
                         &value, 1, 1, GDT_Int64, 0, 0) != CE_None) return OGRERR_FAILURE;
        feature->SetField(fieldIndex, value);
        return output->CreateFeature(feature);
    }
  public:
    ExactIntegerPolygonLayer(OGRLayerH layer, GDALRasterBandH band, int field, GDALDatasetH dataset)
        : output(OGRLayer::FromHandle(layer)), source(band), fieldIndex(field) {
        double transform[6] = {0, 1, 0, 0, 0, 1};
        // GDAL uses identity georeferencing for an ungeoreferenced source.
        if (GDALGetGeoTransform(dataset, transform) != CE_None) {
            transform[0] = transform[2] = transform[3] = transform[4] = 0;
            transform[1] = transform[5] = 1;
        }
        require(GDALInvGeoTransform(transform, inverse), "Polygonize Int64 requires an invertible geotransform");
    }
    const OGRFeatureDefn* GetLayerDefn() const override { return output->GetLayerDefn(); }
    GDALDataset* GetDataset() override { return output->GetDataset(); }
    void ResetReading() override { output->ResetReading(); }
    OGRFeature* GetNextFeature() override { return output->GetNextFeature(); }
    int TestCapability(const char* capability) const override { return output->TestCapability(capability); }
    OGRErr StartTransaction() override { return output->StartTransaction(); }
    OGRErr CommitTransaction() override { return output->CommitTransaction(); }
    OGRErr RollbackTransaction() override { return output->RollbackTransaction(); }
};
inline void polygonizeDatasetPath(Runtime& runtime, GDALDatasetH source, const CPLJSONObject& args, const std::string& path) {
    auto index = args.GetInteger("band");
    require(index >= 1 && index <= GDALGetRasterCount(source), "Polygonize source band is outside the raster");
    auto band = GDALGetRasterBand(source, index);
    auto type = GDALGetRasterDataType(band);
    require(!GDALDataTypeIsComplex(type) && type != GDT_UInt64 && (GDALDataTypeIsInteger(type) || GDALDataTypeIsFloating(type)),
            "Polygonize supports integer/real bands except UInt64; complex values are unsupported");
    require(!path.empty() && path.find('\0') == std::string::npos, "Invalid Polygonize output path");
    auto driverName = args.GetString("driver");
    require(driverName.find('\0') == std::string::npos && !EQUAL(driverName.c_str(), "MEM") && !EQUAL(driverName.c_str(), "Memory") && !EQUAL(driverName.c_str(), "VRT"),
            "Polygonize requires a concrete vector file format");
    auto driver = GDALGetDriverByName(driverName.c_str());
    require(driver && GDALGetMetadataItem(driver, GDAL_DCAP_VECTOR, nullptr) && GDALGetMetadataItem(driver, GDAL_DCAP_CREATE, nullptr),
            "Polygonize driver cannot create vector files");
    auto layerName = args.GetString("layerName"), fieldName = args.GetString("fieldName");
    require(!layerName.empty() && layerName.find('\0') == std::string::npos, "Invalid Polygonize layer name");
    require(!fieldName.empty() && fieldName.find('\0') == std::string::npos, "Invalid Polygonize field name");
    PolygonizeOptions options(args.GetArray("arguments"));
    GDALRasterBandH mask = nullptr;
    auto suppliedMask = args["mask"];
    if (suppliedMask.IsValid() && suppliedMask.GetType() != CPLJSONObject::Type::Null) {
        require(!options.ignoreMask, "Explicit Polygonize mask cannot be combined with -nomask");
        mask = runtime.arg(suppliedMask);
        require(GDALGetRasterBandXSize(mask) == GDALGetRasterBandXSize(band) && GDALGetRasterBandYSize(mask) == GDALGetRasterBandYSize(band),
                "Polygonize mask dimensions differ from the source");
    } else if (!options.ignoreMask) {
        mask = GDALGetMaskBand(band); fileEnsure(mask != nullptr, "Cannot obtain Polygonize default mask");
    }
    VSIStatBufL existing;
    require(VSIStatL(path.c_str(), &existing) != 0, "Polygonize output already exists");
    CPLErrorReset();
    OutputTemporaryDirectory temporary(path);
    PolygonizeOutput output{GDALCreate(driver, path.c_str(), 0, 0, 0, GDT_Unknown, options.dataset.list)};
    fileEnsure(output.handle != nullptr, "Cannot create Polygonize vector output");
    auto layer = GDALDatasetCreateLayer(output.handle, layerName.c_str(), GDALGetSpatialRef(source), wkbPolygon, options.layer.list);
    fileEnsure(layer != nullptr, "Cannot create Polygonize layer");
    const bool floating = GDALDataTypeIsFloating(type);
    auto field = OGR_Fld_Create(fieldName.c_str(), floating ? OFTReal : OFTInteger64);
    fileEnsure(field != nullptr, "Cannot allocate Polygonize value field");
    auto fieldCode = OGR_L_CreateField(layer, field, FALSE);
    OGR_Fld_Destroy(field);
    fileEnsure(fieldCode == OGRERR_NONE, "Cannot create Polygonize value field");
    // Use the created index: a driver may normalize the requested field name.
    auto fieldIndex = OGR_FD_GetFieldCount(OGR_L_GetLayerDefn(layer)) - 1;
    require(fieldIndex >= 0, "Polygonize output value field is missing");
    fileEnsure(GDALFlushCache(output.handle) == CE_None, "Cannot flush Polygonize output schema");
    // GDAL 3.12's internal transaction finalizer does not commit an empty layer.
    // Own the new-output transaction so an all-masked raster still creates a valid file.
    options.algorithm.add("COMMIT_INTERVAL=0");
    const bool transactional = OGR_L_TestCapability(layer, OLCTransactions);
    if (transactional) fileEnsure(OGR_L_StartTransaction(layer) == OGRERR_NONE, "Cannot start Polygonize output transaction");
    CPLErr code;
    if (type == GDT_Int64) {
        ExactIntegerPolygonLayer exact(layer, band, fieldIndex, source);
        code = GDALPolygonize(band, mask, OGRLayer::ToHandle(&exact), -1, options.algorithm.list, progress, activeProgress);
    } else {
        code = floating ? GDALFPolygonize(band, mask, layer, fieldIndex, options.algorithm.list, progress, activeProgress)
                        : GDALPolygonize(band, mask, layer, fieldIndex, options.algorithm.list, progress, activeProgress);
    }
    auto transactionCode = transactional ? (code == CE_None ? OGR_L_CommitTransaction(layer) : OGR_L_RollbackTransaction(layer)) : OGRERR_NONE;
    auto closeCode = output.close();
    fileEnsure(code == CE_None && transactionCode == OGRERR_NONE && closeCode == CE_None, "GDAL Polygonize failed");
}
inline CPLJSONObject polygonizeDataset(Runtime& runtime, GDALDatasetH source, const CPLJSONObject& args, bool direct) {
    if (direct) { polygonizeDatasetPath(runtime, source, args, args.GetString("path")); return nullValue(); }
    auto name = args.GetString("name"); fileBasename(name);
    Directory output;
    polygonizeDatasetPath(runtime, source, args, output.path + "/" + name);
    return outputFiles(output, name);
}
