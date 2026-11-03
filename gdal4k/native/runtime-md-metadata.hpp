// Included after runtime-md.hpp: public typed values never expose GDAL ABI layout.
#pragma once

inline CPLJSONObject mdNoDataValue(GDALMDArrayH array, const CPLJSONObject& args, bool write) {
    MDType type(GDALMDArrayGetDataType(array));
    if (!write) {
        auto raw = GDALMDArrayGetRawNoDataValue(array);
        // The raw pointer is borrowed. In particular a pointer to a null string
        // is a registered null-text NoData, whereas nullptr means no NoData.
        return raw ? objectValue(mdGet(static_cast<const char*>(raw), type)) : nullValue();
    }
    auto value = args["value"];
    if (value.GetType() == CPLJSONObject::Type::Null)
        return g4k::value(GDALMDArraySetRawNoDataValue(array, nullptr) != 0);
    MDBuffer buffer(type, 1);
    mdPut(buffer.data.data(), type, value);
    // GDAL copies the value, including dynamic string/compound members. The
    // temporary buffer therefore retains ownership until this call completes.
    return g4k::value(GDALMDArraySetRawNoDataValue(array, buffer.data.data()) != 0);
}

inline double mdMetadataDouble(const CPLJSONObject& input) {
    const auto type = input.GetType();
    if (type == CPLJSONObject::Type::String) {
        const auto text = mdViewString(input, "Metadata value must be a number or canonical nonfinite string");
        if (text == "NaN") return std::numeric_limits<double>::quiet_NaN();
        if (text == "Infinity") return std::numeric_limits<double>::infinity();
        if (text == "-Infinity") return -std::numeric_limits<double>::infinity();
        require(false, "Invalid nonfinite metadata value");
    }
    require(type == CPLJSONObject::Type::Integer || type == CPLJSONObject::Type::Long ||
                type == CPLJSONObject::Type::Double,
            "Metadata value must be a number or canonical nonfinite string");
    const auto value = input.ToDouble();
    require(std::isfinite(value), "Nonfinite metadata value must use a canonical string");
    return value;
}

inline GDALDataType mdMetadataStorageType(const CPLJSONObject& input) {
    require(input.GetType() == CPLJSONObject::Type::Integer ||
                input.GetType() == CPLJSONObject::Type::Long,
            "Metadata storage type must be an integer");
    const auto code = input.ToLong();
    require(code >= 0 && code < GDT_TypeCount, "Invalid metadata storage type");
    return static_cast<GDALDataType>(code);
}

inline CPLJSONObject mdSetNumericMetadata(GDALMDArrayH array, const CPLJSONObject& args, bool scale) {
    const auto number = mdMetadataDouble(args[scale ? "scale" : "offset"]);
    const auto storage = mdMetadataStorageType(args["storageType"]);
    CPLErrorReset();
    const bool accepted = (scale ? GDALMDArraySetScaleEx(array, number, storage) :
                                  GDALMDArraySetOffsetEx(array, number, storage)) != 0;
    fileEnsure(CPLGetLastErrorType() < CE_Failure,
               scale ? "Cannot set multidimensional scale metadata" :
                       "Cannot set multidimensional offset metadata");
    // The native result does not guarantee persistence or a backing attribute type change.
    return g4k::value(accepted);
}

inline CPLJSONObject mdSetSpatialRef(Runtime& runtime, GDALMDArrayH array, const CPLJSONObject& args) {
    const auto input = args["spatialRef"];
    OGRSpatialReferenceH spatialRef = nullptr;
    if (input.GetType() != CPLJSONObject::Type::Null) {
        require(input.GetType() == CPLJSONObject::Type::Object,
                "Spatial reference must be a handle object or null");
        require(mdViewString(input["type"], "Handle type must be a string") == "SpatialReference",
                "Metadata input has the wrong handle type");
        const auto encoded = mdViewString(input["handle"], "Handle ID must be a string");
        unsigned long long id = 0;
        const auto parsed = std::from_chars(encoded.data(), encoded.data() + encoded.size(), id);
        require(parsed.ec == std::errc() && parsed.ptr == encoded.data() + encoded.size() &&
                    id > 0 && std::to_string(id) == encoded,
                "Metadata input has an invalid handle ID");
        spatialRef = static_cast<OGRSpatialReferenceH>(runtime.get(id, "SpatialReference"));
    }
    CPLErrorReset();
    const bool accepted = GDALMDArraySetSpatialRef(array, spatialRef) != 0;
    fileEnsure(CPLGetLastErrorType() < CE_Failure, "Cannot set multidimensional spatial reference");
    // Borrow only during this call; cloning/serialization and clear behavior remain driver-native.
    return g4k::value(accepted);
}
