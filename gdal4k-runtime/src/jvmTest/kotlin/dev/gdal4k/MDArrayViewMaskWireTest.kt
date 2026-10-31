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

class MDArrayViewMaskWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        val controls = mutableListOf<Pair<String, Boolean>>()
        val closed = mutableListOf<String>()
        var reply = """{"result":{"value":"42"},"allocatedHandles":["42"]}"""
        var closeFailure: String? = null
        var beforeName: suspend () -> Unit = {}
        var beforeOwned: suspend () -> Unit = {}

        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            val method = args.getValue("method").jsonPrimitive.content
            val handle = args.getValue("handle").jsonPrimitive.content
            requests += args
            controls += method to (control != null)
            return when (method) {
                "close" -> {
                    assertNull(control); closed += handle
                    closeFailure?.let { buildJsonObject { put("error", it) }.toString() }
                        ?: """{"result":{"value":null}}"""
                }
                "getName" -> {
                    beforeName()
                    buildJsonObject { putJsonObject("result") { put("value", "array:$handle") } }.toString()
                }
                "getView_String", "getMask_List_String" -> {
                    val ownedControl = assertNotNull(control)
                    ownedControl.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeOwned()
                    ownedControl.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    reply
                }
                else -> error("Unexpected wire method: $method")
            }
        }
        override suspend fun close() {}
    }

    private suspend fun view(source: MDArray, mask: Boolean): MDArray =
        if (mask) source.getMask() else source.getView("[::-1]")

    @Test fun viewExpressionAndMaskOptionsUseOwnedControlAndSnapshotBeforeQueueing() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            source.getView("[1]['quote\\'field']").use { assertEquals("array:42", it.getName()) }
            val expression = calls.requests.single { it.getValue("method").jsonPrimitive.content == "getView_String" }
            assertEquals(buildJsonObject { put("viewExpr", "[1]['quote\\'field']") }, expression.getValue("parameters"))
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val options = mutableListOf("UNMASK_FLAGS=land,water")
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.getMask(options) }
            options[0] = "UNMASK_FLAGS=ice"
            finish.complete(Unit); held.await()
            calls.beforeName = {}
            pending.await().use { assertEquals("array:42", it.getName()) }
            val mask = calls.requests.single { it.getValue("method").jsonPrimitive.content == "getMask_List_String" }
            assertEquals(buildJsonObject { put("options", JsonArray(listOf(JsonPrimitive("UNMASK_FLAGS=land,water")))) },
                mask.getValue("parameters"))
            assertEquals("MDArray", mask.getValue("type").jsonPrimitive.content)
            assertEquals("7", mask.getValue("handle").jsonPrimitive.content)
            assertTrue(calls.controls.filter { it.first == "getName" }.all { !it.second })
            assertTrue(calls.controls.filter { it.first.startsWith("getView_") || it.first.startsWith("getMask_") }.all { it.second })
            val before = calls.requests.size
            assertFailsWith<IllegalArgumentException> { source.getView("[:]\u0000[0]") }
            assertFailsWith<IllegalArgumentException> { source.getMask(listOf("X=\u0000")) }
            assertEquals(before, calls.requests.size)
        } finally { engine.close() }
    }

    @Test fun ownedResultsAcceptUnsignedIdsAndRemainScopedToSource() = runTest {
        for (mask in listOf(false, true)) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                calls.reply = """{"result":{"value":"18446744073709551615"},"allocatedHandles":["18446744073709551615"]}"""
                val result = view(source, mask)
                assertEquals("array:18446744073709551615", result.getName())
                if (mask) assertEquals(buildJsonObject { put("options", JsonArray(emptyList())) },
                    calls.requests.first().getValue("parameters"))
                source.close()
                val before = calls.requests.size
                assertFailsWith<IllegalStateException> { result.getName() }
                assertEquals(before, calls.requests.size)
                result.close(); result.close()
                assertEquals(listOf("7", "18446744073709551615"), calls.closed)
            } finally { engine.close() }
        }
    }

    @Test fun queuedCancellationAllocatesNothingAndCompletionCancellationClosesOnlyNewChild() = runTest {
        for (mask in listOf(false, true)) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                val sibling = BundledMDArray(engine, "8", source)
                val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
                calls.beforeName = { entered.complete(Unit); finish.await() }
                val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
                entered.await()
                val queued = async(start = CoroutineStart.UNDISPATCHED) { view(source, mask) }
                queued.cancel(); queued.join(); finish.complete(Unit); held.await()
                assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
                assertTrue(calls.closed.isEmpty()); calls.beforeName = {}

                val ownedEntered = CompletableDeferred<Unit>(); val ownedFinish = CompletableDeferred<Unit>()
                calls.beforeOwned = { ownedEntered.complete(Unit); ownedFinish.await() }
                val pending = async(start = CoroutineStart.UNDISPATCHED) { view(source, mask) }
                ownedEntered.await(); pending.cancel(); ownedFinish.complete(Unit); pending.join()
                assertFailsWith<CancellationException> { pending.await() }
                assertEquals(listOf("42"), calls.closed)
                assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
            } finally { engine.close() }
        }
    }

    @Test fun malformedRepliesAndNativeErrorsCleanKnownOwnedAllocations() = runTest {
        val badValues = listOf("null", "true", "42", "\"\"", "\"0\"", "\"-1\"", "\"+42\"", "\"042\"",
            "\"1.5\"", "\"18446744073709551616\"", "{}", "[]", "\"43\"")
        val replies = badValues.map { """{"result":{"value":$it},"allocatedHandles":["42"]}""" } + listOf(
            """{"result":{},"allocatedHandles":["42"]}""", """{"result":true,"allocatedHandles":["42"]}""",
            """{"allocatedHandles":["42"]}""", """{"error":{},"allocatedHandles":["42"]}""",
            """{"error":"native view failed","allocatedHandles":["42"]}""")
        for (mask in listOf(false, true)) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                val sibling = BundledMDArray(engine, "8", source)
                for (reply in replies) {
                    calls.reply = reply
                    assertFails(reply) { view(source, mask) }
                    assertEquals(listOf("42"), calls.closed, reply); calls.closed.clear()
                    assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
                }
                calls.reply = """{"result":{"value":"42"}}"""
                assertFailsWith<IllegalStateException> { view(source, mask) }
                assertTrue(calls.closed.isEmpty())
                calls.reply = """{"error":"primary failure","allocatedHandles":{}}"""
                val failure = assertFailsWith<GdalException> { view(source, mask) }
                val primary = (failure.cause as? GdalException) ?: failure
                assertEquals("primary failure", primary.message)
                assertIs<IllegalArgumentException>(primary.suppressedExceptions.single())
                assertTrue(calls.closed.isEmpty())
            } finally { engine.close() }
        }
    }

    @Test fun malformedNativeArgumentsCannotAllocateViewsOrChangeSource() = runTest {
        val transport = createTransport(GdalOptions())
        val engine = RuntimeEngine(transport); val runtime = BundledGdalRuntime(engine)
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { dataset ->
                    requireNotNull(dataset.getRootGroup()).use { group ->
                        requireNotNull(group.createDimension("n", 1)).use { dimension ->
                            requireNotNull(group.createArray("anchor", listOf(dimension), GdalDataType.Int16)).use { source ->
                                assertTrue(source.writeValues(longArrayOf(0), longArrayOf(1), listOf(MDValue.Numeric(7))))
                                val id = (source as RuntimeHandle).runtimeId
                                val wrongShapes = listOf<JsonElement>(JsonNull, JsonPrimitive(true), JsonPrimitive(42), JsonPrimitive("text"),
                                    JsonObject(emptyMap()), JsonArray(emptyList()))
                                val cases = wrongShapes.map { "getView_String" to buildJsonObject { put("viewExpr", it) } } +
                                    wrongShapes.filterNot { it is JsonArray }.map {
                                        "getMask_List_String" to buildJsonObject { put("options", it) }
                                    } + listOf(
                                        "getView_String" to JsonObject(emptyMap()),
                                        "getMask_List_String" to JsonObject(emptyMap()),
                                        "getView_String" to buildJsonObject { put("viewExpr", "[:]\u0000[0]") },
                                        "getMask_List_String" to buildJsonObject { put("options", JsonArray(listOf(JsonPrimitive("X=\u0000")))) },
                                        "getMask_List_String" to buildJsonObject { put("options", JsonArray(listOf(JsonPrimitive(1)))) },
                                        "getMask_List_String" to buildJsonObject { put("options", JsonArray(listOf(JsonPrimitive("X=yes"), JsonNull))) })
                                for ((method, parameters) in cases) {
                                    val request = buildJsonObject {
                                        put("op", "runtime")
                                        putJsonObject("args") {
                                            put("type", "MDArray"); put("handle", id); put("method", method)
                                            put("parameters", parameters)
                                        }
                                    }
                                    val reply = Json.parseToJsonElement(transport.call(request.toString())).jsonObject
                                    assertNotNull(reply["error"], request.toString())
                                    assertTrue(reply["allocatedHandles"]?.jsonArray.orEmpty().isEmpty(), request.toString())
                                    assertEquals(listOf(MDValue.Numeric(7)), source.readValues(longArrayOf(0), longArrayOf(1)))
                                }
                                // A literal backslash followed by u0000 is ordinary option text.
                                source.getMask(listOf("IGNORED=\\u0000")).use {
                                    assertEquals(listOf(MDValue.Numeric(1)), it.readValues(longArrayOf(0), longArrayOf(1)))
                                }
                            }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }

    @Test fun observerFailureAndRejectionKeepPrimaryFailureAndCleanup() = runTest {
        for (mask in listOf(false, true)) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null)
                val expected = IllegalStateException("view observer failed")
                calls.closeFailure = "view cleanup failed"
                val failure = assertFailsWith<IllegalStateException> {
                    withGdalProgress({ if (it.fraction == 1.0) throw expected; true }) { view(source, mask) }
                }
                assertTrue(failure === expected || failure.cause === expected)
                assertEquals("view cleanup failed", expected.suppressedExceptions.single().message)
                assertEquals(listOf("42"), calls.closed)
                calls.closeFailure = null; calls.closed.clear()
                assertFailsWith<CancellationException> {
                    withGdalProgress({ it.fraction < 1.0 }) { view(source, mask) }
                }
                assertEquals(listOf("42"), calls.closed)
                assertEquals("array:7", source.getName())
            } finally { engine.close() }
        }
    }
}
