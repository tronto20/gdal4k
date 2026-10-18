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

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport {
    val bridge: JsAny = suspendCoroutine { c ->
        initialize(createGdalWorker(), { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
    }
    return object : GdalTransport {
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
