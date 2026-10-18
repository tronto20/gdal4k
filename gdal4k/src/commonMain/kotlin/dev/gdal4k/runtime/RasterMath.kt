package dev.gdal4k.runtime

import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.sqrt

/** Processing windows bound raster buffers; the zone accumulator still grows with zone count. */
data class RasterChunkSize(val width: Int = 256, val height: Int = 256) {
    init { require(width > 0 && height > 0) { "Raster chunk dimensions must be positive" } }
}

/** Half-open range [minimum, maximum). Infinite bounds are allowed, overlapping ranges are not. */
data class RasterClass(val minimum: Double, val maximum: Double, val value: Double)

/** Population standard deviation over finite, unmasked samples. Values are raw, not scaled. */
data class RasterZoneStatistics(val count: Long, val minimum: Double, val maximum: Double, val sum: Double, val mean: Double, val standardDeviation: Double)

private val mathTypes = setOf(GdalDataType.Byte, GdalDataType.Int8, GdalDataType.UInt16, GdalDataType.Int16,
    GdalDataType.UInt32, GdalDataType.Int32, GdalDataType.Float16, GdalDataType.Float32, GdalDataType.Float64)

private suspend fun rasterMathShape(inputs: List<Band>): Pair<Int, Int> {
    require(inputs.isNotEmpty()) { "At least one input band is required" }
    val width = inputs.first().getXSize(); val height = inputs.first().getYSize()
    require(width > 0 && height > 0) { "Raster dimensions must be positive" }
    for (band in inputs) {
        require(band.getXSize() == width && band.getYSize() == height) { "Raster dimensions differ" }
        require(band.getRasterDataType() in mathTypes) { "Raster math supports real types through UInt32/Int32 and Float64; Int64/UInt64 and complex bands require explicit conversion" }
    }
    return width to height
}

private suspend fun rasterMathNoData(target: Band, value: Double) {
    val type = target.getRasterDataType()
    require(type in mathTypes) { "Unsupported raster math target type" }
    val range = when (type) {
        GdalDataType.Byte -> 0.0 to 255.0
        GdalDataType.Int8 -> -128.0 to 127.0
        GdalDataType.UInt16 -> 0.0 to 65535.0
        GdalDataType.Int16 -> -32768.0 to 32767.0
        GdalDataType.UInt32 -> 0.0 to 4294967295.0
        GdalDataType.Int32 -> -2147483648.0 to 2147483647.0
        else -> null
    }
    if (range != null) require(value.isFinite() && value >= range.first && value <= range.second && value % 1.0 == 0.0) { "Integer output requires a representable finite NoData value" }
    else {
        require(!value.isInfinite()) { "Output NoData must be finite or NaN" }
        val maximum = when (type) { GdalDataType.Float16 -> 65504.0; GdalDataType.Float32 -> Float.MAX_VALUE.toDouble(); else -> Double.MAX_VALUE }
        require(value.isNaN() || abs(value) <= maximum) { "Output NoData exceeds target precision" }
    }
}

private data class MathMask(val band: Band?, val noData: Double?, val floating: Boolean)

private suspend fun <T> withMathMasks(inputs: List<Band>, block: suspend (List<MathMask>) -> T): T {
    val masks = mutableListOf<MathMask>(); var failure: Throwable? = null
    try {
        for (band in inputs) {
            val flags = band.getMaskFlags(); val type = band.getRasterDataType()
            val floating = type in setOf(GdalDataType.Float16, GdalDataType.Float32, GdalDataType.Float64)
            val rawNoData = band.getNoDataValue()
            val noData = rawNoData?.let { if (type == GdalDataType.Float32 || type == GdalDataType.Float16) it.toFloat().toDouble() else if (!floating && it.isFinite()) it.toLong().toDouble() else it }
            // GDAL's PAM band may cache ALL_VALID or a generated NoData mask after metadata
            // changes. Recompute the single-band NoData condition from fresh metadata.
            val mask = if (flags == 8 || flags and 1 != 0) null else requireNotNull(band.getMaskBand())
            masks += MathMask(mask, noData, floating)
        }
        return block(masks)
    } catch (error: Throwable) { failure = error; throw error }
    finally { withContext(NonCancellable) {
        var cleanupFailure: Throwable? = null
        for (mask in masks) try { mask.band?.close() } catch (error: Throwable) {
            if (cleanupFailure == null) cleanupFailure = error else cleanupFailure.addSuppressed(error)
        }
        val original = failure; val secondary = cleanupFailure
        if (secondary != null) { if (original == null) throw secondary else original.addSuppressed(secondary) }
    } }
}

private suspend fun mathWindows(width: Int, height: Int, chunk: RasterChunkSize, block: suspend (BandRasterIOOptions, Int) -> Unit) {
    var y = 0
    while (y < height) {
        val rows = min(chunk.height, height - y); var x = 0
        while (x < width) {
            currentCoroutineContext().ensureActive()
            val columns = min(chunk.width, width - x)
            require(columns.toLong() * rows <= Int.MAX_VALUE) { "Window is too large; select smaller chunks" }
            block(BandRasterIOOptions(xOffset = x, yOffset = y, xSize = columns, ySize = rows), columns * rows)
            x += columns
        }
        y += rows
    }
}

private suspend fun readMathWindow(inputs: List<Band>, masks: List<MathMask>, window: BandRasterIOOptions, count: Int): Pair<List<DoubleArray>, BooleanArray> {
    val valid = BooleanArray(count) { true }
    val values = inputs.mapIndexed { index, band ->
        val buffer = DoubleArray(count); check(band.readRaster(window, buffer) == 0) { "Raster math input read failed" }
        val policy = masks[index]; val mask = policy.band
        if (mask != null) {
            val bytes = ByteArray(count); check(mask.readRaster(window, bytes) == 0) { "Raster math mask read failed" }
            for (i in bytes.indices) if (bytes[i] == 0.toByte()) valid[i] = false
        }
        for (i in buffer.indices) {
            val value = buffer[i]; val noData = policy.noData
            val isNoData = noData != null && (value == noData || (policy.floating && abs(value - noData) < 1.1920928955078125e-7 * abs(value + noData) * 2))
            if (!value.isFinite() || isNoData) valid[i] = false
        }
        buffer
    }
    return values to valid
}

/** Applies a pixel function to aligned bands using bounded windows. All GDAL masks and nonfinite
 * input values propagate to [outputNoData]; nonfinite callback results do too. Per-band NoData
 * metadata is snapshotted fresh and floating comparisons use GDAL's Float32-epsilon rule. The callback receives
 * a reused input array: copy it if retaining samples. Inputs and target remain caller-owned.
 * Dimensions must match; CRS/geotransform alignment is the caller's responsibility. No metadata,
 * resampling, reprojection or scale/offset conversion is supplied. Target writes follow GDAL type
 * conversion rules. Set target NoData only after success, so in-place inputs retain their old masks
 * during processing. Failure/cancellation leaves partial writes. Cancellation is checked each row.
 */
suspend fun rasterCalculate(inputs: List<Band>, target: Band, chunk: RasterChunkSize = RasterChunkSize(), outputNoData: Double = Double.NaN, transform: (DoubleArray) -> Double) {
    val (width, height) = rasterMathShape(inputs)
    require(target.getXSize() == width && target.getYSize() == height) { "Target dimensions differ" }
    require(target.getAccess() == 1) { "Raster math target is read-only" }
    rasterMathNoData(target, outputNoData)
    withMathMasks(inputs) { masks -> mathWindows(width, height, chunk) { window, count ->
        val (values, valid) = readMathWindow(inputs, masks, window, count)
        val sample = DoubleArray(inputs.size); val result = DoubleArray(count) { outputNoData }
        val context = currentCoroutineContext()
        for (i in 0 until count) {
            if (i % window.xSize == 0) context.ensureActive()
            if (valid[i]) {
                for (band in values.indices) sample[band] = values[band][i]
                val value = transform(sample); if (value.isFinite()) result[i] = value
            }
        }
        check(target.writeRaster(window, result) == 0) { "Raster math target write failed" }
    } }
    check(target.setNoDataValue(outputNoData) == 0) { "Cannot set raster math output NoData" }
}

/** Reclassifies disjoint half-open ranges, ordered internally. Unmatched values and invalid
 * samples become outputNoData. Supports in-place processing with the same policy as rasterCalculate.
 */
suspend fun Band.reclassifyTo(target: Band, classes: List<RasterClass>, chunk: RasterChunkSize = RasterChunkSize(), outputNoData: Double = Double.NaN) {
    val ordered = classes.sortedBy { it.minimum }
    require(ordered.isNotEmpty()) { "At least one class is required" }
    for (range in ordered) require(!range.minimum.isNaN() && !range.maximum.isNaN() && range.minimum < range.maximum && range.value.isFinite()) { "Invalid reclassification range or value" }
    for (i in 1 until ordered.size) require(ordered[i - 1].maximum <= ordered[i].minimum) { "Reclassification ranges overlap" }
    rasterCalculate(listOf(this), target, chunk, outputNoData) { input ->
        val value = input[0]; var low = 0; var high = ordered.lastIndex; var result = outputNoData
        while (low <= high) {
            val middle = low + (high - low) / 2; val range = ordered[middle]
            if (value < range.minimum) high = middle - 1 else if (value >= range.maximum) low = middle + 1 else { result = range.value; break }
        }
        result
    }
}

private class ZoneAccumulator {
    var count = 0L; var minimum = Double.POSITIVE_INFINITY; var maximum = Double.NEGATIVE_INFINITY
    var mean = 0.0; var m2 = 0.0; var sum = 0.0; var correction = 0.0
    fun add(value: Double) {
        count++; minimum = minOf(minimum, value); maximum = maxOf(maximum, value)
        val delta = value - mean; mean += delta / count; m2 += delta * (value - mean)
        val adjusted = value - correction; val next = sum + adjusted; correction = (next - sum) - adjusted; sum = next
        check(mean.isFinite() && m2.isFinite() && sum.isFinite()) { "Zone statistics overflow Double precision" }
    }
    fun snapshot() = RasterZoneStatistics(count, minimum, maximum, sum, mean, sqrt(maxOf(0.0, m2 / count)))
}

/** Statistics per aligned integer zone label. Masks of both bands and nonfinite values are
 * excluded; zero is a normal zone unless masked. Zone labels must be integral and exactly
 * representable as signed Int32/UInt32, even if stored in a floating band. Missing zones are omitted.
 * CRS/grid alignment is caller-owned. Memory is proportional to window size plus distinct zones.
 * No polygon area weighting is implied; rasterize polygon IDs onto the same grid when needed.
 * Accumulator overflow throws instead of returning nonfinite statistics.
 */
suspend fun Band.zonalStatistics(zones: Band, chunk: RasterChunkSize = RasterChunkSize()): Map<Long, RasterZoneStatistics> {
    val inputs = listOf(this, zones); val (width, height) = rasterMathShape(inputs)
    val result = linkedMapOf<Long, ZoneAccumulator>()
    withMathMasks(inputs) { masks -> mathWindows(width, height, chunk) { window, count ->
        val (values, valid) = readMathWindow(inputs, masks, window, count)
        val context = currentCoroutineContext()
        for (i in 0 until count) {
            if (i % window.xSize == 0) context.ensureActive()
            if (valid[i]) {
                val zone = values[1][i]
                require(zone % 1.0 == 0.0 && zone >= -2147483648.0 && zone <= 4294967295.0) { "Zone labels must be Int32/UInt32 integers" }
                result.getOrPut(zone.toLong()) { ZoneAccumulator() }.add(values[0][i])
            }
        }
    } }
    return result.mapValues { it.value.snapshot() }
}
