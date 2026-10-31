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

class MDArrayCoordinateVariablesWireTest {
    private class Calls : GdalTransport {
        val methods = mutableListOf<Pair<String, Boolean>>()
        val closed = mutableListOf<String>()
        val closeFailures = mutableSetOf<String>()
        var next = 42uL
        var response: String? = null
        var ordinaryResponse = """{"result":{"value":"41"},"allocatedHandles":["41"]}"""
        var beforeName: suspend () -> Unit = {}
        var beforeOwned: suspend () -> Unit = {}

        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            val method = args.getValue("method").jsonPrimitive.content
            val handle = args.getValue("handle").jsonPrimitive.content
            methods += method to (control != null)
            return when (method) {
                "close" -> {
                    assertNull(control); closed += handle
                    if (handle in closeFailures) """{"error":"cleanup:$handle","allocatedHandles":[]}"""
                    else """{"result":{"value":null},"allocatedHandles":[]}"""
                }
                "getName" -> {
                    beforeName()
                    buildJsonObject {
                        putJsonObject("result") { put("value", "array:$handle") }
                        put("allocatedHandles", JsonArray(emptyList()))
                    }.toString()
                }
                "openArray_String_List_String" -> {
                    assertNull(control)
                    ordinaryResponse
                }
                "getCoordinateVariables" -> {
                    val ownedControl = assertNotNull(control)
                    ownedControl.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeOwned()
                    ownedControl.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    response ?: run {
                        val ids = JsonArray(List(2) { JsonPrimitive((next++).toString()) })
                        buildJsonObject { putJsonObject("result") { put("value", ids) }; put("allocatedHandles", ids) }.toString()
                    }
                }
                else -> error("Unexpected method: $method")
            }
        }
        override suspend fun close() {}
    }

    @Test fun emptyAndUnsignedListsUseOwnedControlAndRemainScopedToSource() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.response = """{"result":{"value":[]},"allocatedHandles":[]}"""
            assertEquals(emptyList(), source.getCoordinateVariables())
            calls.response = """{"result":{"value":["42","18446744073709551615"]},"allocatedHandles":["42","18446744073709551615"]}"""
            val coordinates = source.getCoordinateVariables()
            assertEquals(listOf("array:42", "array:18446744073709551615"), coordinates.map { it.getName() })
            coordinates[0].close(); coordinates[0].close()
            assertEquals("array:7", source.getName())
            assertEquals("array:18446744073709551615", coordinates[1].getName())
            source.close()
            val before = calls.methods.size
            assertFailsWith<IllegalStateException> { coordinates[1].getName() }
            assertEquals(before, calls.methods.size)
            coordinates[1].close()
            assertEquals(listOf("42", "7", "18446744073709551615"), calls.closed)
            assertTrue(calls.methods.filter { it.first == "getCoordinateVariables" }.all { it.second })
            assertTrue(calls.methods.filter { it.first == "getName" }.all { !it.second })
        } finally { engine.close() }
    }

    @Test fun malformedWholeListsCleanEveryKnownFreshAllocationBeforeAnyWrapping() = runTest {
        val badValues = listOf("null", "true", "42", "\"42\"", "{}", "[null]", "[42]", "[\"\"]",
            "[\"0\"]", "[\"-1\"]", "[\"+42\"]", "[\"042\"]", "[\"1.5\"]",
            "[\"18446744073709551616\"]", "[\"42\",\"42\"]", "[\"42\",\"7\"]")
        val replies = badValues.map { """{"result":{"value":$it},"allocatedHandles":["42","43"]}""" } + listOf(
            """{"result":{},"allocatedHandles":["42","43"]}""",
            """{"result":true,"allocatedHandles":["42","43"]}""",
            """{"allocatedHandles":["42","43"]}""",
            """{"error":{},"allocatedHandles":["42","43"]}""")
        for (reply in replies) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                val sibling = BundledMDArray(engine, "8", source)
                calls.response = reply
                assertFails(reply) { source.getCoordinateVariables() }
                assertEquals(listOf("43", "42"), calls.closed, reply)
                assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
            } finally { engine.close() }
        }
    }

    @Test fun malformedLedgersCannotCloseEarlierHandlesOrRepeatFreshCleanup() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val sibling = BundledMDArray(engine, "8", source)
            calls.response = """{"result":{"value":["42","43"]},"allocatedHandles":["7","8","42","42","043",44,"43"]}"""
            assertFailsWith<IllegalStateException> { source.getCoordinateVariables() }
            assertEquals(listOf("43", "42"), calls.closed)
            assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
        } finally { engine.close() }

        for (ledger in listOf("null", "{}")) {
            val broken = Calls(); val runtime = RuntimeEngine(broken)
            try {
                broken.response = """{"result":{"value":["42"]},"allocatedHandles":$ledger}"""
                assertFailsWith<IllegalStateException> { BundledMDArray(runtime, "7", null).getCoordinateVariables() }
                assertEquals(listOf("42"), broken.closed) // The array-shaped result still identifies a fresh allocation.
            } finally { runtime.close() }
        }
        val missing = Calls(); val runtime = RuntimeEngine(missing)
        try {
            missing.response = """{"result":{"value":["42"]}}"""
            assertFailsWith<IllegalStateException> { BundledMDArray(runtime, "7", null).getCoordinateVariables() }
            assertEquals(listOf("42"), missing.closed); missing.closed.clear()
            missing.response = """{"result":{"value":["42","43"]},"allocatedHandles":[]}"""
            assertFailsWith<IllegalStateException> { BundledMDArray(runtime, "7", null).getCoordinateVariables() }
            assertEquals(listOf("43"), missing.closed)
        } finally { runtime.close() }
        val subset = Calls(); val subsetEngine = RuntimeEngine(subset)
        try {
            subset.response = """{"result":{"value":["42","43"]},"allocatedHandles":["42"]}"""
            assertFailsWith<IllegalStateException> { BundledMDArray(subsetEngine, "7", null).getCoordinateVariables() }
            assertEquals(listOf("43", "42"), subset.closed)
        } finally { subsetEngine.close() }
    }

    @Test fun ordinaryAllocationIsProtectedBeforeItsGeneratedWrapperExists() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val raw = engine.invoke("Group", "1", "openArray_String_List_String", emptyMap())
            assertEquals(JsonPrimitive("41"), raw)
            // Another call can enter after invoke unlocks and before the earlier wrapper is made.
            calls.response = """{"result":{"value":["41","42"]},"allocatedHandles":["41","42"]}"""
            assertFailsWith<IllegalStateException> { source.getCoordinateVariables() }
            assertEquals(listOf("42"), calls.closed)
            val prior = BundledMDArray(engine, raw.jsonPrimitive.content, null)
            assertEquals("array:41", prior.getName()); assertEquals("array:7", source.getName())
            calls.response = """{"result":{"value":["43","44"]},"allocatedHandles":["43","44"]}"""
            val fresh = source.getCoordinateVariables()
            assertEquals(listOf("array:43", "array:44"), fresh.map { it.getName() })
            fresh.forEach { it.close() }; prior.close(); source.close()
        } finally { engine.close() }
    }

    @Test fun ordinaryErrorAllocationAlsoAdvancesTheMonotonicProtectionBoundary() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.ordinaryResponse = """{"error":"ordinary failure","allocatedHandles":["41"]}"""
            assertFailsWith<GdalException> { engine.invoke("Group", "1", "openArray_String_List_String", emptyMap()) }
            assertEquals(listOf("41"), calls.closed); calls.closed.clear()
            calls.response = """{"result":{"value":["41","42"]},"allocatedHandles":["41","42"]}"""
            assertFailsWith<IllegalStateException> { source.getCoordinateVariables() }
            assertEquals(listOf("42"), calls.closed)
            assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    @Test fun queuedAndCompletionCancellationReleaseOnlyTheNewBatch() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val sibling = BundledMDArray(engine, "8", source)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val queued = async(start = CoroutineStart.UNDISPATCHED) { source.getCoordinateVariables() }
            queued.cancel(); queued.join(); finish.complete(Unit); held.await()
            assertEquals(listOf("getName"), calls.methods.map { it.first }); assertTrue(calls.closed.isEmpty())
            calls.beforeName = {}

            val ownedEntered = CompletableDeferred<Unit>(); val ownedFinish = CompletableDeferred<Unit>()
            calls.beforeOwned = { ownedEntered.complete(Unit); ownedFinish.await() }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.getCoordinateVariables() }
            ownedEntered.await(); pending.cancel(); ownedFinish.complete(Unit); pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            assertEquals(listOf("43", "42"), calls.closed)
            assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
            calls.beforeOwned = {}; calls.closed.clear()
            // IDs allocated by the cancelled batch stay protected even after their release.
            calls.response = """{"result":{"value":["42","44"]},"allocatedHandles":["42","44"]}"""
            assertFailsWith<IllegalStateException> { source.getCoordinateVariables() }
            assertEquals(listOf("44"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun nativeAndCleanupFailuresPreservePrimaryAndCloseTheWholeFreshBatch() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.response = """{"error":"primary failure","allocatedHandles":["42","43"]}"""
            calls.closeFailures += listOf("42", "43")
            val failure = assertFailsWith<GdalException> { source.getCoordinateVariables() }
            assertEquals("primary failure", failure.message)
            assertEquals(listOf("43", "42"), calls.closed)
            assertEquals(listOf("cleanup:43", "cleanup:42"), failure.suppressedExceptions.map { it.message })
            assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }
}
