package dev.gdal4k

import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.withGdalProgress
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class GdalProgressCleanupTest {
    @Test fun lateFailureClosesEveryAllocatedHandleAndPreservesCleanupErrors() = runTest {
        val closed = mutableListOf<String>()
        val expected = IllegalStateException("observer error")
        val engine = RuntimeEngine(object : GdalTransport {
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
                if (args.getValue("method").jsonPrimitive.content == "close") {
                    val id = args.getValue("handle").jsonPrimitive.content; closed += id
                    return if (id == "43") """{"error":"cleanup failed"}""" else """{"result":{"value":null}}"""
                }
                requireNotNull(control).accept("""{"sequence":1,"fraction":1,"message":"done"}""")
                return """{"result":{"value":"42"},"allocatedHandles":["42","43"]}"""
            }
            override suspend fun close() {}
        })
        val failure = assertFailsWith<IllegalStateException> {
            withGdalProgress({ throw expected }) { engine.invoke("Driver", "1", "createCopy", emptyMap()) }
        }
        assertTrue(failure === expected || failure.cause === expected); assertEquals(listOf("43", "42"), closed)
        assertEquals("cleanup failed", expected.suppressedExceptions.single().message)
    }

    @Test fun observerFalseBecomesCancellationAfterAllocationCleanup() = runTest {
        var closed = false
        val engine = RuntimeEngine(object : GdalTransport {
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                if (control == null) { closed = true; return """{"result":{"value":null}}""" }
                control.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                return """{"result":{"value":"42"},"allocatedHandles":["42"]}"""
            }
            override suspend fun close() {}
        })
        assertFailsWith<CancellationException> { withGdalProgress({ false }) { engine.invoke("Driver", "1", "createCopy", emptyMap()) } }
        assertTrue(closed)
    }
}
