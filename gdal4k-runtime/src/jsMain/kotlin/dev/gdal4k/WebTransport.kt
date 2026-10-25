package dev.gdal4k

import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

private fun initialize(promise: dynamic, success: (dynamic) -> Unit, failure: (String) -> Unit) { js("promise.then(success, e => failure(String(e.stack || e)))") }
private fun invoke(bridge: dynamic, request: String, file: dynamic, success: (String) -> Unit, failure: (String) -> Unit) { js("bridge.call(request, file).then(success, e => failure(String(e.stack || e)))") }
private fun controlledInvoke(bridge: dynamic, request: String, file: dynamic, progress: (String) -> Unit, cancelled: () -> Boolean, success: (String) -> Unit, failure: (String) -> Unit) { js("bridge.call(request, file, progress, cancelled).then(success, e => failure(String(e.stack || e)))") }
private fun dispose(bridge: dynamic, success: () -> Unit, failure: (String) -> Unit) { js("bridge.close().then(success, e => failure(String(e.stack || e)))") }

private fun binaryInvoke(bridge: dynamic, request: String, bytes: ByteArray?, progress: (String) -> Unit, cancelled: () -> Boolean, success: (dynamic) -> Unit, failure: (String) -> Unit) {
    js("bridge.callBinary(request, bytes, progress, cancelled).then(success, e => failure(String(e.stack || e)))")
}
private fun binaryBytes(reply: dynamic): ByteArray = js("new Int8Array(reply.bytes)")

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport {
    val bridge: dynamic = suspendCoroutine { c ->
        initialize(createGdalWorker(), { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
    }
    return object : GdalTransport {
        override val supportsBinaryRaster: Boolean = bridge.binaryVersion == 1 || bridge.binaryVersion == 2 || bridge.binaryVersion == 3 || bridge.binaryVersion == 4 || bridge.binaryVersion == 5 || bridge.binaryVersion == 6
        override val supportsBinaryCoordinates: Boolean = bridge.binaryVersion == 2 || bridge.binaryVersion == 3 || bridge.binaryVersion == 4 || bridge.binaryVersion == 5 || bridge.binaryVersion == 6
        override val supportsBinaryColumns: Boolean = bridge.binaryVersion == 3 || bridge.binaryVersion == 4 || bridge.binaryVersion == 5 || bridge.binaryVersion == 6
        override val supportsBinaryFiles: Boolean = bridge.binaryVersion == 4 || bridge.binaryVersion == 5 || bridge.binaryVersion == 6
        override val supportsBinaryMD: Boolean = bridge.binaryVersion == 5 || bridge.binaryVersion == 6
        override val supportsMDJsonStringSubtype: Boolean = bridge.binaryVersion == 6
        override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply = suspendCoroutine { c ->
            binaryInvoke(bridge, request, input, { control?.accept(it) }, { control?.shouldCancel() ?: false },
                { c.resume(GdalBinaryReply(it.json as String, binaryBytes(it))) }, { c.resumeWithException(GdalException(it)) })
        }
        override val supportsHttpInputPolicy = true
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String = suspendCoroutine { c ->
            if (control == null) invoke(bridge, request, attachment, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
            else controlledInvoke(bridge, request, attachment, control::accept, control::shouldCancel, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
        }
        override suspend fun close(): Unit = suspendCoroutine { c ->
            dispose(bridge, { c.resume(Unit) }, { c.resumeWithException(GdalException(it)) })
        }
    }
}
