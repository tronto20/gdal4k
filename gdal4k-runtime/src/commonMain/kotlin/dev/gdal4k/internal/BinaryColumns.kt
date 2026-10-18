package dev.gdal4k.internal

import dev.gdal4k.GdalBinaryCall
import dev.gdal4k.runtime.FeatureColumn
import dev.gdal4k.runtime.FeatureColumnBatch
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlinx.serialization.json.*

/** Private version-3 wire format: columns in schema order, validity byte then present values.
 * Integers/doubles are little endian; strings/binary are Int32 length plus bytes. No native
 * pointers/Arrow ownership cross the bridge. Decode fully before returning the detached batch.
 */
internal suspend fun decodeColumnBatch(value: JsonElement, bytes: ByteArray): FeatureColumnBatch? {
    require(bytes.size <= 64 * 1024 * 1024) { "Oversized column payload" }
    if (value is JsonNull) { require(bytes.isEmpty()) { "Payload at column EOF" }; return null }
    val metadata = value.jsonObject
    val count = metadata.getValue("rows").jsonPrimitive.int
    val columns = metadata.getValue("columns").jsonArray
    require(count in 1..65536 && columns.size <= 65536 && count.toLong() * columns.size <= bytes.size) { "Invalid column batch shape" }
    var offset = 0
    fun byte(): Int {
        require(offset < bytes.size) { "Truncated column payload" }
        return bytes[offset++].toInt() and 255
    }
    fun integer(): Int {
        var result = 0
        repeat(4) { result = result or (byte() shl (it * 8)) }
        return result
    }
    fun long(): Long {
        var result = 0L
        repeat(8) { result = result or (byte().toLong() shl (it * 8)) }
        return result
    }
    fun data(): ByteArray {
        val length = integer()
        require(length >= 0 && length <= bytes.size - offset) { "Invalid column value length" }
        return bytes.copyOfRange(offset, offset + length).also { offset += length }
    }
    val decoded = ArrayList<FeatureColumn>(columns.size)
    for (item in columns) {
        currentCoroutineContext().ensureActive()
        val column = item.jsonObject
        val name = column.getValue("name").jsonPrimitive.content
        val kind = column.getValue("kind").jsonPrimitive.content
        val geometry = column.getValue("geometry").jsonPrimitive.boolean
        val crsWkt = column["crsWkt"]?.jsonPrimitive?.contentOrNull
        require(!geometry || kind == "binary") { "Invalid column geometry encoding" }
        require(geometry || crsWkt == null) { "CRS on a non-geometry column" }
        val valid = BooleanArray(count)
        val result: FeatureColumn = when (kind) {
            "int" -> FeatureColumn.Integers(name, valid, IntArray(count))
            "long" -> FeatureColumn.Longs(name, valid, LongArray(count))
            "real" -> FeatureColumn.Reals(name, valid, DoubleArray(count))
            "bool" -> FeatureColumn.Booleans(name, valid, BooleanArray(count))
            "string" -> FeatureColumn.Strings(name, valid, Array(count) { "" })
            "binary" -> FeatureColumn.Binary(name, valid, Array(count) { ByteArray(0) }, geometry, crsWkt)
            else -> error("Unknown column wire kind: $kind")
        }
        repeat(count) { row ->
            if (row and 1023 == 0) { currentCoroutineContext().ensureActive(); yield() }
            val present = byte()
            require(present in 0..1) { "Invalid column validity byte" }
            valid[row] = present == 1
            if (present == 1) when (result) {
                is FeatureColumn.Integers -> result.values[row] = integer()
                is FeatureColumn.Longs -> result.values[row] = long()
                is FeatureColumn.Reals -> result.values[row] = Double.fromBits(long())
                is FeatureColumn.Booleans -> {
                    val boolean = byte(); require(boolean in 0..1) { "Invalid column boolean byte" }
                    result.values[row] = boolean == 1
                }
                is FeatureColumn.Strings -> result.values[row] = data().decodeToString(throwOnInvalidSequence = true)
                is FeatureColumn.Binary -> result.values[row] = data()
            }
        }
        decoded.add(result)
    }
    require(offset == bytes.size) { "Trailing column payload" }
    currentCoroutineContext().ensureActive()
    return FeatureColumnBatch(count, decoded)
}

internal suspend fun RuntimeHandle.columnRead(): FeatureColumnBatch? {
    checkOpen()
    check(engine.supportsBinaryColumns) { "Column transfer requires engine binary protocol 3" }
    var result: FeatureColumnBatch? = null
    try {
        engine.invoke("FeatureColumnStream", runtimeId, "read", emptyMap(), binary = GdalBinaryCall(
            prepare = { null }, complete = { value, bytes -> result = decodeColumnBatch(value, bytes) }
        ))
        return result
    } catch (failure: Throwable) {
        try { closeHandle() } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
        throw failure
    }
}
