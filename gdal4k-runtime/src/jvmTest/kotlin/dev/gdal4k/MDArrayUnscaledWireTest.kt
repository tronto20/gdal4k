package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArrayUnscaledWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        val controls = mutableListOf<Pair<String, Boolean>>()
        val closed = mutableListOf<String>()
        var reply = """{"result":{"value":"42"},"allocatedHandles":["42"]}"""
        var beforeUnscaled: suspend () -> Unit = {}

        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            val method = args.getValue("method").jsonPrimitive.content
            val handle = args.getValue("handle").jsonPrimitive.content
            requests += args
            controls += method to (control != null)
            return when (method) {
                "close" -> {
                    assertNull(control)
                    closed += handle
                    """{"result":{"value":null}}"""
                }
                "getName" -> buildJsonObject {
                    putJsonObject("result") { put("value", "array:$handle") }
                }.toString()
                "getUnscaled" -> {
                    val ownedControl = assertNotNull(control,
                        "An ordinary getUnscaled call must use the controlled owned-result route")
                    ownedControl.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeUnscaled()
                    ownedControl.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    reply
                }
                else -> error("Unexpected wire method: $method")
            }
        }

        override suspend fun close() {}
    }

    @Test fun ordinaryGetUnscaledUsesOwnedControlAndKeepsTheSourceLifetime() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            assertEquals("array:7", source.getName())
            val view = source.getUnscaled()
            val request = calls.requests.single {
                it.getValue("method").jsonPrimitive.content == "getUnscaled"
            }
            assertEquals("MDArray", request.getValue("type").jsonPrimitive.content)
            assertEquals("7", request.getValue("handle").jsonPrimitive.content)
            assertEquals(JsonObject(emptyMap()), request.getValue("parameters"))
            assertEquals(listOf("getName" to false, "getUnscaled" to true), calls.controls)
            assertEquals("array:42", view.getName())

            // Returning an owned wrapper must not detach it from the source lifetime.
            source.close()
            val before = calls.requests.size
            assertFailsWith<IllegalStateException> { view.getName() }
            assertEquals(before, calls.requests.size)
            view.close(); view.close()
            assertEquals(listOf("7", "42"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun cancellationAfterGetUnscaledAllocationClosesOnlyTheNewView() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val sibling = BundledMDArray(engine, "8", source)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeUnscaled = { entered.complete(Unit); finish.await() }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.getUnscaled() }
            entered.await()
            pending.cancel()
            finish.complete(Unit)
            pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            assertEquals(listOf("42"), calls.closed)
            assertEquals("array:7", source.getName())
            assertEquals("array:8", sibling.getName())
        } finally { engine.close() }
    }

    @Test fun malformedUnscaledReplyCleansKnownAllocationsAndRejectsMissingOwnership() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val sibling = BundledMDArray(engine, "8", source)
            val invalidOwnedHandle = "GDAL getUnscaled returned an invalid owned handle"
            for (reply in listOf(
                """{"result":{"value":"43"},"allocatedHandles":["42"]}""",
                """{"result":{"value":42},"allocatedHandles":["42"]}""",
            )) {
                calls.reply = reply
                val failure = assertFailsWith<IllegalStateException>(reply) { source.getUnscaled() }
                assertEquals(invalidOwnedHandle, failure.message, reply)
                val primary = failure.cause?.let { assertIs<IllegalStateException>(it, reply) } ?: failure
                assertEquals(invalidOwnedHandle, primary.message, reply)
                assertNull(primary.cause, reply)
                assertEquals(listOf("42"), calls.closed, reply)
                calls.closed.clear()
                assertEquals("array:7", source.getName())
                assertEquals("array:8", sibling.getName())
            }
            calls.reply = """{"error":{},"allocatedHandles":["42"]}"""
            val malformedError = assertFailsWith<IllegalArgumentException>(calls.reply) { source.getUnscaled() }
            val notJsonPrimitive = "Element ${JsonObject::class} is not a JsonPrimitive"
            assertEquals(notJsonPrimitive, malformedError.message, calls.reply)
            // Coroutine stacktrace recovery may copy the primary exception across withContext.
            val malformedPrimary = malformedError.cause?.let {
                assertIs<IllegalArgumentException>(it, calls.reply)
            } ?: malformedError
            assertEquals(notJsonPrimitive, malformedPrimary.message, calls.reply)
            assertNull(malformedPrimary.cause, calls.reply)
            assertEquals(listOf("42"), calls.closed, calls.reply)
            calls.closed.clear()
            assertEquals("array:7", source.getName())
            assertEquals("array:8", sibling.getName())

            calls.reply = """{"result":{"value":"42"}}"""
            val missingOwnership = assertFailsWith<IllegalStateException> { source.getUnscaled() }
            assertEquals(invalidOwnedHandle, missingOwnership.message)
            val missingPrimary = missingOwnership.cause?.let { assertIs<IllegalStateException>(it) } ?: missingOwnership
            assertEquals(invalidOwnedHandle, missingPrimary.message)
            assertNull(missingPrimary.cause)
            assertTrue(calls.closed.isEmpty(), "Missing allocation metadata cannot identify a new wrapper to close")
            assertEquals("array:7", source.getName())
            assertEquals("array:8", sibling.getName())
        } finally { engine.close() }
    }
}
