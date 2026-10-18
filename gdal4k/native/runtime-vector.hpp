// Included inside g4k after Runtime and the shared file helpers.
struct VectorGeometry {
    OGRGeometryH handle = nullptr;
    ~VectorGeometry() { if (handle) OGR_G_DestroyGeometry(handle); }
    CPLJSONObject result(Runtime& runtime) {
        auto value = runtime.result(handle, "Geometry", true); handle = nullptr; return value;
    }
};
inline std::string vectorText(const CPLJSONObject& input) {
    auto text = input.ToString();
    require(!text.empty() && text.find('\0') == std::string::npos, "Empty geometry text or embedded NUL");
    return text;
}
inline void vectorSameCrs(OGRSpatialReferenceH left, OGRSpatialReferenceH right) {
    require(!left || !right || OSRIsSame(left, right), "Geometry CRSs differ; transform coordinates explicitly before this operation");
}
inline CPLJSONObject createVectorGeometry(Runtime& runtime, const CPLJSONObject& args, const std::string& method) {
    auto srs = args["spatialReference"].GetType() == CPLJSONObject::Type::Null ? nullptr : runtime.arg(args["spatialReference"]);
    VectorGeometry geometry;
    CPLErrorReset();
    if (method == "createGeometryFromWkt") {
        auto text = vectorText(args["wkt"]); char* cursor = text.data();
        auto code = OGR_G_CreateFromWkt(&cursor, srs, &geometry.handle);
        fileEnsure(code == OGRERR_NONE && geometry.handle, "Invalid WKT geometry");
        while (*cursor && std::isspace(static_cast<unsigned char>(*cursor))) ++cursor;
        require(!*cursor, "Trailing content after WKT geometry");
    } else if (method == "createGeometryFromWkb") {
        const auto input = args.GetArray("wkb");
        // GDAL interprets size zero as unchecked input. Reject it before passing a pointer.
        require(input.Size() > 0, "Empty WKB geometry");
        std::vector<GByte> bytes; bytes.reserve(input.Size());
        for (const auto& item : input) bytes.push_back(static_cast<GByte>(item.ToInteger()));
        auto code = OGR_G_CreateFromWkbEx(bytes.data(), srs, &geometry.handle, bytes.size());
        fileEnsure(code == OGRERR_NONE && geometry.handle, "Invalid or truncated WKB geometry");
    } else {
        auto text = vectorText(args["geoJson"]);
        geometry.handle = OGR_G_CreateGeometryFromJson(text.c_str());
        fileEnsure(geometry.handle != nullptr, "Invalid GeoJSON geometry");
        if (srs) OGR_G_AssignSpatialReference(geometry.handle, srs);
    }
    return geometry.result(runtime);
}
inline CPLJSONObject vectorOverlay(Runtime& runtime, OGRGeometryH source, const CPLJSONObject& args, const std::string& method) {
    auto other = runtime.arg(args["other"]);
    vectorSameCrs(OGR_G_GetSpatialReference(source), OGR_G_GetSpatialReference(other));
    CPLErrorReset();
    VectorGeometry output;
    output.handle = method == "intersection" ? OGR_G_Intersection(source, other) :
                    method == "union" ? OGR_G_Union(source, other) : OGR_G_Difference(source, other);
    fileEnsure(CPLGetLastErrorType() < CE_Failure, "Geometry overlay failed");
    return output.result(runtime);
}
inline CPLJSONObject setVectorSpatialFilter(Runtime& runtime, OGRLayerH layer, const CPLJSONObject& args, bool rectangle) {
    auto native = OGRLayer::FromHandle(layer);
    if (rectangle) {
        auto minX = CPLAtof(args["minX"].ToString().c_str()), minY = CPLAtof(args["minY"].ToString().c_str());
        auto maxX = CPLAtof(args["maxX"].ToString().c_str()), maxY = CPLAtof(args["maxY"].ToString().c_str());
        require(std::isfinite(minX) && std::isfinite(minY) && std::isfinite(maxX) && std::isfinite(maxY) && minX <= maxX && minY <= maxY,
                "Spatial filter rectangle must be finite and ordered");
        require(OGR_FD_GetGeomFieldCount(OGR_L_GetLayerDefn(layer)) > 0, "Layer has no geometry field");
        return value(native->SetSpatialFilterRect(minX, minY, maxX, maxY));
    }
    auto filter = args["filter"].GetType() == CPLJSONObject::Type::Null ? nullptr : runtime.arg(args["filter"]);
    if (filter) {
        require(OGR_FD_GetGeomFieldCount(OGR_L_GetLayerDefn(layer)) > 0, "Layer has no geometry field");
        vectorSameCrs(OGR_L_GetSpatialRef(layer), OGR_G_GetSpatialReference(filter));
    }
    return value(native->SetSpatialFilter(OGRGeometry::FromHandle(filter)));
}

inline CPLJSONObject layerOverlay(Runtime& runtime, OGRLayerH source, const CPLJSONObject& args) {
    auto method = runtime.arg(args["method"]), result = runtime.arg(args["result"]);
    require(source != method, "Overlay inputs require independent native layer cursors; obtaining another handle to the same layer does not create one");
    require(result != source && result != method, "Overlay result must differ from both inputs");
    auto operation = args.GetString("operation");
    require(operation == "intersection" || operation == "union" || operation == "symDifference" || operation == "identity" || operation == "update" || operation == "clip" || operation == "erase", "Unsupported layer overlay operation");
    vectorSameCrs(OGR_L_GetSpatialRef(source), OGR_L_GetSpatialRef(method));
    vectorSameCrs(OGR_L_GetSpatialRef(source), OGR_L_GetSpatialRef(result));
    vectorSameCrs(OGR_L_GetSpatialRef(method), OGR_L_GetSpatialRef(result));
    require(OGR_L_TestCapability(result, OLCSequentialWrite) && OGR_L_TestCapability(result, OLCCreateField), "Overlay result must support writes and schema creation");
    auto filter = OGRLayer::FromHandle(result)->GetAttrQueryString();
    require((!filter || !*filter) && !OGR_L_GetSpatialFilter(result), "Clear result filters before overlay");
    auto schema = OGR_L_GetLayerDefn(result);
    require(schema && OGR_FD_GetFieldCount(schema) == 0 && OGR_L_GetFeatureCount(result, TRUE) == 0, "Overlay result must have no attribute fields or features");
    for (const auto& item : runtime.handles) {
        const auto& entry = item.second;
        require(entry.type != "Feature" || OGR_F_GetDefnRef(entry.pointer) != schema, "Close all result Features before overlay");
        if (entry.type == "Layer" && entry.owned) {
            auto parent = runtime.handles.find(entry.parent);
            require(parent == runtime.handles.end() || parent->second.pointer != OGR_L_GetDataset(result), "Close active SQL result layers on the output dataset before overlay");
        }
    }
    FileOptions options(CPLJSONArray{}); std::set<std::string> seen;
    const bool bothFields = operation == "intersection" || operation == "union" || operation == "symDifference" || operation == "identity";
    const bool prepared = operation == "intersection" || operation == "union" || operation == "identity";
    for (auto entry : args.GetArray("arguments")) {
        auto text = entry.ToString(); auto equal = text.find('=');
        require(text.find('\0') == std::string::npos && equal != std::string::npos && equal > 0, "Overlay option requires KEY=VALUE");
        auto key = text.substr(0, equal), val = text.substr(equal + 1);
        require(seen.insert(key).second, "Duplicate overlay option");
        const bool prefix = bothFields && (key == "INPUT_PREFIX" || key == "METHOD_PREFIX");
        require(prefix || key == "SKIP_FAILURES" || key == "PROMOTE_TO_MULTI" || (prepared && (key == "USE_PREPARED_GEOMETRIES" || key == "KEEP_LOWER_DIMENSION_GEOMETRIES")) || (operation == "intersection" && key == "PRETEST_CONTAINMENT"), "Unsupported option for this layer overlay");
        require(prefix || val == "YES" || val == "NO", "Overlay boolean option must be YES or NO");
        options.add(text);
    }
    runtime.invalidatePointer(schema);
    if (operation == "intersection") return value(OGR_L_Intersection(source, method, result, options.list, progress, activeProgress));
    if (operation == "union") return value(OGR_L_Union(source, method, result, options.list, progress, activeProgress));
    if (operation == "symDifference") return value(OGR_L_SymDifference(source, method, result, options.list, progress, activeProgress));
    if (operation == "identity") return value(OGR_L_Identity(source, method, result, options.list, progress, activeProgress));
    if (operation == "update") return value(OGR_L_Update(source, method, result, options.list, progress, activeProgress));
    if (operation == "clip") return value(OGR_L_Clip(source, method, result, options.list, progress, activeProgress));
    return value(OGR_L_Erase(source, method, result, options.list, progress, activeProgress));
}
