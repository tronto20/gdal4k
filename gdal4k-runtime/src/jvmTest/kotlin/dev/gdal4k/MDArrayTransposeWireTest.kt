package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArrayTransposeWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        val controls = mutableListOf<Pair<String, Boolean>>()
        val closed = mutableListOf<String>()
        var reply = """{"result":{"value":"42"},"allocatedHandles":["42"]}"""
        var closeFailure: String? = null
        var beforeName: suspend () -> Unit = {}
        var beforeTranspose: suspend (GdalCallControl) -> Unit = {}

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
                    closeFailure?.let { buildJsonObject { put("error", it) }.toString() }
                        ?: """{"result":{"value":null}}"""
                }
                "getName" -> {
                    beforeName()
                    buildJsonObject { putJsonObject("result") { put("value", "array:$handle") } }.toString()
                }
                "transpose_IntArray" -> {
                    val ownedControl = assertNotNull(control, "An ordinary transpose must use the controlled owned-result route")
                    ownedControl.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeTranspose(ownedControl)
                    ownedControl.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    reply
                }
                else -> error("Unexpected wire method: $method")
            }
        }

        override suspend fun close() {}
    }

    @Test fun signedAxisSnapshotSurvivesQueueAndOnlyTransposeForcesControl() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val mapping = intArrayOf(-1, 1, 0, -1)
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.transpose(mapping) }
            mapping.fill(0)
            assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            finish.complete(Unit)
            assertEquals("array:7", held.await())
            val view = pending.await()
            val request = calls.requests.single { it.getValue("method").jsonPrimitive.content == "transpose_IntArray" }
            assertEquals("MDArray", request.getValue("type").jsonPrimitive.content)
            assertEquals("7", request.getValue("handle").jsonPrimitive.content)
            assertEquals(JsonArray(listOf(-1, 1, 0, -1).map(::JsonPrimitive)),
                request.getValue("parameters").jsonObject.getValue("newAxisToOldAxis"))
            assertEquals(listOf("getName" to false, "transpose_IntArray" to true), calls.controls)
            view.close()
            assertEquals(listOf("42"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun returnedViewAcceptsFullUnsignedHandleAndKeepsItsParentLifetime() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.reply = """{"result":{"value":"18446744073709551615"},"allocatedHandles":["18446744073709551615"]}"""
            val view = source.transpose(intArrayOf())
            assertEquals("array:18446744073709551615", view.getName())
            calls.reply = """{"result":{"value":"43"},"allocatedHandles":["43"]}"""
            val sibling = source.transpose(intArrayOf(-1))
            view.close(); view.close()
            assertEquals(listOf("18446744073709551615"), calls.closed)
            assertEquals("array:7", source.getName())
            assertEquals("array:43", sibling.getName())
            source.close()
            val before = calls.requests.size
            assertFailsWith<IllegalStateException> { sibling.getName() }
            assertEquals(before, calls.requests.size, "A closed source must reject its view before transport entry")
            sibling.close()
            assertEquals(listOf("18446744073709551615", "7", "43"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun ordinaryCancellationAfterAllocationClosesOnlyTheNewChild() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val sibling = BundledMDArray(engine, "8", source)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeTranspose = { entered.complete(Unit); finish.await() }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.transpose(intArrayOf(1, 0)) }
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

    @Test fun observerFailurePreservesTheOriginalAndSuppressedCleanupFailure() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val expected = IllegalStateException("transpose observer failed")
            calls.closeFailure = "view cleanup failed"
            val failure = assertFailsWith<IllegalStateException> {
                withGdalProgress({ progress -> if (progress.fraction == 1.0) throw expected; true }) {
                    source.transpose(intArrayOf(1, 0))
                }
            }
            assertTrue(failure === expected || failure.cause === expected)
            assertEquals("view cleanup failed", expected.suppressedExceptions.single().message)
            assertEquals(listOf("42"), calls.closed)
            assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    @Test fun observerCancellationAfterCompletionClosesTheAllocatedView() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val fractions = mutableListOf<Double>()
            assertFailsWith<CancellationException> {
                withGdalProgress({ progress -> fractions += progress.fraction; progress.fraction < 1.0 }) {
                    source.transpose(intArrayOf(1, 0))
                }
            }
            assertEquals(listOf(0.0, 1.0), fractions)
            assertEquals(listOf("42"), calls.closed)
            assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    @Test fun malformedRepliesReleaseKnownAllocationsBeforeThrowing() = runTest {
        val values = listOf("null", "true", "42", "\"\"", "\"0\"", "\"-1\"", "\"+42\"", "\"042\"",
            "\"1.5\"", "\"18446744073709551616\"", "{}", "[]", "\"43\"")
        val replies = values.map { """{"result":{"value":$it},"allocatedHandles":["42"]}""" } + listOf(
            """{"result":{},"allocatedHandles":["42"]}""",
            """{"result":true,"allocatedHandles":["42"]}""",
            """{"allocatedHandles":["42"]}""",
            """{"error":{},"allocatedHandles":["42"]}""",
            """{"error":[],"allocatedHandles":["42"]}""",
        )
        for (reply in replies) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                val sibling = BundledMDArray(engine, "8", source)
                calls.reply = reply
                assertFails(reply) { source.transpose(intArrayOf(1, 0)) }
                assertEquals(listOf("42"), calls.closed, reply)
                assertEquals("array:7", source.getName())
                assertEquals("array:8", sibling.getName())
            } finally { engine.close() }
        }
    }

    @Test fun nativeErrorsAndUnknownAllocationMetadataKeepTheirPrimaryFailure() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.reply = """{"error":"native transpose failed","allocatedHandles":["42"]}"""
            assertEquals("native transpose failed", assertFailsWith<GdalException> {
                source.transpose(intArrayOf(1, 0))
            }.message)
            assertEquals(listOf("42"), calls.closed)
            calls.closed.clear()
            calls.reply = """{"error":"native transpose failed","allocatedHandles":{}}"""
            val failure = assertFailsWith<GdalException> { source.transpose(intArrayOf(1, 0)) }
            assertEquals("native transpose failed", failure.message)
            // Coroutine stacktrace recovery may wrap the original primary exception.
            val primary = (failure.cause as? GdalException) ?: failure
            println(buildJsonObject {
                put("allocationMetadataCaughtType", failure.javaClass.name)
                put("caughtSuppressedCount", failure.suppressedExceptions.size)
                put("primaryType", primary.javaClass.name)
                put("primaryMessage", primary.message)
                putJsonArray("primarySuppressed") {
                    primary.suppressedExceptions.forEach { cleanup -> addJsonObject {
                        put("type", cleanup.javaClass.name); put("message", cleanup.message)
                    } }
                }
            })
            assertEquals("native transpose failed", primary.message)
            val cleanup = assertIs<IllegalArgumentException>(primary.suppressedExceptions.single())
            assertContains(cleanup.message.orEmpty(), "is not a JsonArray")
            assertTrue(calls.closed.isEmpty(), "Corrupt allocation metadata cannot safely identify a new child")
            calls.reply = """{"result":{"value":"42"}}"""
            assertFailsWith<IllegalStateException> { source.transpose(intArrayOf(1, 0)) }
            assertTrue(calls.closed.isEmpty())
            assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    @Test fun cancellingAQueuedTransposeDoesNotEnterNativeOrCloseExistingHandles() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.transpose(intArrayOf(1, 0)) }
            pending.cancel(); pending.join()
            finish.complete(Unit)
            assertEquals("array:7", held.await())
            assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            assertTrue(calls.closed.isEmpty())
        } finally { engine.close() }
    }
}
