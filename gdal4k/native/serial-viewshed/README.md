# GDAL 3.12.4 serial Viewshed executor for ordinary Wasm

These unmodified files are copied from OSGeo/gdal tag v3.12.4. sha256.json pins
all six originals. The headers preserve the original class ABI. CMake compiles
a locally generated serial variant of viewshed_executor.cpp only on Emscripten.
It replaces the three job queues with immediate calls and constructs an empty
worker pool. The calculation, scan order within each half, height correction,
read/write, output and progress code are unchanged. Up/down scans operate on
independent line buffers and distinct raster rows; left/right scans write
distinct halves of a line, so sequential execution preserves their data flow.

GDAL 3.12.4 unconditionally starts four threads in this executor. Its failed
thread creation leaves jobs waiting forever in a non-pthread Wasm module.
The bridge supplies all executor symbols before the GDAL archive is searched,
so that archive's original executor object is not selected. No installed GDAL
library or shared dependency prefix is modified, and no pthread/SIMD/GC feature
is enabled. Host engines continue to link the original executor.

Source: https://github.com/OSGeo/gdal/tree/v3.12.4/alg/viewshed
CPL header: https://github.com/OSGeo/gdal/blob/v3.12.4/port/cpl_worker_thread_pool.h
