package dev.gdal4k

internal data class GdalOptions(
    val libraryPath: String? = null,
    val projData: String? = null,
    val gdalData: String? = null,
)

internal interface GdalTransport {
    val supportsBinaryRaster: Boolean get() = false
    val supportsBinaryCoordinates: Boolean get() = false
    suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply = error("Binary raster transport unavailable")
    val supportsHttpInputPolicy: Boolean get() = false
    suspend fun call(request: String, attachment: Any? = null, control: GdalCallControl? = null): String
    suspend fun close()
}
internal expect suspend fun createTransport(options: GdalOptions): GdalTransport

internal data class GdalBinaryReply(val json: String, val bytes: ByteArray)
internal class GdalBinaryCall(val prepare: suspend () -> ByteArray?, val complete: suspend (kotlinx.serialization.json.JsonElement, ByteArray) -> Unit) { var output = ByteArray(0) }
