#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <vector>
#include "progress.hpp"
namespace g4k {
struct BinaryRaster {
    const unsigned char* input;
    size_t length;
    std::vector<unsigned char> output;
};
inline thread_local BinaryRaster* activeBinary = nullptr;
inline void binaryCheckpoint() {
    if (activeProgress && !progress(0.01, "Raster buffer transfer", activeProgress))
        throw std::runtime_error("GDAL operation cancelled");
}
inline void validateBinary(size_t count,size_t width,bool write) {
    if (!activeBinary) return;
    if (count>static_cast<size_t>(std::numeric_limits<int>::max())/width)
        throw std::runtime_error("Binary raster response exceeds addressable array size");
    if (write && (count>activeBinary->length/width || (count&&!activeBinary->input)))
        throw std::runtime_error("Binary raster input is too small");
}
template<class T> void binaryInput(T* data, size_t count) {
    if (count > activeBinary->length / sizeof(T) || (count && !activeBinary->input))
        throw std::runtime_error("Binary raster input is too small");
    const uint16_t endian = 1;
    auto* bytes = reinterpret_cast<unsigned char*>(data);
    for (size_t offset=0; offset<count*sizeof(T);) {
        binaryCheckpoint();
        const auto n=std::min<size_t>(262144,count*sizeof(T)-offset);
        std::memcpy(bytes+offset,activeBinary->input+offset,n); offset+=n;
    }
    if (*reinterpret_cast<const unsigned char*>(&endian)!=1)
        for(size_t i=0;i<count;++i) std::reverse(bytes+i*sizeof(T),bytes+(i+1)*sizeof(T));
}
template<class T> void binaryOutput(const T* data, size_t count) {
    activeBinary->output.resize(count*sizeof(T));
    const auto* bytes=reinterpret_cast<const unsigned char*>(data);
    for (size_t offset=0; offset<count*sizeof(T);) {
        binaryCheckpoint();
        const auto n=std::min<size_t>(262144,count*sizeof(T)-offset);
        std::memcpy(activeBinary->output.data()+offset,bytes+offset,n); offset+=n;
    }
    const uint16_t endian = 1;
    if (*reinterpret_cast<const unsigned char*>(&endian)!=1)
        for(size_t i=0;i<count;++i) std::reverse(activeBinary->output.begin()+i*sizeof(T),activeBinary->output.begin()+(i+1)*sizeof(T));
}
}
