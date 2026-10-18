package dev.gdal4k.internal

internal actual suspend fun platformPackRaster(array: Any): ByteArray? = null
internal actual suspend fun platformUnpackRaster(bytes: ByteArray, array: Any): Boolean = false
