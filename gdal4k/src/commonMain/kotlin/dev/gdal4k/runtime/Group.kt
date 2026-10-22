package dev.gdal4k.runtime

/** A GDAL handle. Close with use; children cannot outlive their parent. */
interface Group {
    suspend fun close()
    suspend fun getName(): String
    suspend fun getFullName(): String
    suspend fun getGroupNames(options: List<String> = emptyList()): List<String>
    suspend fun getArrayNames(options: List<String> = emptyList()): List<String>
    /**
     * Dimensions registered with this group according to the driver. Array-local axes may appear
     * only in [MDArray.getDimensions], so an empty group list does not imply that its arrays are scalar.
     * Close each returned handle; it cannot outlive this group. Options are driver specific.
     */
    suspend fun getDimensions(options: List<String> = emptyList()): List<Dimension>
    suspend fun openGroup(name: String, options: List<String> = emptyList()): Group?
    suspend fun openArray(name: String, options: List<String> = emptyList()): MDArray?
    suspend fun createGroup(name: String, options: List<String> = emptyList()): Group?
    suspend fun createDimension(name: String, size: Long, type: String = "", direction: String = "", options: List<String> = emptyList()): Dimension?
    suspend fun createArray(name: String, dimensions: List<Dimension>, dataType: Int, options: List<String> = emptyList()): MDArray?
    suspend fun getAttribute(name: String): Attribute?
    suspend fun getAttributes(options: List<String> = emptyList()): List<Attribute>
    /** Empty dimensions create a scalar attribute. */
    suspend fun createAttribute(name: String, dimensions: LongArray, dataType: MDDataType, options: List<String> = emptyList()): Attribute?
    suspend fun deleteAttribute(name: String, options: List<String> = emptyList()): Boolean
    suspend fun createArray(name: String, dimensions: List<Dimension>, dataType: MDDataType, options: List<String> = emptyList()): MDArray?

}

suspend inline fun <T : Group, R> T.use(block: (T) -> R): R {
    try { return block(this) } finally {
        kotlinx.coroutines.withContext(kotlinx.coroutines.NonCancellable) { close() }
    }
}
