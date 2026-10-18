package dev.gdal4k.internal

import dev.gdal4k.*
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlinx.serialization.json.*

private suspend fun checkpoint(index: Int) {
    if (index and 16383 == 0) { currentCoroutineContext().ensureActive(); yield() }
}
internal expect suspend fun platformPackRaster(array: Any): ByteArray?
internal expect suspend fun platformUnpackRaster(bytes: ByteArray, array: Any): Boolean

private fun ByteArray.putBits(offset: Int, width: Int, bits: Long) {
    val lower=bits.toInt()
    for (j in 0 until minOf(width,4)) this[offset+j]=(lower ushr (8*j)).toByte()
    if (width==8) {
        val upper=(bits ushr 32).toInt()
        for (j in 0 until 4) this[offset+4+j]=(upper ushr (8*j)).toByte()
    }
}
private fun ByteArray.bits(offset: Int, width: Int): Long {
    var lower=0
    for (j in 0 until minOf(width,4)) lower=lower or ((this[offset+j].toInt() and 255) shl (8*j))
    if(width<=4) return lower.toLong() and 0xffffffffL
    var upper=0
    for (j in 0 until 4) upper=upper or ((this[offset+4+j].toInt() and 255) shl (8*j))
    return (upper.toLong() shl 32) or (lower.toLong() and 0xffffffffL)
}
internal fun rasterArraySize(array: Any): Int = when (array) {
    is ByteArray -> array.size; is ShortArray -> array.size; is IntArray -> array.size
    is LongArray -> array.size; is FloatArray -> array.size; is DoubleArray -> array.size
    else -> error("Unsupported raster array")
}
internal fun rasterArrayWidth(array: Any): Int = when (array) {
    is ByteArray -> 1; is ShortArray -> 2; is IntArray, is FloatArray -> 4
    is LongArray, is DoubleArray -> 8; else -> error("Unsupported raster array")
}
internal suspend fun packRaster(array: Any): ByteArray {
    val width = rasterArrayWidth(array); val size = rasterArraySize(array)
    require(size <= Int.MAX_VALUE / width) { "Raster byte buffer exceeds addressable array size" }
    currentCoroutineContext().ensureActive()
    platformPackRaster(array)?.let { return it }
    val bytes = ByteArray(size * width)
    when (array) {
        is ByteArray -> for (offset in array.indices step 262144) {
            checkpoint(offset)
            array.copyInto(bytes,offset,offset,offset+minOf(262144,array.size-offset))
        }
        is ShortArray -> for (i in array.indices) { checkpoint(i); bytes.putBits(i*2, 2, array[i].toLong()) }
        is IntArray -> for (i in array.indices) { checkpoint(i); bytes.putBits(i*4, 4, array[i].toLong()) }
        is LongArray -> for (i in array.indices) { checkpoint(i); bytes.putBits(i*8, 8, array[i]) }
        is FloatArray -> for (i in array.indices) { checkpoint(i); bytes.putBits(i*4, 4, array[i].toRawBits().toLong()) }
        is DoubleArray -> for (i in array.indices) { checkpoint(i); bytes.putBits(i*8, 8, array[i].toRawBits()) }
    }
    currentCoroutineContext().ensureActive()
    return bytes
}
internal suspend fun unpackRaster(bytes: ByteArray, array: Any) {
    val width = rasterArrayWidth(array)
    require(bytes.size % width == 0 && bytes.size / width <= rasterArraySize(array)) { "Invalid binary raster response length" }
    if (platformUnpackRaster(bytes,array)) return
    val size = bytes.size / width
    // Decode into private storage. Cancellation/failure never exposes a partially decoded read.
    when (array) {
        is ByteArray -> {
            // Raw bytes already are private decoded storage; avoid a second staging array/copy.
            currentCoroutineContext().ensureActive()
            bytes.copyInto(array)
        }
        is ShortArray -> {
            val decoded = ShortArray(size)
            for (i in 0 until size) { checkpoint(i); decoded[i] = bytes.bits(i*2,2).toShort() }
            currentCoroutineContext().ensureActive()
            decoded.copyInto(array)
        }
        is IntArray -> {
            val decoded = IntArray(size)
            for (i in 0 until size) { checkpoint(i); decoded[i] = bytes.bits(i*4,4).toInt() }
            currentCoroutineContext().ensureActive()
            decoded.copyInto(array)
        }
        is LongArray -> {
            val decoded = LongArray(size)
            for (i in 0 until size) { checkpoint(i); decoded[i] = bytes.bits(i*8,8) }
            currentCoroutineContext().ensureActive()
            decoded.copyInto(array)
        }
        is FloatArray -> {
            val decoded = FloatArray(size)
            for (i in 0 until size) { checkpoint(i); decoded[i] = Float.fromBits(bytes.bits(i*4,4).toInt()) }
            currentCoroutineContext().ensureActive()
            decoded.copyInto(array)
        }
        is DoubleArray -> {
            val decoded = DoubleArray(size)
            for (i in 0 until size) { checkpoint(i); decoded[i] = Double.fromBits(bytes.bits(i*8,8)) }
            currentCoroutineContext().ensureActive()
            decoded.copyInto(array)
        }
    }
}
internal suspend fun RuntimeHandle.rasterCall(type: String, method: String, buffer: Any,
    parameters: Map<String, JsonElement>, write: Boolean): Int {
    checkOpen()
    currentCoroutineContext().ensureActive()
    val size = rasterArraySize(buffer)
    val metadata = parameters + ("bufferSize" to JsonPrimitive(size))
    // Old external/custom engines retain their JSON protocol. No new file-size cap is imposed.
    if (!engine.supportsBinaryRaster || size > Int.MAX_VALUE / rasterArrayWidth(buffer)) {
        val args = if (write) metadata + ("buffer" to encode(buffer)) else metadata
        val response = invoke(type, method, args).jsonObject
        val code = response.getValue("code").jsonPrimitive.int
        if (!write && code == 0) {
            val values = response.getValue("buffer").jsonArray
            when (buffer) {
                is ByteArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toInt().toByte() }
                is ShortArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toInt().toShort() }
                is IntArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toInt() }
                is LongArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toLong() }
                is FloatArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toFloat() }
                is DoubleArray -> values.forEachIndexed { i,v -> buffer[i] = v.jsonPrimitive.content.toDouble() }
            }
        }
        return code
    }
    val binary = GdalBinaryCall(prepare = { if (write) packRaster(buffer) else null }, complete = { value, bytes ->
        if (!write && value.jsonObject.getValue("code").jsonPrimitive.int == 0) unpackRaster(bytes, buffer)
    })
    return engine.invoke(type, runtimeId, method, metadata, binary = binary).jsonObject.getValue("code").jsonPrimitive.int
}
