package dev.gdal4k

import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

private fun initialize(promise: dynamic, success: (dynamic) -> Unit, failure: (String) -> Unit) { js("promise.then(success, e => failure(String(e.stack || e)))") }
private fun invoke(bridge: dynamic, request: String, file: dynamic, success: (String) -> Unit, failure: (String) -> Unit) { js("bridge.call(request, file).then(success, e => failure(String(e.stack || e)))") }
private fun controlledInvoke(bridge: dynamic, request: String, file: dynamic, progress: (String) -> Unit, cancelled: () -> Boolean, success: (String) -> Unit, failure: (String) -> Unit) { js("bridge.call(request, file, progress, cancelled).then(success, e => failure(String(e.stack || e)))") }
private fun dispose(bridge: dynamic, success: () -> Unit, failure: (String) -> Unit) { js("bridge.close().then(success, e => failure(String(e.stack || e)))") }

internal actual suspend fun createTransport(options: GdalOptions): GdalTransport {
    val bridge: dynamic = suspendCoroutine { c ->
        initialize(createGdalWorker(), { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
    }
    return object : GdalTransport {
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String = suspendCoroutine { c ->
            if (control == null) invoke(bridge, request, attachment, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
            else controlledInvoke(bridge, request, attachment, control::accept, control::shouldCancel, { c.resume(it) }, { c.resumeWithException(GdalException(it)) })
        }
        override suspend fun close(): Unit = suspendCoroutine { c ->
            dispose(bridge, { c.resume(Unit) }, { c.resumeWithException(GdalException(it)) })
        }
    }
}
