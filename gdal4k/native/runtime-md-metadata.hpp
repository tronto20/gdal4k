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
