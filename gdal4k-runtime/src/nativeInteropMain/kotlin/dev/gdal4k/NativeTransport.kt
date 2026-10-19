@file:OptIn(kotlinx.cinterop.ExperimentalForeignApi::class)
package dev.gdal4k

import dev.gdal4k.cinterop.*
import cnames.structs.gdal4k_control
import kotlinx.cinterop.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport = withContext(Dispatchers.Default + NonCancellable) {
    val handle = gdal4k_create(options.projData, options.gdalData) ?: error("Unable to initialize GDAL")
    object : GdalTransport {
        private val binaryVersion = gdal4k_binary_version()
        override val supportsBinaryRaster = binaryVersion in 1..4
        override val supportsBinaryCoordinates = binaryVersion in 2..4
        override val supportsBinaryColumns = binaryVersion in 3..4
        override val supportsBinaryFiles = binaryVersion == 4
        private fun binaryNow(request: String, input: ByteArray?, control: CPointer<gdal4k_control>?): GdalBinaryReply {
            fun execute(pointer: CPointer<UByteVar>?): GdalBinaryReply {
                val result = gdal4k_execute_binary(handle, request, pointer, input?.size ?: 0, control)
                    ?: error("Binary GDAL bridge failed")
                return try {
                    val size = gdal4k_binary_size(result)
                    val bytes = if (size == 0) ByteArray(0) else requireNotNull(gdal4k_binary_data(result)).reinterpret<ByteVar>().readBytes(size)
                    GdalBinaryReply(requireNotNull(gdal4k_binary_json(result)).toKString(), bytes)
                } finally { gdal4k_binary_destroy(result) }
            }
            return if (input == null || input.isEmpty()) execute(null) else input.usePinned { execute(it.addressOf(0).reinterpret()) }
        }
        override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply =
            if (control == null) withContext(Dispatchers.Default + NonCancellable) { binaryNow(request, input, null) }
            else pollingGdalCall(control, { gdal4k_control_create() ?: error("Unable to allocate GDAL control") },
                { gdal4k_control_cancel(it) }, {
                    val text = gdal4k_control_snapshot(it) ?: error("GDAL progress allocation failed")
                    try { text.toKString() } finally { gdal4k_free(text) }
                }, { gdal4k_control_destroy(it) }) { token ->
                withContext(Dispatchers.Default + NonCancellable) { binaryNow(request, input, token) }
            }

        private fun callNow(request: String, control: CPointer<gdal4k_control>? = null): String {
            val result = (if (control == null) gdal4k_execute(handle, request) else gdal4k_execute_controlled(handle, request, control)) ?: error("GDAL bridge returned null")
            return try { result.toKString() } finally { gdal4k_free(result) }
        }
        private fun closeNow() { gdal4k_destroy(handle) }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            require(attachment == null) { "Browser files require JS/wasmJs" }
            return if (control == null) withContext(Dispatchers.Default + NonCancellable) { callNow(request) }
            else pollingGdalCall(control, { gdal4k_control_create() ?: error("Unable to allocate GDAL control") },
                { gdal4k_control_cancel(it) }, {
                    val text = gdal4k_control_snapshot(it) ?: error("GDAL progress allocation failed")
                    try { text.toKString() } finally { gdal4k_free(text) }
                }, { gdal4k_control_destroy(it) }) { token ->
                withContext(Dispatchers.Default + NonCancellable) { callNow(request, token) }
            }
        }
        override suspend fun close() = withContext(Dispatchers.Default + NonCancellable) { closeNow() }
    }
}
