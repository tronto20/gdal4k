package dev.gdal4k.internal

import dev.gdal4k.GdalBinaryCall
import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.yield
import kotlinx.serialization.json.*

// Protocol 4 keeps names/sizes in JSON and concatenates exact file bytes in metadata order.
// Public buffers remain owned by their caller. Packing is deferred until the engine lock;
// queued calls retain references and small metadata, rather than complete encoded copies.
private suspend fun copyFileBytes(source: ByteArray, destination: ByteArray, offset: Int = 0) {
    var position = 0
    while (position < source.size) {
        currentCoroutineContext().ensureActive(); yield()
        val size = minOf(262144, source.size - position)
        source.copyInto(destination, offset + position, position, position + size)
        position += size
    }
    currentCoroutineContext().ensureActive()
}

internal suspend fun decodeBinaryFiles(value: JsonElement, bytes: ByteArray): GdalFiles {
    val metadata = value.jsonObject
    if (metadata["binaryFiles"]?.jsonPrimitive?.boolean != true) {
        require(bytes.isEmpty()) { "Unexpected payload with legacy file response" }
        return decodeFiles(value)
    }
    val main = metadata.getValue("main").jsonPrimitive.content
    val plan = metadata.getValue("files").jsonArray.map { item ->
        val file = item.jsonObject
        val size = file.getValue("size").jsonPrimitive.long
        require(size in 0..Int.MAX_VALUE.toLong()) { "Invalid file payload size" }
        file.getValue("name").jsonPrimitive.content to size.toInt()
    }
    // Validate the complete shape/names before allocating any final file buffers.
    GdalFiles(main, plan.map { GdalFile(it.first, ByteArray(0)) })
    require(plan.sumOf { it.second.toLong() } == bytes.size.toLong()) { "Truncated or trailing file payload" }
    var offset = 0
    val files = plan.map { (name, count) ->
        val data = ByteArray(count)
        var position = 0
        while (position < count) {
            currentCoroutineContext().ensureActive(); yield()
            val size = minOf(262144, count - position)
            bytes.copyInto(data, position, offset + position, offset + position + size)
            position += size
        }
        offset += count
        GdalFile(name, data)
    }
    currentCoroutineContext().ensureActive()
    return GdalFiles(main, files)
}

internal suspend fun RuntimeHandle.fileInputCall(type: String, method: String, parameters: Map<String, JsonElement>, input: GdalFiles): JsonElement {
    checkOpen(); currentCoroutineContext().ensureActive()
    val files = input.files.toList()
    val total = files.sumOf { it.bytes.size.toLong() }
    if (!engine.supportsBinaryFiles || total > Int.MAX_VALUE) {
        return invoke(type, method, parameters + ("files" to encode(input)))
    }
    val metadata = buildJsonObject {
        put("main", input.main)
        put("files", JsonArray(files.map { file -> buildJsonObject {
            put("name", file.name); put("size", file.bytes.size)
        } }))
    }
    return engine.invoke(type, runtimeId, method, parameters + ("files" to metadata), binary = GdalBinaryCall(
        prepare = {
            val packed = ByteArray(total.toInt())
            var offset = 0
            for (file in files) { copyFileBytes(file.bytes, packed, offset); offset += file.bytes.size }
            packed
        },
        complete = { _, bytes -> require(bytes.isEmpty()) { "Unexpected file input response payload" } }
    ))
}

internal suspend fun RuntimeHandle.fileOutputCall(type: String, method: String, parameters: Map<String, JsonElement>): GdalFiles {
    checkOpen(); currentCoroutineContext().ensureActive()
    if (!engine.supportsBinaryFiles) return decodeFiles(invoke(type, method, parameters))
    var files: GdalFiles? = null
    engine.invoke(type, runtimeId, method, parameters, binary = GdalBinaryCall(
        prepare = { null }, complete = { value, bytes -> files = decodeBinaryFiles(value, bytes) }
    ))
    return requireNotNull(files)
}

internal suspend fun RuntimeHandle.writeMemoryFileCall(path: String, bytes: ByteArray) {
    checkOpen(); currentCoroutineContext().ensureActive()
    val method = "writeMemoryFile_String_ByteArray"
    if (!engine.supportsBinaryFiles) {
        invoke("GdalRuntime", method, mapOf("path" to encode(path), "bytes" to encode(bytes)))
        return
    }
    engine.invoke("GdalRuntime", runtimeId, method, mapOf("path" to encode(path), "byteSize" to JsonPrimitive(bytes.size)),
        binary = GdalBinaryCall(prepare = { ByteArray(bytes.size).also { copyFileBytes(bytes, it) } },
            complete = { value, output -> require(value is JsonNull && output.isEmpty()) { "Invalid memory file write response" } }))
}

internal suspend fun RuntimeHandle.readMemoryFileCall(path: String): ByteArray {
    checkOpen(); currentCoroutineContext().ensureActive()
    val method = "readMemoryFile_String"
    if (!engine.supportsBinaryFiles) return invoke("GdalRuntime", method, mapOf("path" to encode(path)))
        .jsonArray.map { it.jsonPrimitive.content.toInt().toByte() }.toByteArray()
    var result: ByteArray? = null
    engine.invoke("GdalRuntime", runtimeId, method, mapOf("path" to encode(path)),
        binary = GdalBinaryCall(prepare = { null }, complete = { value, bytes ->
            require(value.jsonPrimitive.long == bytes.size.toLong()) { "Invalid memory file read response" }
            result = bytes
        }))
    return requireNotNull(result)
}
