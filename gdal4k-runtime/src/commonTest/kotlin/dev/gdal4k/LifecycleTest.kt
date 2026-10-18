package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class LifecycleTest {
    @Test fun lazyInitializationAndSharedLifetime() = runTest(timeout = 180.seconds) {
        Gdal4k.close()
        try {
            val cancelled = launch(start = CoroutineStart.UNDISPATCHED) {
                currentCoroutineContext().cancel()
                assertFailsWith<CancellationException> { Gdal4k.runtime() }
            }
            cancelled.join()
            val runtimes = List(8) { async(Dispatchers.Default) { Gdal4k.runtime() } }.awaitAll()
            val runtime = runtimes.first()
            runtimes.forEach { assertSame(runtime, it) }
            assertEquals("3120400", runtime.versionInfo("VERSION_NUM"))
            Gdal4k.prepare() // Optional warm-up must not replace an active runtime.
            assertSame(runtime, Gdal4k.runtime())
            Gdal4k.close()
            val restarted = Gdal4k.runtime() // No separate prepare, including after explicit release.
            assertNotSame(runtime, restarted)
            assertFails { runtime.versionInfo() }
            assertEquals("3120400", restarted.versionInfo("VERSION_NUM"))
            repeat(3) {
                // Either acquisition can win; neither may fail with "not prepared".
                val acquiring = async(Dispatchers.Default) { Gdal4k.runtime() }
                val closing = async(Dispatchers.Default) { Gdal4k.close() }
                acquiring.await()
                closing.await()
                assertEquals("3120400", Gdal4k.runtime().versionInfo("VERSION_NUM"))
            }
        } finally { Gdal4k.close() }
    }

    @Test fun cancellationAndShutdown() = runTest(timeout = 180.seconds) {
        val cancelledPreparation = launch(start = CoroutineStart.UNDISPATCHED) {
            currentCoroutineContext().cancel()
            assertFailsWith<CancellationException> { Gdal4k.prepare() }
        }
        cancelledPreparation.join()
        val runtime = Gdal4k.runtime()
        try {
            val memory = requireNotNull(runtime.getDriver("MEM"))
            val source = requireNotNull(memory.create("", 2048, 2048, 1, GdalDataType.Byte))
            val jxl = requireNotNull(runtime.getDriver("JPEGXL"))
            var completed: Dataset? = null
            var cancellationObserved = false
            val job = launch(start = CoroutineStart.UNDISPATCHED) {
                try { completed = jxl.createCopy("/vsimem/gdal4k-cancellation/image.jxl", source, options = listOf("LOSSLESS=YES", "EFFORT=7")) }
                catch (cancelled: CancellationException) { cancellationObserved = true; throw cancelled }
            }
            // Wait for native cancellation/completion and cleanup before touching inputs. A call
            // that won the completion race can return; otherwise cancellation owns no result.
            job.cancelAndJoin()
            val result = completed
            if (result != null) result.use { assertEquals(2048, it.getRasterXSize()) }
            else assertTrue(cancellationObserved)
            assertEquals(2048, source.getRasterXSize())
            runtime.deleteMemoryFile("/vsimem/gdal4k-cancellation")
            source.close()
            val cancelled = launch(start = CoroutineStart.UNDISPATCHED) {
                currentCoroutineContext().cancel()
                assertFailsWith<CancellationException> { runtime.getDriver("MEM") }
            }
            cancelled.join()
            runtime.createSpatialReference("EPSG:4326").use { assertTrue(it.isGeographic()) }
        } finally { Gdal4k.close() }
        assertFails { runtime.versionInfo() }
        Gdal4k.close() // idempotent
        try {
            val restarted = Gdal4k.runtime()
            assertEquals("3120400", restarted.versionInfo("VERSION_NUM"))
            val source = requireNotNull(requireNotNull(restarted.getDriver("MEM")).create("", 2048, 2048, 1, GdalDataType.Byte))
            val driver = requireNotNull(restarted.getDriver("JPEGXL"))
            val active = async(start = CoroutineStart.UNDISPATCHED) {
                driver.createCopy("/vsimem/gdal4k-shutdown.jxl", source, options = listOf("LOSSLESS=YES", "EFFORT=7"))
            }
            val closing = async(start = CoroutineStart.UNDISPATCHED) { Gdal4k.close() }
            requireNotNull(active.await()).close()
            closing.await()
            assertFails { restarted.versionInfo() }
        }
        finally { Gdal4k.close() }
        println("GDAL4K_CANCELLATION_AND_SHUTDOWN_OK")
    }
}
