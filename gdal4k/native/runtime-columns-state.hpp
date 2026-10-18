#pragma once
#include <ogr_recordbatch.h>

struct VectorColumnStream {
    struct Column { std::string name, format, kind; bool geometry; std::string crsWkt; };
    ArrowArrayStream stream{};
    ArrowSchema schema{};
    OGRLayerH layer = nullptr;
    GDALDatasetH dataset = nullptr;
    std::vector<Column> columns;
    int batchSize = 0;
    bool eof = false;
    ~VectorColumnStream() {
        if (schema.release) schema.release(&schema);
        if (stream.release) stream.release(&stream);
    }
};
