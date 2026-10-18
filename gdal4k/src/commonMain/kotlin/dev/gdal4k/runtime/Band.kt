package dev.gdal4k.runtime

/** Access hints; LargestChunkPossible may be combined with a traversal value. */
object BlockAccessPattern {
    const val Unknown = 0
    const val Random = 1
    const val TopToBottom = 2
    const val BottomToTop = 3
    const val LargestChunkPossible = 0x100
}

/**
 * Shared raster band handle.
 *
 * Provides band metadata, overview/mask access, and full-band or windowed raster I/O.
 */
interface Band {
    /** Replaces connected regions smaller than [threshold] with their largest neighbour.
     * Source and target must have identical dimensions. Source may equal target for in-place use.
     * Default mask excludes source NoData; an explicit mask overrides it. Floating values are
     * rounded to integers by GDAL. No georeferencing/metadata is copied to the target.
     * Returns GDAL's error code; target may be partially changed on failure.
     */
    suspend fun sieveTo(target: Band, threshold: Int, connectedness: Int = 4, mask: Band? = null, useDefaultMask: Boolean = true): Int

    /** Distances to nonzero source pixels, or VALUES=n[,n] targets. Distinct, equally sized
     * target must be writable. GDAL processes source values as Int32; use a classified band.
     * Options are KEY=VALUE: VALUES, DISTUNITS=PIXEL/GEO, MAXDIST, NODATA,
     * USE_INPUT_NODATA=YES/NO, FIXED_BUF_VAL. No metadata is copied to the target.
     * Returns GDAL's error code; partial writes belong to the caller.
     */
    suspend fun proximityTo(target: Band, arguments: List<String>): Int

    /** Modifies this band in place, filling zero pixels in [mask] (default: this band's mask).
     * Search distance is in pixels; zero searches the full raster. GDAL interpolates Float32
     * values and converts them to this band's type; scale/offset are not applied.
     * KEY=VALUE options: INTERPOLATION=INV_DIST/NEAREST, NODATA, TEMP_FILE_DRIVER.
     * Scratch defaults to MEM for portable execution. Select GTiff with a writable GDAL
     * temporary directory for large rasters. NODATA is ignored by GDAL's smoothing pass.
     * NoData metadata is retained. Returns the GDAL error code; partial writes remain.
     */
    suspend fun fillNoData(maxSearchDistance: Double = 100.0, smoothingIterations: Int = 0, mask: Band? = null, arguments: List<String>): Int

    /** Detached caller-owned copy, unaffected by later palette replacement. */
    suspend fun getColorTable(): ColorTable?
    /** Copies the table; null removes the palette if the driver supports it. */
    suspend fun setColorTable(table: ColorTable?): Int
    /** Borrowed table, with row-window reads; replacement invalidates all aliases. */
    suspend fun getDefaultRAT(): RasterAttributeTable?
    /** Copies the table; borrowed aliases are invalidated after successful replacement. */
    suspend fun setDefaultRAT(table: RasterAttributeTable?): Int
    suspend fun getActualBlockSize(blockX: Int, blockY: Int): IntArray
    /** Driver access hint using [BlockAccessPattern] values and flags. */
    suspend fun getSuggestedBlockAccessPattern(): Int
    /** Raw native pixel bytes, no conversion or resampling. Buffer holds the full nominal
     * block (blockWidth * blockHeight * bytesPerPixel). Native byte order applies.
     * Edge padding is zeroed on read; oversized buffer tails are unchanged.
     * Pending RasterIO writes are flushed before accessing the direct block.
     */
    suspend fun readBlock(blockX: Int, blockY: Int, buffer: ByteArray): Int
    suspend fun writeBlock(blockX: Int, blockY: Int, buffer: ByteArray): Int

    suspend fun close()

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: DoubleArray): Int

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: FloatArray): Int

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: LongArray): Int

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: IntArray): Int

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: ShortArray): Int

    suspend fun writeRaster(options: BandRasterIOOptions, buffer: ByteArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: DoubleArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: FloatArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: LongArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: IntArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: ShortArray): Int

    suspend fun readRaster(options: BandRasterIOOptions, buffer: ByteArray): Int

    suspend fun getBandNumber(): Int
    suspend fun getAccess(): Int
    suspend fun getDescription(): String
    suspend fun setDescription(description: String)
    suspend fun getMetadataDomainList(): List<String>
    suspend fun getMetadata(domain: String = ""): List<String>
    suspend fun setMetadata(metadata: List<String>, domain: String = ""): Int
    suspend fun getMetadataItem(name: String, domain: String = ""): String?
    /** Null deletes the item. XML domains retain GDAL's string-list representation. */
    suspend fun setMetadataItem(name: String, value: String?, domain: String = ""): Int

    /** Null means the driver has no explicitly defined value (GDAL's implicit scale/offset are 1/0). */
    suspend fun getScale(): Double?
    suspend fun setScale(scale: Double): Int
    suspend fun getOffset(): Double?
    suspend fun setOffset(offset: Double): Int
    suspend fun getMinimum(): Double?
    suspend fun getMaximum(): Double?
    /** Exact integer APIs require an Int64/UInt64 band respectively. */
    suspend fun getNoDataValueAsInt64(): Long?
    suspend fun setNoDataValueAsInt64(value: Long): Int
    suspend fun getNoDataValueAsUInt64(): ULong?
    suspend fun setNoDataValueAsUInt64(value: ULong): Int
    suspend fun fill(realValue: Double, imaginaryValue: Double = 0.0): Int

    /** Cached/cheap lookup by default. force=true may scan; approximate=true permits sampling. */
    suspend fun getStatistics(approximate: Boolean = false, force: Boolean = false): RasterStatistics?
    /** Always recomputes. Raw values exclude NoData; scale/offset are not applied. */
    suspend fun computeStatistics(approximate: Boolean = false): RasterStatistics
    suspend fun setStatistics(minimum: Double, maximum: Double, mean: Double, standardDeviation: Double): Int
    suspend fun computeRasterMinMax(approximate: Boolean = false): DoubleArray
    /** Upper bound is exclusive. Outliers may be folded into the first/last bucket. */
    suspend fun getHistogram(minimum: Double, maximum: Double, buckets: Int, includeOutOfRange: Boolean = false, approximate: Boolean = false): LongArray
    /** Null when no cached histogram exists and force is false. */
    suspend fun getDefaultHistogram(force: Boolean = false): RasterHistogram?
    suspend fun setDefaultHistogram(minimum: Double, maximum: Double, counts: LongArray): Int

    suspend fun getXSize(): Int

    suspend fun getYSize(): Int

    suspend fun getRasterDataType(): Int

    suspend fun getBlockSize(): IntArray

    suspend fun checksum(): Int

    suspend fun getColorInterpretation(): Int

    suspend fun setColorInterpretation(colorInterpretation: Int): Int

    /** Reacquire borrowed masks after a native NoData mutation attempt. Generated masks
     * are refreshed; explicit mask data is retained. Validation errors leave masks untouched.
     */
    suspend fun getNoDataValue(): Double?

    suspend fun setNoDataValue(value: Double): Int

    suspend fun deleteNoDataValue(): Int

    suspend fun getUnitType(): String

    suspend fun setUnitType(unitType: String): Int

    suspend fun getCategoryNames(): List<String>

    suspend fun setCategoryNames(names: List<String>): Int

    suspend fun getOverviewCount(): Int

    suspend fun getOverview(index: Int): Band?

    suspend fun getSampleOverview(sampleSize: Long): Band?

    suspend fun getMaskBand(): Band?

    suspend fun getMaskFlags(): Int

    suspend fun createMaskBand(flags: Int): Int

    suspend fun isMaskBand(): Boolean

    suspend fun flushCache()

    suspend fun readRaster(buffer: ByteArray): Int

    suspend fun readRaster(buffer: ShortArray): Int

    suspend fun readRaster(buffer: IntArray): Int

    suspend fun readRaster(buffer: LongArray): Int

    suspend fun readRaster(buffer: FloatArray): Int

    suspend fun readRaster(buffer: DoubleArray): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: ByteArray,
    ): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: ShortArray,
    ): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: IntArray,
    ): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: LongArray,
    ): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: FloatArray,
    ): Int

    suspend fun readRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: DoubleArray,
    ): Int

    suspend fun writeRaster(buffer: ByteArray): Int

    suspend fun writeRaster(buffer: ShortArray): Int

    suspend fun writeRaster(buffer: IntArray): Int

    suspend fun writeRaster(buffer: LongArray): Int

    suspend fun writeRaster(buffer: FloatArray): Int

    suspend fun writeRaster(buffer: DoubleArray): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: ByteArray,
    ): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: ShortArray,
    ): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: IntArray,
    ): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: LongArray,
    ): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: FloatArray,
    ): Int

    suspend fun writeRaster(
        xOffset: Int,
        yOffset: Int,
        xSize: Int,
        ySize: Int,
        buffer: DoubleArray,
    ): Int
}

suspend inline fun <T : Band, R> T.use(block: (T) -> R): R {
    try {
        return block(this)
    } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
