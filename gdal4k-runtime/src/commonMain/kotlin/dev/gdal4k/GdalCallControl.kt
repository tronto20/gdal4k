package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*

/** Native completion is awaited even when the original caller is cancelled. */
internal class GdalCallControl(private val caller: Job?, private val observer: GdalProgressContext?, private val operation: String) {
    @kotlin.concurrent.Volatile var failure: Throwable? = null
        private set
    private var sequence = -1L
    fun shouldCancel(): Boolean = failure != null || caller?.isActive == false
    fun reject(error: Throwable) { if (failure == null) failure = error }
    fun accept(snapshot: String) {
        if (failure != null || caller?.isActive == false) return
        try {
            val value = Json.parseToJsonElement(snapshot).jsonObject
            val seq = value.getValue("sequence").jsonPrimitive.long
            if (seq <= sequence || seq == 0L) return
            sequence = seq
            if (observer?.observer?.invoke(GdalProgress(operation, value.getValue("fraction").jsonPrimitive.double,
                    value.getValue("message").jsonPrimitive.content)) == false) {
                reject(CancellationException("GDAL operation cancelled by progress observer"))
            }
        } catch (error: Throwable) { reject(error) }
    }
    fun cancellation(): Throwable? = failure ?: if (caller?.isActive == false) CancellationException("GDAL operation cancelled") else null
}

/** Polling never takes the GDAL context lock and never calls Kotlin from a native GDAL callback. */
internal suspend fun <Token, T> pollingGdalCall(control: GdalCallControl, create: () -> Token,
    cancel: (Token) -> Unit, snapshot: (Token) -> String, destroy: (Token) -> Unit, block: suspend (Token) -> T): T = coroutineScope {
    val token = create()
    if (control.shouldCancel()) cancel(token)
    val monitor = launch(Dispatchers.Default) {
        while (isActive) {
            try { control.accept(snapshot(token)); if (control.shouldCancel()) cancel(token) }
            catch (error: Throwable) { control.reject(error); cancel(token) }
            delay(20)
        }
    }
    try { block(token) }
    finally {
        monitor.cancelAndJoin()
        try { control.accept(snapshot(token)); if (control.shouldCancel()) cancel(token) }
        catch (error: Throwable) { control.reject(error) }
        finally { destroy(token) }
    }
}
