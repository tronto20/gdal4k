package dev.gdal4k.internal

import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield

private val littleEndian: Boolean = js("new Uint8Array(new Uint16Array([1]).buffer)[0] === 1")
private fun typedBytes(value: Any): ByteArray = js("new Int8Array(value.buffer, value.byteOffset, value.byteLength)")
private fun nativeView(array: Any): ByteArray? {
    if (!littleEndian) return null
    return when (array) {
        is ShortArray, is IntArray, is FloatArray, is DoubleArray -> typedBytes(array)
        else -> null // Byte uses the common bulk-copy path; Kotlin/JS LongArray stores boxed Longs.
    }
}
internal actual suspend fun platformPackRaster(array: Any): ByteArray? {
    val view=nativeView(array) ?: return null
    val packed=ByteArray(view.size)
    for (offset in view.indices step 262144) {
        currentCoroutineContext().ensureActive();yield()
        view.copyInto(packed,offset,offset,offset+minOf(262144,view.size-offset))
    }
    currentCoroutineContext().ensureActive()
    return packed
}
internal actual suspend fun platformUnpackRaster(bytes: ByteArray, array: Any): Boolean {
    val view=nativeView(array) ?: return false
    // Incoming bytes already are private decoded storage. On a little-endian host no
    // per-sample conversion or second typed staging array is necessary.
    currentCoroutineContext().ensureActive();yield();currentCoroutineContext().ensureActive()
    bytes.copyInto(view)
    return true
}
