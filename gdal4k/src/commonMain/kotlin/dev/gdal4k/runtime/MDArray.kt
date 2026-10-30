package dev.gdal4k.runtime

/** A GDAL handle. Close with use; children cannot outlive their parent. */
interface MDArray {
    suspend fun close()
    suspend fun getName(): String
    suspend fun getFullName(): String
    suspend fun getDimensions(): List<Dimension>
    /**
     * Lazy aliased view with the requested output dimension order.
     *
     * Each entry selects a zero-based source axis. Every source axis must appear exactly once;
     * repeated -1 entries insert size-one axes. Other negative, duplicate, missing or out-of-range
     * source axes fail. Scalars accept an empty map or maps containing only -1.
     *
     * The view and its descendants depend on this array and its ancestors. Closing the view does
     * not close its source or siblings. Typed value I/O preserves the stored type and aliases the
     * source when the underlying array permits writes; numeric read/write retain Float64 conversion.
     * Metadata setters may be unsupported by a view even when its source supports them.
     *
     * CRS data-axis mappings follow the reordered axes, including valid negative mappings.
     * A CRS copy returned by getSpatialRef remains independently owned.
     * The map is snapshotted before queued engine work. Cancellation is checked at operation
     * boundaries; this operation does not supply a native progress callback.
     */
    suspend fun transpose(newAxisToOldAxis: IntArray): MDArray
    /**
     * Lazy numeric view that applies the source's scale and offset to each value.
     *
     * Nonidentity views capture those metadata values and expose Float64 or CFloat64 values;
     * complex real and imaginary components each use component * scale + offset. Writes apply
     * the inverse conversion when the source permits them. Binary64 arithmetic can round large
     * integers, and zero or nonfinite metadata can produce NaN or infinite results.
     *
     * A nonidentity view initially has NaN NoData when its source has NoData. Remapping requires
     * both view and current source NoData; clearing either disables it. Source presence is initially
     * captured, so adding source NoData later does not by itself enable the original view's NoData.
     * Exact Int64/UInt64 sentinel comparisons precede conversion, and missing writes restore the
     * exact source value. Complex missingness follows the real component; a real NaN write is also
     * missing while remapping is enabled. A finite view NoData may collide with a valid physical
     * value. Clearing this view's NoData changes the view, not its source.
     *
     * Effective identity metadata (scale 1 and offset 0) returns another owned wrapper for the
     * original numeric array, retaining its type and shared metadata. Nonnumeric arrays fail.
     * Every returned wrapper depends on this array and its ancestors; closing it leaves its
     * source and siblings usable. A CRS copy returned by getSpatialRef remains independent.
     * Cancellation is checked at operation boundaries; there is no native progress callback.
     */
    suspend fun getUnscaled(): MDArray
    /** Independent CRS copy, usable after this array closes; null means no CRS. Close the copy.
     * Native failures throw instead of returning null.
     */
    suspend fun getSpatialRef(): SpatialReference?
    /** Detached natural block sizes in dimension order. Zero means no hint; scalars return an empty list. */
    suspend fun getBlockSize(): List<ULong>
    /** Detached structural KEY=VALUE entries; empty when the driver provides no information. */
    suspend fun getStructuralInfo(): List<String>
    /** Typed NoData, preserving exact integers and complex/string values; null means unset. */
    suspend fun getNoDataValue(): MDValue?
    /** null removes NoData. A non-null value must match the array's exact stored type. */
    suspend fun setNoDataValue(value: MDValue?): Boolean
    suspend fun getUnit(): String
    suspend fun setUnit(unit: String): Boolean
    /** null means no explicit scale/offset. Values are metadata, not applied to read/write. */
    suspend fun getScale(): Double?
    suspend fun getOffset(): Double?
    /** Explicit scale and its storage type, or null when unset. Native failures throw. */
    suspend fun getScaleEx(): MDNumericMetadata?
    /** Explicit offset and its storage type, or null when unset. Native failures throw. */
    suspend fun getOffsetEx(): MDNumericMetadata?
    suspend fun setScale(scale: Double): Boolean
    suspend fun setOffset(offset: Double): Boolean
    /** GDAL numeric type; non-numeric arrays return GdalDataType.Unknown. */
    suspend fun getDataType(): Int
    /** Numeric slices are converted to Float64, in row-major dimension order.
     * Pass one start and count per dimension; scalar arrays use empty arrays.
     * Use readValues/writeValues to preserve integer precision or complex values.
     */
    suspend fun read(start: LongArray, count: LongArray): DoubleArray
    suspend fun write(start: LongArray, count: LongArray, values: DoubleArray): Boolean
    suspend fun getAttribute(name: String): Attribute?
    suspend fun getAttributes(options: List<String> = emptyList()): List<Attribute>
    /** Empty dimensions create a scalar attribute. */
    suspend fun createAttribute(name: String, dimensions: LongArray, dataType: MDDataType, options: List<String> = emptyList()): Attribute?
    suspend fun deleteAttribute(name: String, options: List<String> = emptyList()): Boolean
    suspend fun getExtendedDataType(): MDDataType
    /** Reads only the requested window, in row-major order, preserving the stored type. */
    suspend fun readValues(start: LongArray, count: LongArray): List<MDValue>
    suspend fun writeValues(start: LongArray, count: LongArray, values: List<MDValue>): Boolean

}

suspend inline fun <T : MDArray, R> T.use(block: (T) -> R): R {
    try { return block(this) } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
