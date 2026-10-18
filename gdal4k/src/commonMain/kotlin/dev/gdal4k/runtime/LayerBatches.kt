package dev.gdal4k.runtime

import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

enum class LayerOverlayOperation(val code: String) {
    Intersection("intersection"), Union("union"), SymDifference("symDifference"), Identity("identity"),
    Update("update"), Clip("clip"), Erase("erase"),
}

suspend fun Layer.overlayTo(method: Layer, result: Layer, operation: LayerOverlayOperation, vararg arguments: String): Int =
    overlayTo(method, result, operation.code, arguments.asList())

enum class FeatureWriteMode { Create, Replace, Upsert }
/** On a native nonzero code, completed features remain written; the failing feature may be modified.
 * No input handle is closed. A transport/validation exception propagates with earlier writes retained.
 */
data class FeatureBatchWriteResult(val completed: Int, val errorCode: Int) { val successful: Boolean get() = errorCode == 0 }

private suspend fun closeBatch(features: List<Feature>, primary: Throwable? = null) = withContext(NonCancellable) {
    var failure: Throwable? = null
    for (feature in features) try { feature.close() } catch (error: Throwable) {
        val previous = failure; if (previous == null) failure = error else previous.addSuppressed(error)
    }
    val error = failure
    if (error != null) { if (primary == null) throw error else primary.addSuppressed(error) }
}

/** Reads up to limit from the current filtered cursor. Empty means EOF. Caller owns every returned
 * Feature and must close them. Errors/cancellation close the partially collected batch, but do not
 * rewind the cursor. The GDAL backend distinguishes native read errors from EOF.
 */
suspend fun Layer.readFeatureBatch(limit: Int): List<Feature> {
    require(limit > 0) { "Batch limit must be positive" }
    val features = mutableListOf<Feature>()
    try {
        repeat(limit) {
            currentCoroutineContext().ensureActive()
            features += getNextFeature() ?: return features
        }
        return features
    } catch (error: Throwable) { closeBatch(features, error); throw error }
}

/** Iterates bounded batches, closing their Features after each callback, including on error/cancel.
 * Clone a Feature within the callback to retain it. Filters remain active. reset=true rewinds first;
 * false starts at the current cursor. Do not concurrently iterate/mutate this layer from another task.
 */
suspend fun Layer.forEachFeatureBatch(batchSize: Int = 256, reset: Boolean = true, block: suspend (List<Feature>) -> Unit) {
    require(batchSize > 0) { "Batch size must be positive" }
    if (reset) resetReading()
    while (true) {
        val batch = readFeatureBatch(batchSize); if (batch.isEmpty()) return
        var failure: Throwable? = null
        try { currentCoroutineContext().ensureActive(); block(batch) } catch (error: Throwable) { failure = error; throw error }
        finally { closeBatch(batch, failure) }
    }
}

/** Sequential writes with native GDAL semantics, not a bulk driver transaction. Stops at the first
 * nonzero code; earlier writes remain. Create may assign/modify caller Feature FIDs. Use
 * Dataset.withTransaction and throw on an unsuccessful result when rollback is desired.
 */
suspend fun Layer.writeFeatureBatch(features: List<Feature>, mode: FeatureWriteMode = FeatureWriteMode.Create): FeatureBatchWriteResult {
    for ((index, feature) in features.withIndex()) {
        currentCoroutineContext().ensureActive()
        val code = when (mode) { FeatureWriteMode.Create -> createFeature(feature); FeatureWriteMode.Replace -> setFeature(feature); FeatureWriteMode.Upsert -> upsertFeature(feature) }
        if (code != 0) return FeatureBatchWriteResult(index, code)
    }
    return FeatureBatchWriteResult(features.size, 0)
}

/** Owns a transaction only if start succeeds. Commits after a successful block; attempts rollback
 * on block/cancellation/commit failure, preserving the original error. Driver support and transaction
 * scope apply; nested/emulated transactions and commit/rollback errors are not hidden. Dataset and
 * Features remain caller-owned. Flush newly created schema before starting: deferred driver DDL
 * can be rolled back while cached layer handles remain stale. Structural changes/rollback recovery
 * remain driver-specific. A returned nonzero result does not itself throw: check it in block.
 */
suspend fun <T> Dataset.withTransaction(force: Boolean = false, block: suspend () -> T): T {
    check(startTransaction(force) == 0) { "Cannot start dataset transaction" }
    try {
        val value = block()
        currentCoroutineContext().ensureActive()
        check(commitTransaction() == 0) { "Cannot commit dataset transaction" }
        return value
    } catch (error: Throwable) {
        withContext(NonCancellable) {
            try { check(rollbackTransaction() == 0) { "Cannot roll back dataset transaction" } }
            catch (rollback: Throwable) { error.addSuppressed(rollback) }
        }
        throw error
    }
}
