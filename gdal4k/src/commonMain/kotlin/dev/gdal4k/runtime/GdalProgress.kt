package dev.gdal4k.runtime

import kotlin.coroutines.AbstractCoroutineContextElement
import kotlin.coroutines.CoroutineContext
import kotlinx.coroutines.withContext

/** A sampled GDAL callback, with fraction in [0, 1]. Each native call starts its own sequence.
 * Drivers may report sparse progress or none between the initial/final events. Messages are hints.
 */
data class GdalProgress(val operation: String, val fraction: Double, val message: String)

/** Context used by the bundled runtime. The observer runs serially per native call, on an
 * unspecified dispatcher, and must be short, thread-safe and must not reenter the same runtime.
 * Return false to cooperatively stop the current call; throw to preserve your own error.
 */
class GdalProgressContext(val observer: (GdalProgress) -> Boolean) : AbstractCoroutineContextElement(Key) {
    companion object Key : CoroutineContext.Key<GdalProgressContext>
}

/** Observe native work without changing the operation's signature. Coroutine cancellation and a
 * false observer result request GDAL cancellation. The call waits for native completion/cleanup;
 * drivers without intermediate callbacks can only stop at the next callback or call boundary.
 * Caller inputs stay owned; already written output/cursor/metadata changes are not rolled back.
 * Fast operations may provide just the final sampled event. Different runtimes may call the same
 * observer concurrently. Use a Flow/Channel or dispatch UI updates instead of blocking the callback.
 */
suspend fun <T> withGdalProgress(observer: (GdalProgress) -> Boolean, block: suspend () -> T): T =
    withContext(GdalProgressContext(observer)) { block() }
