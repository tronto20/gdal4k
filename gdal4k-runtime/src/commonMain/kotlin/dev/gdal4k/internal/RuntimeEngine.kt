package dev.gdal4k.internal

import dev.gdal4k.*
import dev.gdal4k.runtime.*
import kotlinx.serialization.json.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import kotlinx.coroutines.Job
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlin.io.encoding.Base64

internal class RuntimeEngine(private val transport: GdalTransport) {
    val supportsBinaryCoordinates: Boolean get() = transport.supportsBinaryCoordinates
    val supportsBinaryRaster: Boolean get() = transport.supportsBinaryRaster
    val supportsHttpInputPolicy: Boolean get() = transport.supportsHttpInputPolicy
    private val mutex = Mutex()
    private var closed = false
    suspend fun close() = withContext(NonCancellable) { mutex.withLock {
        if (!closed) { transport.close(); closed = true }
    } }
    suspend fun invoke(type: String, handle: String, method: String, parameters: Map<String, JsonElement>, attachment: Any? = null, binary: GdalBinaryCall? = null): JsonElement {
        val caller = currentCoroutineContext()
        if (method != "close") caller.ensureActive()
        val name = method.substringBefore('_')
        val cancellable = transport.supportsHttpInputPolicy || name in setOf("openDataset", "translate", "translateTo", "warp", "warpTo", "translateVector", "translateVectorTo",
            "demProcessing", "demProcessingTo", "rasterize", "rasterizeTo", "polygonize", "polygonizeTo", "buildVRT",
            "transformPoints", "buildOverviews", "createCopy", "computeStatistics", "getHistogram", "getDefaultHistogram", "readRaster", "writeRaster", "readBlock", "writeBlock")
        val control = if (method != "close" && (cancellable || caller[GdalProgressContext] != null))
            GdalCallControl(caller[Job], caller[GdalProgressContext], "$type.$name") else null
        return mutex.withLock {
        if (method != "close") { caller.ensureActive(); check(!closed) { "GDAL runtime is closed" } }
        // Queued calls hold only caller references and metadata, never eagerly packed large buffers.
        val input = binary?.prepare?.invoke()
        val value = withContext(NonCancellable) {
        if (closed) {
            check(method == "close") { "GDAL runtime is closed" }
            return@withContext JsonNull
        }
        val request = buildJsonObject {
            put("op", "runtime")
            putJsonObject("args") {
                put("type", type); put("handle", handle); put("method", method)
                put("parameters", JsonObject(parameters))
            }
        }
        if ((parameters["options"] as? JsonObject)?.containsKey("httpInputPolicy") == true) {
            require(type == "GdalRuntime" && method == "openDataset_String_DatasetOpenOptions" &&
                parameters["source"]?.jsonPrimitive?.content?.let { it.startsWith("http://", true) || it.startsWith("https://", true) } == true) {
                "HTTP input policy requires a direct HTTP(S) URL"
            }
        }
        val response = try {
            val text = if (binary == null) transport.call(request.toString(), attachment, control)
                else transport.callBinary(request.toString(), input, control).let { binary.output = it.bytes; it.json }
            Json.parseToJsonElement(text).jsonObject
        }
        catch (transportFailure: Throwable) {
            val cancelled = control?.cancellation()
            if (cancelled != null) { if (cancelled !== transportFailure) cancelled.addSuppressed(transportFailure); throw cancelled }
            throw transportFailure
        }
        val failure = control?.cancellation() ?: response["error"]?.let { GdalException(it.jsonPrimitive.content) }
        if (failure != null) {
            for (id in response["allocatedHandles"]?.jsonArray.orEmpty().asReversed()) {
                try {
                    val close = buildJsonObject {
                        put("op", "runtime")
                        putJsonObject("args") { put("type", "Handle"); put("handle", id); put("method", "close"); putJsonObject("parameters") {} }
                    }
                    val closed = Json.parseToJsonElement(transport.call(close.toString())).jsonObject
                    closed["error"]?.let { throw GdalException(it.jsonPrimitive.content) }
                } catch (cleanup: Throwable) { failure.addSuppressed(cleanup) }
            }
            throw failure
        }
        response.getValue("result").jsonObject.getValue("value")
        }
        // Decode under the same lock in the original cancellable context. Multiple queued
        // reads cannot accumulate one decoded staging buffer each while another read starts.
        if (binary != null) binary.complete(value, binary.output)
        value
    }
    }
}

internal open class RuntimeHandle(val engine: RuntimeEngine, private val id: String, private val parent: RuntimeHandle?) {
    @kotlin.concurrent.Volatile private var closed = false
    internal val runtimeId: String get() = id
    fun checkOpen() { check(!closed) { "GDAL handle is closed" }; parent?.checkOpen() }
    suspend fun invoke(type: String, method: String, parameters: Map<String, JsonElement>): JsonElement {
        checkOpen()
        return engine.invoke(type, id, method, parameters)
    }
    suspend fun closeHandle() = withContext(NonCancellable) {
        if (!closed) {
            // Closing a borrowed handle never destroys the parent's GDAL object.
            engine.invoke("Handle", id, "close", emptyMap())
            closed = true
        }
    }
    fun handle(value: Any?): JsonElement {
        if (value == null) return JsonNull
        val h = value as? RuntimeHandle ?: error("Handle was not created by the bundled GDAL runtime")
        require(h.engine === engine) { "Handle belongs to another GDAL runtime" }
        h.checkOpen()
        val type = when(value) {
            is ColorTable -> "ColorTable"; is RasterAttributeTable -> "RasterAttributeTable"
            is Attribute -> "Attribute"; is Group -> "Group"; is MDArray -> "MDArray"; is Dimension -> "Dimension"
            is FieldDomain -> "FieldDomain"; is Relationship -> "Relationship"
            is Dataset -> "Dataset"; is Driver -> "Driver"; is Band -> "Band"
            is SpatialReference -> "SpatialReference"; is CoordinateTransformation -> "CoordinateTransformation"
            is GCP -> "GCP"; is Layer -> "Layer"; is Feature -> "Feature"
            is FeatureDefn -> "FeatureDefn"; is FieldDefn -> "FieldDefn"; is GeomFieldDefn -> "GeomFieldDefn"
            is Geometry -> "Geometry"; else -> error("Unknown GDAL handle")
        }
        return buildJsonObject { put("handle", h.id); put("type", type) }
    }
    fun encode(value: Any?): JsonElement = when(value) {
        null -> JsonNull
        is MDDataType -> encodeMDType(value)
        is MDValue -> encodeMDValue(value)
        is CodedFieldValue -> buildJsonObject { put("code", encode(value.code)); put("label", encode(value.label)) }
        is FieldDomainBound -> buildJsonObject { put("value", encode(value.value)); put("inclusive", value.inclusive) }
        is ColorEntry -> JsonArray(listOf(value.c1, value.c2, value.c3, value.c4).map(::JsonPrimitive))
        is ULong -> JsonPrimitive(value.toString())
        is BandRasterIOOptions -> buildJsonObject {
            put("xOffset", value.xOffset); put("yOffset", value.yOffset)
            put("xSize", value.xSize); put("ySize", value.ySize)
            put("bufferWidth", value.bufferWidth); put("bufferHeight", value.bufferHeight)
            put("resampling", value.resampling.code)
        }
        is RasterIOOptions -> buildJsonObject {
            put("xOffset", value.xOffset); put("yOffset", value.yOffset)
            put("xSize", value.xSize); put("ySize", value.ySize)
            put("bufferWidth", value.bufferWidth); put("bufferHeight", value.bufferHeight)
            put("bands", encode(value.bands)); put("resampling", value.resampling.code)
        }
        is String -> { require('\u0000' !in value) { "GDAL strings cannot contain NUL" }; JsonPrimitive(value) }
        is Boolean -> JsonPrimitive(value)
        is Long -> JsonPrimitive(value.toString()) // JavaScript numbers cannot represent every Int64.
        is Double -> if (value.isFinite()) JsonPrimitive(value) else JsonPrimitive(value.toString())
        is Float -> encode(value.toDouble())
        is Number -> JsonPrimitive(value)
        is List<*> -> JsonArray(value.map { if (it is RuntimeHandle) handle(it) else encode(requireNotNull(it)) })
        is Array<*> -> JsonArray(value.map { encode(requireNotNull(it)) })
        is ByteArray -> JsonArray(value.map(::JsonPrimitive))
        is ShortArray -> JsonArray(value.map(::JsonPrimitive))
        is IntArray -> JsonArray(value.map(::JsonPrimitive))
        is LongArray -> JsonArray(value.map { encode(it) })
        is FloatArray -> JsonArray(value.map { encode(it) })
        is DoubleArray -> JsonArray(value.map { encode(it) })
        is DatasetOpenOptions -> buildJsonObject {
            put("update", value.accessMode == DatasetAccessMode.Update)
            put("kind", when(value.kind) { DatasetKind.Any -> 0; DatasetKind.Raster -> 2; DatasetKind.Vector -> 4; DatasetKind.Multidimensional -> 16 })
            put("options", encode(value.openOptions))
            value.httpInputPolicy?.let { policy ->
                require(engine.supportsHttpInputPolicy) { "HTTP input policy requires the JS/wasmJs HTTP backend; Native/JNI support is not provided by the default engine" }
                putJsonObject("httpInputPolicy") {
                    put("requestTimeoutMillis", policy.requestTimeoutMillis); put("maxRetries", policy.maxRetries)
                    put("retryDelayMillis", policy.retryDelayMillis); put("maxRetryDelayMillis", policy.maxRetryDelayMillis)
                    put("requireStrongETag", policy.requireStrongETag)
                }
            }
        }
        is DatasetInfoOptions -> buildJsonObject { put("arguments", encode(value.arguments)) }
        is GdalFiles -> buildJsonObject {
            put("main", value.main)
            put("files", JsonArray(value.files.map { file -> buildJsonObject {
                put("name", file.name); put("data", Base64.encode(file.bytes))
            } }))
        }
        else -> error("Unsupported GDAL argument: $value")
    }
}

internal fun decodeFiles(value: JsonElement): GdalFiles = value.jsonObject.let { files ->
    GdalFiles(files.getValue("main").jsonPrimitive.content, files.getValue("files").jsonArray.map { entry ->
        entry.jsonObject.let { file -> GdalFile(file.getValue("name").jsonPrimitive.content, Base64.decode(file.getValue("data").jsonPrimitive.content)) }
    })
}
