package dev.gdal4k

internal data class GdalOptions(
    val libraryPath: String? = null,
    val projData: String? = null,
    val gdalData: String? = null,
)

internal interface GdalTransport {
    suspend fun call(request: String, attachment: Any? = null, control: GdalCallControl? = null): String
    suspend fun close()
}
internal expect suspend fun createTransport(options: GdalOptions): GdalTransport
