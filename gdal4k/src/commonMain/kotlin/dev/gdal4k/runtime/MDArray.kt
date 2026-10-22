package dev.gdal4k.runtime

/** A GDAL handle. Close with use; children cannot outlive their parent. */
interface MDArray {
    suspend fun close()
    suspend fun getName(): String
    suspend fun getFullName(): String
    suspend fun getDimensions(): List<Dimension>
    /** Typed NoData, preserving exact integers and complex/string values; null means unset. */
    suspend fun getNoDataValue(): MDValue?
    /** null removes NoData. A non-null value must match the array's exact stored type. */
    suspend fun setNoDataValue(value: MDValue?): Boolean
    suspend fun getUnit(): String
    suspend fun setUnit(unit: String): Boolean
    /** null means no explicit scale/offset. Values are metadata, not applied to read/write. */
    suspend fun getScale(): Double?
    suspend fun getOffset(): Double?
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
