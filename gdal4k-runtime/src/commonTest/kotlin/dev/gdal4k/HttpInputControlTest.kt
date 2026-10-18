package dev.gdal4k

import dev.gdal4k.internal.RuntimeEngine
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*

class HttpInputControlTest {
    @Test fun cancelledOpenKeepsCancellationWhenBackendAbortRejectsTransport() = runTest {
        cancelDuringTransport("openDataset_String_DatasetOpenOptions", false)
    }
    @Test fun webBlockReadsWithoutGdalProgressStillReceiveCancellation() = runTest {
        cancelDuringTransport("readBlock_Int_Int_ByteArray", true)
    }
    private suspend fun kotlinx.coroutines.CoroutineScope.cancelDuringTransport(method: String, web: Boolean) {
        val backend = IllegalStateException("fetch aborted")
        var original: Job? = null
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsHttpInputPolicy = web
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                assertNotNull(control) // Opening can wait for HTTP before GDAL starts.
                original!!.cancel()
                assertTrue(control.shouldCancel())
                throw backend
            }
            override suspend fun close() {}
        })
        val task = async { original = currentCoroutineContext().job; engine.invoke(if (web) "Band" else "GdalRuntime", "0", method, emptyMap()) }
        assertFailsWith<CancellationException> { task.await() }
        engine.close()
    }
}
