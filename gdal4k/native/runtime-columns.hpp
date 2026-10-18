// Included inside g4k, after Runtime. No Arrow library: GDAL owns the C stream/arrays.
constexpr size_t columnResponseLimit = 64 * 1024 * 1024;
inline bool columnGeometry(const ArrowSchema& schema) {
    // Arrow C metadata is length-prefixed, not a NUL-terminated text string. The producer is
    // trusted GDAL; bound all declared counts/lengths before walking its metadata.
    if (!schema.metadata) return false;
    const char* cursor = schema.metadata;
    size_t total = 0;
    auto integer = [&]() {
        int32_t number; std::memcpy(&number, cursor, 4); cursor += 4; total += 4;
        require(number >= 0 && number <= 1024 * 1024 && total <= 1024 * 1024, "Invalid Arrow metadata size");
        return number;
    };
    auto text = [&]() {
        const auto size = integer(); total += size;
        require(total <= 1024 * 1024, "Arrow metadata exceeds bridge limit");
        std::string result(cursor, size); cursor += size; return result;
    };
    const auto count = integer(); require(count <= 1024, "Too many Arrow metadata entries");
    bool geometry = false;
    for (int i = 0; i < count; ++i) {
        const auto key = text(), value = text();
        if (key == "ARROW:extension:name") {
            require(value == "ogc.wkb" || value == "geoarrow.wkb", "Unsupported Arrow extension; use Feature iteration");
            geometry = true;
        }
    }
    return geometry;
}
inline std::string columnKind(const std::string& format) {
    if (format == "b") return "bool";
    if (format == "c" || format == "C" || format == "s" || format == "S" || format == "i") return "int";
    if (format == "I" || format == "l") return "long";
    if (format == "f" || format == "g") return "real";
    if (format == "u" || format == "U") return "string";
    if (format == "z" || format == "Z") return "binary";
    throw std::runtime_error("Unsupported Arrow column type '" + format + "'; use Feature iteration");
}
inline CPLJSONObject openColumnStream(Runtime& runtime, OGRLayerH layer, const CPLJSONObject& args, unsigned long long parent) {
    const auto batchSize = args.GetInteger("batchSize");
    require(batchSize > 0 && batchSize <= 65536, "Column batch size must be between 1 and 65536");
    runtime.requireNoColumnStream(layer);
    auto state = std::make_unique<VectorColumnStream>(); state->layer = layer;
    state->dataset = OGR_L_GetDataset(layer); state->batchSize = batchSize;
    // Some virtual/result layer implementations do not expose GetDataset; use its handle owner.
    for (auto id = parent; !state->dataset && id;) {
        const auto& entry = runtime.handles.at(id);
        if (entry.type == "Dataset") state->dataset = entry.pointer;
        id = entry.parent;
    }
    Strings options(CPLJSONArray{});
    const auto batchOption = "MAX_FEATURES_IN_BATCH=" + std::to_string(batchSize);
    options.list = CSLAddString(options.list, batchOption.c_str());
    options.list = CSLAddString(options.list, "INCLUDE_FID=YES");
    options.list = CSLAddString(options.list, "GEOMETRY_ENCODING=WKB");
    OGR_L_ResetReading(layer); CPLErrorReset();
    fileEnsure(OGR_L_GetArrowStream(layer, &state->stream, options.data()) != 0, "Cannot open GDAL Arrow stream");
    fileEnsure(state->stream.get_schema && state->stream.get_next && state->stream.release, "Incomplete GDAL Arrow stream");
    fileEnsure(state->stream.get_schema(&state->stream, &state->schema) == 0, "Cannot read GDAL Arrow schema");
    require(state->schema.release && state->schema.format && std::string(state->schema.format) == "+s" &&
            !state->schema.dictionary && state->schema.n_children >= 0 && state->schema.n_children <= 65536,
            "Unsupported Arrow root schema");
    require(!state->schema.n_children || state->schema.children, "Missing Arrow column schema");
    const auto definition = OGR_L_GetLayerDefn(layer); int geometryIndex = 0;
    for (int64_t i = 0; i < state->schema.n_children; ++i) {
        const auto* column = state->schema.children[i];
        require(column && column->format && !column->dictionary && column->n_children == 0,
                "Nested/dictionary Arrow columns require Feature iteration");
        auto format = std::string(column->format), kind = columnKind(format);
        const bool geometry = columnGeometry(*column);
        require(!geometry || kind == "binary", "Geometry must use WKB binary encoding");
        std::string crsWkt;
        if (geometry) {
            // GDAL emits non-ignored geometry fields in layer-definition order. Keep their CRS
            // alongside detached WKB, which cannot carry an SRS itself. No reprojection occurs.
            const auto count = OGR_FD_GetGeomFieldCount(definition);
            while (geometryIndex < count && OGR_GFld_IsIgnored(OGR_FD_GetGeomFieldDefn(definition, geometryIndex))) ++geometryIndex;
            require(geometryIndex < count, "Arrow geometry schema differs from layer definition");
            const auto field = OGR_FD_GetGeomFieldDefn(definition, geometryIndex++);
            const auto srs = OGR_GFld_GetSpatialRef(field);
            if (srs) {
                char* text = nullptr;
                const auto code = OSRExportToWkt(srs, &text);
                if (code != OGRERR_NONE) { CPLFree(text); fileEnsure(false, "Cannot export geometry column CRS"); }
                crsWkt = text ? text : ""; CPLFree(text);
            }
        }
        state->columns.push_back({column->name ? column->name : "", format, kind, geometry, crsWkt});
    }
    auto result = runtime.result(state.get(), "FeatureColumnStream", true, parent); state.release(); return result;
}
inline void columnAppend(const void* data, size_t size) {
    auto& output = activeBinary->output;
    require(size <= columnResponseLimit - output.size(), "Column response exceeds 64 MiB; use a smaller batch");
    const auto start = output.size(); output.resize(start + size);
    if (size) std::memcpy(output.data() + start, data, size);
}
template<class T> inline void columnNumber(T number) {
    unsigned char bytes[sizeof(T)]; std::memcpy(bytes, &number, sizeof(T));
    const uint16_t endian = 1;
    if (*reinterpret_cast<const unsigned char*>(&endian) != 1) std::reverse(bytes, bytes + sizeof(T));
    columnAppend(bytes, sizeof(T));
}
template<class T> inline T columnScalar(const void* buffer, size_t index) {
    require(buffer && index <= SIZE_MAX / sizeof(T), "Missing/oversized Arrow value buffer");
    T result; std::memcpy(&result, static_cast<const unsigned char*>(buffer) + index * sizeof(T), sizeof(T)); return result;
}
inline CPLJSONObject readColumnBatch(VectorColumnStream& state) {
    require(activeBinary && activeBinary->length == 0, "Column reads require binary transport with no input payload");
    if (state.eof) return nullValue();
#if defined(__EMSCRIPTEN__)
    // GDAL 3.12.4's optimized GPKG path can start an asynchronous std::thread even with a
    // single consumer (e.g. FIDs have holes). Our Web engine is threadless. Select GDAL's own
    // generic Arrow producer for GPKG during this call; never change caller/global config.
    struct ThreadlessGeoPackageArrow {
        char** previous = CPLGetThreadLocalConfigOptions();
        ThreadlessGeoPackageArrow() { CPLSetThreadLocalConfigOption("OGR_GPKG_STREAM_BASE_IMPL", "YES"); }
        ~ThreadlessGeoPackageArrow() { CPLSetThreadLocalConfigOptions(previous); CSLDestroy(previous); }
    } threadlessGeoPackageArrow;
#endif
    struct Array { ArrowArray value{}; ~Array() { if (value.release) value.release(&value); } } array;
    while (true) {
        binaryCheckpoint(); CPLErrorReset();
        const auto code = state.stream.get_next(&state.stream, &array.value);
        fileEnsure(code == 0, "Cannot read GDAL Arrow batch");
        if (!array.value.release) { state.eof = true; return nullValue(); }
        if (array.value.length != 0) break;
        // A valid empty Arrow batch is not EOF. Release it before requesting another one.
        array.value.release(&array.value); array.value = {};
    }
    const auto rows = array.value.length;
    require(rows > 0 && rows <= state.batchSize && array.value.offset >= 0 && array.value.null_count == 0 &&
            array.value.n_children == static_cast<int64_t>(state.columns.size()), "Invalid Arrow batch shape");
    require(state.columns.empty() || array.value.children, "Missing Arrow column arrays");
    CPLJSONObject batch; batch.Add("rows", static_cast<int>(rows)); CPLJSONArray columns;
    for (size_t i = 0; i < state.columns.size(); ++i) {
        binaryCheckpoint();
        const auto& schema = state.columns[i]; const auto* values = array.value.children[i];
        require(values && values->offset >= 0 && values->length >= rows &&
                array.value.offset <= values->length - rows && values->offset <= INT64_MAX - values->length &&
                values->n_buffers >= 1 && values->buffers && !values->dictionary, "Invalid Arrow column shape");
        const auto offset = values->offset + array.value.offset;
        require(static_cast<uint64_t>(offset + rows) <= SIZE_MAX, "Arrow column exceeds addressable memory");
        const auto* validity = static_cast<const unsigned char*>(values->buffers[0]);
        require(values->null_count <= 0 || validity, "Missing Arrow validity bitmap");
        CPLJSONObject column; column.Add("name", schema.name); column.Add("kind", schema.kind); column.Add("geometry", schema.geometry);
        if (schema.crsWkt.empty()) column.AddNull("crsWkt"); else column.Add("crsWkt", schema.crsWkt);
        for (int64_t row = 0; row < rows; ++row) {
            if ((row & 1023) == 0) binaryCheckpoint();
            const auto index = static_cast<size_t>(offset + row);
            const bool valid = !validity || ((validity[index / 8] >> (index % 8)) & 1);
            const unsigned char flag = valid ? 1 : 0; columnAppend(&flag, 1);
            if (!valid) continue;
            require(values->n_buffers >= 2 && values->buffers[1], "Missing Arrow data buffer");
            const auto* data = values->buffers[1]; const auto& format = schema.format;
            if (schema.kind == "bool") {
                const unsigned char value = (static_cast<const unsigned char*>(data)[index / 8] >> (index % 8)) & 1;
                columnAppend(&value, 1);
            } else if (schema.kind == "int") {
                const int32_t value = format == "c" ? columnScalar<int8_t>(data, index) : format == "C" ? columnScalar<uint8_t>(data, index) :
                    format == "s" ? columnScalar<int16_t>(data, index) : format == "S" ? columnScalar<uint16_t>(data, index) : columnScalar<int32_t>(data, index);
                columnNumber(value);
            } else if (schema.kind == "long") {
                const int64_t value = format == "I" ? static_cast<int64_t>(columnScalar<uint32_t>(data, index)) : columnScalar<int64_t>(data, index);
                columnNumber(value);
            } else if (schema.kind == "real") {
                const double value = format == "f" ? static_cast<double>(columnScalar<float>(data, index)) : columnScalar<double>(data, index);
                columnNumber(value);
            } else {
                require(values->n_buffers >= 3, "Missing Arrow variable-width buffers");
                const auto start = format == "U" || format == "Z" ? columnScalar<int64_t>(data, index) : static_cast<int64_t>(columnScalar<int32_t>(data, index));
                const auto end = format == "U" || format == "Z" ? columnScalar<int64_t>(data, index + 1) : static_cast<int64_t>(columnScalar<int32_t>(data, index + 1));
                require(start >= 0 && end >= start && end - start <= INT_MAX && static_cast<uint64_t>(end) <= SIZE_MAX,
                        "Invalid Arrow variable-width offset");
                const auto size = static_cast<size_t>(end - start);
                require(!size || values->buffers[2], "Missing Arrow string/binary data");
                columnNumber(static_cast<int32_t>(size));
                columnAppend(size ? static_cast<const unsigned char*>(values->buffers[2]) + start : nullptr, size);
            }
        }
        columns.Add(column);
    }
    batch.Add("columns", columns); CPLJSONObject result; result.Add("value", batch); return result;
}
