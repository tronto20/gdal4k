package dev.gdal4k.internal

import dev.gdal4k.GdalBinaryCall
import dev.gdal4k.runtime.MDValue
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlinx.serialization.json.*

private suspend fun mdCheckpoint(index: Int) {
    if (index and 1023 == 0) { currentCoroutineContext().ensureActive(); yield() }
}

// Numeric MDValue components are already decimal strings in the public contract.
// Length-prefixed ASCII preserves the engine's canonical formatting and parser/range
// rules, without per-value JSON objects or a Float64 conversion of exact integers.
private fun ByteArray.mdLength(offset: Int, value: Int) {
    for (i in 0..3) this[offset + i] = (value ushr (i * 8)).toByte()
}
private suspend fun packMDValues(values: List<MDValue.Numeric>, size: Int): ByteArray {
    val bytes = ByteArray(size); var offset = 0
    suspend fun component(text: String?) {
        bytes.mdLength(offset, text?.length ?: -1); offset += 4
        if (text != null) for (i in text.indices) {
            if (i > 0 && i and 262143 == 0) { currentCoroutineContext().ensureActive(); yield() }
            require(text[i].code < 128) { "Numeric component must be ASCII" }
            bytes[offset++] = text[i].code.toByte()
        }
    }
    for ((index, value) in values.withIndex()) { mdCheckpoint(index); component(value.real); component(value.imaginary) }
    currentCoroutineContext().ensureActive(); return bytes
}
internal suspend fun decodeMDValues(value: JsonElement, bytes: ByteArray): List<MDValue> {
    if (value is JsonArray) {
        require(bytes.isEmpty()) { "Unexpected payload with structured MD fallback" }
        return value.map(::decodeMDValue)
    }
    val metadata = value.jsonObject
    require(metadata["binaryMDValues"]?.jsonPrimitive?.boolean == true) { "Invalid numeric MD response" }
    val count = metadata.getValue("count").jsonPrimitive.long
    require(count >= 0 && count <= bytes.size / 8) { "Invalid numeric MD response count" }
    var offset = 0
    suspend fun component(nullable: Boolean): String? {
        require(bytes.size - offset >= 4) { "Truncated numeric component length" }
        var size = 0
        for (i in 0..3) size = size or ((bytes[offset++].toInt() and 255) shl (i * 8))
        if (nullable && size == -1) return null
        require(size >= 0 && size <= bytes.size - offset) { "Invalid numeric component size" }
        for (i in 0 until size) {
            if (i > 0 && i and 262143 == 0) { currentCoroutineContext().ensureActive(); yield() }
            require(bytes[offset + i] >= 0) { "Numeric component must be ASCII" }
        }
        val text = bytes.decodeToString(offset, offset + size); offset += size
        return text
    }
    val result = ArrayList<MDValue>(count.toInt())
    repeat(count.toInt()) { index ->
        mdCheckpoint(index); result.add(MDValue.Numeric(requireNotNull(component(false)), component(true)))
    }
    require(offset == bytes.size) { "Trailing numeric MD response bytes" }
    currentCoroutineContext().ensureActive(); return result
}
internal suspend fun RuntimeHandle.mdWriteValuesCall(type: String, method: String,
    parameters: Map<String, JsonElement>, values: List<MDValue>): Boolean {
    checkOpen(); currentCoroutineContext().ensureActive()
    if (!engine.supportsBinaryMD) return invoke(type, method, parameters + ("values" to encode(values))).jsonPrimitive.boolean
    val numeric = values.takeIf { it.all { value -> value is MDValue.Numeric } }?.map { it as MDValue.Numeric }
    val size = numeric?.sumOf { 8L + it.real.length + (it.imaginary?.length ?: 0) }
    if (numeric == null || size!! > Int.MAX_VALUE) {
        return invoke(type, method, parameters + ("values" to encode(values))).jsonPrimitive.boolean
    }
    val metadata = parameters + ("numericValueCount" to JsonPrimitive(values.size))
    val result = engine.invoke(type, runtimeId, method, metadata, binary = GdalBinaryCall(
        prepare = { packMDValues(numeric, size.toInt()) },
        complete = { value, bytes ->
            require(bytes.isEmpty() && value is JsonPrimitive && value.booleanOrNull != null) { "Invalid numeric MD write reply" }
        }))
    return result.jsonPrimitive.boolean
}
internal suspend fun RuntimeHandle.mdReadValuesCall(type: String, method: String,
    parameters: Map<String, JsonElement>): List<MDValue> {
    checkOpen(); currentCoroutineContext().ensureActive()
    if (!engine.supportsBinaryMD) return invoke(type, method, parameters).jsonArray.map(::decodeMDValue)
    var decoded: List<MDValue>? = null
    engine.invoke(type, runtimeId, method, parameters, binary = GdalBinaryCall(prepare = { null },
        complete = { value, bytes -> decoded = decodeMDValues(value, bytes) }))
    return requireNotNull(decoded)
}
internal suspend fun RuntimeHandle.mdWriteRasterCall(start: LongArray, count: LongArray,
    values: DoubleArray): Boolean {
    checkOpen(); currentCoroutineContext().ensureActive()
    var size = 1L
    for (c in count) { require(c > 0 && c <= Int.MAX_VALUE && size <= Int.MAX_VALUE / c) { "Invalid MD slice count" }; size *= c }
    val parameters = mapOf("start" to encode(start), "count" to encode(count))
    if (!engine.supportsBinaryMD || size > Int.MAX_VALUE / 8) {
        return invoke("MDArray", "write_LongArray_LongArray_DoubleArray", parameters + ("values" to encode(values))).jsonPrimitive.boolean
    }
    require(values.size.toLong() == size) { "MD slice values size does not match count" }
    return engine.invoke("MDArray", runtimeId, "write_LongArray_LongArray_DoubleArray", parameters + ("valueCount" to JsonPrimitive(size)),
        binary = GdalBinaryCall(prepare = { packRaster(values) },
            complete = { value, bytes -> require(bytes.isEmpty() && value.jsonPrimitive.booleanOrNull != null) { "Invalid MD write response" } })).jsonPrimitive.boolean
}
internal suspend fun RuntimeHandle.mdReadRasterCall(start: LongArray, count: LongArray): DoubleArray {
    checkOpen(); currentCoroutineContext().ensureActive()
    var size = 1L
    for (c in count) { require(c > 0 && c <= Int.MAX_VALUE && size <= Int.MAX_VALUE / c) { "Invalid MD slice count" }; size *= c }
    val parameters = mapOf("start" to encode(start), "count" to encode(count))
    if (!engine.supportsBinaryMD || size > Int.MAX_VALUE / 8) return invoke("MDArray", "read_LongArray_LongArray", parameters)
        .jsonArray.map { it.jsonPrimitive.content.toDouble() }.toDoubleArray()
    var decoded: DoubleArray? = null
    engine.invoke("MDArray", runtimeId, "read_LongArray_LongArray", parameters + ("valueCount" to JsonPrimitive(size)),
        binary = GdalBinaryCall(prepare = { null }, complete = { value, bytes ->
            require(value.jsonPrimitive.long == size && bytes.size.toLong() == size * 8) { "Invalid MD read response length" }
            decoded = DoubleArray(size.toInt()).also { unpackRaster(bytes, it) }
        }))
    return requireNotNull(decoded)
}
