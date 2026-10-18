@file:OptIn(kotlin.js.ExperimentalWasmJsInterop::class)
package dev.gdal4k

import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine
import kotlin.js.*

@JsFun("(promise, success, failure) => promise.then(success, e => failure(String(e.stack || e)))")
private external fun initialize(promise: JsAny, success: (JsAny) -> Unit, failure: (String) -> Unit)
@JsFun("(bridge, request, file, success, failure) => bridge.call(request, file).then(success, e => failure(String(e.stack || e)))")
private external fun invoke(bridge: JsAny, request: String, file: JsAny?, success: (String) -> Unit, failure: (String) -> Unit)
@JsFun("(bridge, request, file, progress, cancelled, success, failure) => bridge.call(request, file, progress, cancelled).then(success, e => failure(String(e.stack || e)))")
private external fun controlledInvoke(bridge: JsAny, request: String, file: JsAny?, progress: (String) -> Unit, cancelled: () -> Boolean, success: (String) -> Unit, failure: (String) -> Unit)
@JsFun("(bridge, success, failure) => bridge.close().then(success, e => failure(String(e.stack || e)))")
private external fun dispose(bridge: JsAny, success: () -> Unit, failure: (String) -> Unit)

@JsFun("bridge => bridge.binaryVersion === 1 || bridge.binaryVersion === 2 || bridge.binaryVersion === 3")
private external fun supportsBinary(bridge: JsAny): Boolean
@JsFun("bridge => bridge.binaryVersion === 2 || bridge.binaryVersion === 3")
private external fun supportsCoordinates(bridge: JsAny): Boolean
@JsFun("bridge => bridge.binaryVersion === 3")
private external fun supportsColumns(bridge: JsAny): Boolean
@JsFun("(bridge, request, bytes, progress, cancelled, success, failure) => bridge.callBinary(request, bytes, progress, cancelled).then(success, e => failure(String(e.stack || e)))")
private external fun binaryInvoke(bridge: JsAny, request: String, bytes: JsAny?, progress: (String) -> Unit, cancelled: () -> Boolean, success: (JsAny) -> Unit, failure: (String) -> Unit)
@JsFun("size => new Uint8Array(size)")
private external fun newBytes(size: Int): JsAny
@JsFun("(bytes, offset, value, width) => { for (let j=0;j<width;j++) bytes[offset+j] = value >>> (8*j); }")
private external fun setBytes(bytes: JsAny, offset: Int, value: Int, width: Int)
@JsFun("reply => reply.json")
private external fun replyJson(reply: JsAny): String
@JsFun("reply => new Uint8Array(reply.bytes)")
private external fun replyBytes(reply: JsAny): JsAny
@JsFun("bytes => bytes.length")
private external fun bytesLength(bytes: JsAny): Int
@JsFun("(bytes, offset, width) => { let value=0; for(let j=0;j<width;j++) value |= bytes[offset+j] << (8*j); return value; }")
private external fun getBytes(bytes: JsAny, offset: Int, width: Int): Int
private suspend fun copyBytes(input: ByteArray, control: GdalCallControl?): JsAny {
    val bytes = newBytes(input.size)
    var offset=0
    while (offset<input.size) {
        if (offset and 65535 == 0) { control?.cancellation()?.let { throw it }; kotlinx.coroutines.yield() }
        val width=minOf(4,input.size-offset)
        var value=0
        for (j in 0 until width) value = value or ((input[offset+j].toInt() and 255) shl (8*j))
        setBytes(bytes,offset,value,width); offset+=width
    }
    return bytes
}
private suspend fun copyReply(reply: JsAny, control: GdalCallControl?): GdalBinaryReply {
    val bytes=replyBytes(reply); val result=ByteArray(bytesLength(bytes)); var offset=0
    while (offset<result.size) {
        if (offset and 65535 == 0) { control?.cancellation()?.let { throw it }; kotlinx.coroutines.yield() }
        val width=minOf(4,result.size-offset); val value=getBytes(bytes,offset,width)
        for (j in 0 until width) result[offset+j]=(value ushr (8*j)).toByte()
        offset+=width
    }
    return GdalBinaryReply(replyJson(reply),result)
}

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport {
    val bridge: JsAny = suspendCoroutine { c ->
        initialize(createGdalWorker(), { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
    }
    return object : GdalTransport {
        override val supportsBinaryRaster = supportsBinary(bridge)
        override val supportsBinaryCoordinates = supportsCoordinates(bridge)
        override val supportsBinaryColumns = supportsColumns(bridge)
        override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply {
            val bytes = input?.let { copyBytes(it, control) }
            val reply: JsAny = suspendCoroutine { c ->
                binaryInvoke(bridge, request, bytes, { control?.accept(it) }, { control?.shouldCancel() ?: false },
                    { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
            }
            return copyReply(reply, control)
        }
        override val supportsHttpInputPolicy = true
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String = suspendCoroutine { c ->
            if (control == null) invoke(bridge, request, attachment as? JsAny, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
            else controlledInvoke(bridge, request, attachment as? JsAny, control::accept, control::shouldCancel, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
        }
        override suspend fun close(): Unit = suspendCoroutine { c ->
            dispose(bridge, { c.resume(Unit) }, { c.resumeWithException(GdalException(it)) })
        }
    }
}
