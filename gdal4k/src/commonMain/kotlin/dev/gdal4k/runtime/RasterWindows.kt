package dev.gdal4k.runtime

import dev.gdal4k.RasterWindow
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlin.math.min

/** Visits tightly packed raw band windows in row-major order using a caller-owned buffer.
 * The same array is passed to every callback. Only its first window.width * window.height
 * samples belong to that window; excess capacity is unchanged. Copy samples if retaining them.
 * Edge windows are clipped to [window] or the band extent. No resampling, scale/offset,
 * NoData replacement, CRS conversion or metadata changes are performed. GDAL converts raw
 * samples to the selected array type. Inputs, this band and the buffer stay caller-owned.
 *
 * The callback is awaited before reading the next window, with cancellation checks and a
 * scheduling yield between windows. A failed read does not invoke its callback. Earlier
 * callback effects are not rolled back. The traversal is not an atomic dataset snapshot:
 * do not close/change the band, mutate the buffer concurrently, or retain it without copying.
 * Memory for this API is bounded by buffer capacity; GDAL/driver/transport caches and copies,
 * callback allocations and any retained samples are additional. This is not zero-copy I/O.
 */
suspend fun Band.forEachRasterWindow(
    buffer: ByteArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, ByteArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

suspend fun Band.forEachRasterWindow(
    buffer: ShortArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, ShortArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

suspend fun Band.forEachRasterWindow(
    buffer: IntArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, IntArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

suspend fun Band.forEachRasterWindow(
    buffer: LongArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, LongArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

suspend fun Band.forEachRasterWindow(
    buffer: FloatArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, FloatArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

suspend fun Band.forEachRasterWindow(
    buffer: DoubleArray,
    windowWidth: Int = 256,
    windowHeight: Int = 256,
    window: RasterWindow? = null,
    action: suspend (RasterWindow, DoubleArray) -> Unit,
) = visitRasterWindows(buffer.size, windowWidth, windowHeight, window) { region ->
    check(readRaster(BandRasterIOOptions(region.x, region.y, region.width, region.height), buffer) == 0) { "GDAL raster window read failed" }
    currentCoroutineContext().ensureActive()
    action(region, buffer)
}

private suspend fun Band.visitRasterWindows(
    capacity: Int, windowWidth: Int, windowHeight: Int, window: RasterWindow?,
    visit: suspend (RasterWindow) -> Unit,
) {
    currentCoroutineContext().ensureActive()
    require(windowWidth > 0 && windowHeight > 0) { "Window dimensions must be positive" }
    val width = getXSize(); val height = getYSize()
    require(width > 0 && height > 0) { "Band dimensions must be positive" }
    val region = window ?: RasterWindow(0, 0, width, height)
    require(region.x.toLong() + region.width <= width && region.y.toLong() + region.height <= height) { "Window is outside the band" }
    val maximumSamples = min(windowWidth, region.width).toLong() * min(windowHeight, region.height)
    require(maximumSamples <= capacity.toLong()) { "Buffer is too small for a traversal window" }
    val endX = region.x + region.width; val endY = region.y + region.height
    var y = region.y
    while (y < endY) {
        val rows = min(windowHeight, endY - y); var x = region.x
        while (x < endX) {
            currentCoroutineContext().ensureActive()
            val columns = min(windowWidth, endX - x)
            visit(RasterWindow(x, y, columns, rows))
            currentCoroutineContext().ensureActive()
            yield()
            x += columns
        }
        y += rows
    }
}
