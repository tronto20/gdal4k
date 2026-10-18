package dev.gdal4k.runtime

import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext

/** A bounded scan of one layer. The parent layer/dataset owns its native lifetime. Closing any
 * alias of that layer or its dataset also closes the scan. Close explicitly, including after EOF.
 * Only one stream per native layer can be active. Concurrent mutation/iteration is rejected.
 */
interface FeatureColumnStream {
    /** Returns detached column arrays, or null at EOF. A failure/cancellation closes the stream.
     * Batch row count is at most the requested size. Responses above 64 MiB fail explicitly;
     * lower the batch size for large values. A single GDAL value may still require a large native
     * allocation. No zero-copy or hard native-memory bound is promised.
     */
    suspend fun read(): FeatureColumnBatch?
    suspend fun close()
}

/** Caller-owned arrays remain usable after stream/layer/dataset close. Columns retain Arrow
 * order and names, including the FID column. Names need not be unique. Geometry columns contain
 * ISO WKB with [FeatureColumn.geometry] set and a detached CRS WKT when known.
 * Null validity combines unset and explicit null.
 * Array slots where valid=false have no value; no coercion or feature editing is provided.
 */
data class FeatureColumnBatch(val rowCount: Int, val columns: List<FeatureColumn>)

sealed class FeatureColumn(val name: String, val valid: BooleanArray, val geometry: Boolean) {
    class Integers(name: String, valid: BooleanArray, val values: IntArray) : FeatureColumn(name, valid, false)
    class Longs(name: String, valid: BooleanArray, val values: LongArray) : FeatureColumn(name, valid, false)
    class Reals(name: String, valid: BooleanArray, val values: DoubleArray) : FeatureColumn(name, valid, false)
    class Booleans(name: String, valid: BooleanArray, val values: BooleanArray) : FeatureColumn(name, valid, false)
    class Strings(name: String, valid: BooleanArray, val values: Array<String>) : FeatureColumn(name, valid, false)
    class Binary(name: String, valid: BooleanArray, val values: Array<ByteArray>, geometry: Boolean,
                 val spatialReferenceWkt: String? = null) : FeatureColumn(name, valid, geometry)
}

suspend inline fun <T : FeatureColumnStream, R> T.use(block: (T) -> R): R =
    try { block(this) } finally { withContext(NonCancellable) { close() } }

/** Scoped scan. Detached batches may be retained; native arrays are released before callbacks. */
suspend fun Layer.forEachColumnBatch(batchSize: Int = 1024, block: suspend (FeatureColumnBatch) -> Unit) {
    openColumnStream(batchSize).use { stream ->
        while (true) {
            currentCoroutineContext().ensureActive()
            val batch = stream.read() ?: return
            block(batch)
        }
    }
}
