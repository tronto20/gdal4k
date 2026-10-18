package dev.gdal4k

import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

internal class JniBridge {
    external fun binaryVersion(): Int
    external fun executeBinary(handle: Long, request: ByteArray, input: ByteArray?, control: Long): Array<ByteArray>
    external fun create(proj: ByteArray, data: ByteArray): Long
    external fun execute(handle: Long, request: ByteArray): ByteArray
    external fun executeControlled(handle: Long, request: ByteArray, control: Long): ByteArray
    external fun controlCreate(): Long
    external fun controlCancel(control: Long)
    external fun controlSnapshot(control: Long): ByteArray
    external fun controlDestroy(control: Long)
    external fun destroy(handle: Long)
    external fun mount(handle: Long, directory: JniDirectory): String
    external fun unmount(prefix: String)
}
internal expect fun loadBundledLibrary(path: String?)

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport = withContext(Dispatchers.IO + NonCancellable) {
    loadBundledLibrary(options.libraryPath)
    val bridge = JniBridge()
    val handle = bridge.create(options.projData.orEmpty().encodeToByteArray(), options.gdalData.orEmpty().encodeToByteArray())
    check(handle != 0L) { "Unable to initialize GDAL" }
    object : GdalTransport {
        override val supportsBinaryRaster = try { bridge.binaryVersion() == 1 } catch (_: UnsatisfiedLinkError) { false }
        private var closed = false
        private val inputs = JniInputs({ bridge.mount(handle, it) }, bridge::unmount)
        @Synchronized private fun callNow(request: String, attachment: Any?, control: Long = 0): String {
            check(!closed) { "GDAL runtime is closed" }
            return inputs.execute(request, attachment) {
                (if (control == 0L) bridge.execute(handle, it.encodeToByteArray()) else bridge.executeControlled(handle, it.encodeToByteArray(), control)).decodeToString()
            }
        }
        @Synchronized private fun binaryNow(request: String, input: ByteArray?, control: Long): GdalBinaryReply {
            check(!closed) { "GDAL runtime is closed" }
            val result = bridge.executeBinary(handle, request.encodeToByteArray(), input, control)
            return GdalBinaryReply(result[0].decodeToString(), result[1])
        }
        override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply =
            if (control == null) withContext(Dispatchers.IO + NonCancellable) { binaryNow(request, input, 0L) }
            else pollingGdalCall(control, { bridge.controlCreate().also { check(it != 0L) } },
                bridge::controlCancel, { bridge.controlSnapshot(it).decodeToString() }, bridge::controlDestroy) { token ->
                withContext(Dispatchers.IO + NonCancellable) { binaryNow(request, input, token) }
            }
        @Synchronized private fun closeNow() {
            if (!closed) {
                try { bridge.destroy(handle) } finally { closed = true; inputs.close() }
            }
        }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String =
            if (control == null) withContext(Dispatchers.IO + NonCancellable) { callNow(request, attachment) }
            else pollingGdalCall(control, { bridge.controlCreate().also { check(it != 0L) { "Unable to allocate GDAL control" } } },
                bridge::controlCancel, { bridge.controlSnapshot(it).decodeToString() }, bridge::controlDestroy) { token ->
                withContext(Dispatchers.IO + NonCancellable) { callNow(request, attachment, token) }
            }
        override suspend fun close() = withContext(Dispatchers.IO + NonCancellable) { closeNow() }
    }
}
