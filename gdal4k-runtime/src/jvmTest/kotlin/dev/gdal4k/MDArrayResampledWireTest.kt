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

class MDArrayResampledWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        val closed = mutableListOf<String>()
        val closeFailures = mutableSetOf<String>()
        var next = 42uL
        var reply: String? = null
        var beforeName: suspend () -> Unit = {}
        var beforeOwned: suspend () -> Unit = {}
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            requests += args
            val method = args.getValue("method").jsonPrimitive.content
            val id = args.getValue("handle").jsonPrimitive.content
            return when {
                method == "close" -> {
                    assertNull(control); closed += id
                    if (id in closeFailures) """{"error":"cleanup:$id","allocatedHandles":[]}"""
                    else """{"result":{"value":null},"allocatedHandles":[]}"""
                }
                method == "getName" -> {
                    assertNull(control); beforeName()
                    """{"result":{"value":"array:$id"},"allocatedHandles":[]}"""
                }
                method == "openArray_String_List_String" -> {
                    assertNull(control)
                    """{"result":{"value":"41"},"allocatedHandles":["41"]}"""
                }
                method.startsWith("getResampled_") -> {
                    val owned = assertNotNull(control)
                    owned.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeOwned()
                    owned.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    reply ?: (next++).toString().let {
                        """{"result":{"value":"$it"},"allocatedHandles":["$it"]}"""
                    }
                }
                else -> error("Unexpected method: $method")
            }
        }
        override suspend fun close() {}
    }

    @Test fun nullableDimensionOrderEnumSrsAndOptionsSnapshotBeforeQueueing() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val dimension = BundledDimension(engine, "10", null)
            val srs = BundledSpatialReference(engine, "11", null)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val dimensions = mutableListOf<Dimension?>(null, dimension, null)
            val options = mutableListOf("IGNORED=original")
            val pending = async(start = CoroutineStart.UNDISPATCHED) {
                source.getResampled(dimensions, RasterResampling.Rms, srs, options)
            }
            dimensions[1] = null; dimensions += dimension; options[0] = "IGNORED=changed"
            finish.complete(Unit); held.await(); calls.beforeName = {}
            pending.await().use { assertEquals("array:42", it.getName()) }
            val request = calls.requests.single { it.getValue("method").jsonPrimitive.content.startsWith("getResampled_") }
            assertEquals("getResampled_List_Dimension_RasterResampling_SpatialReference_List_String",
                request.getValue("method").jsonPrimitive.content)
            assertEquals("MDArray", request.getValue("type").jsonPrimitive.content)
            assertEquals("7", request.getValue("handle").jsonPrimitive.content)
            assertEquals(buildJsonObject {
                put("newDimensions", JsonArray(listOf(JsonNull, buildJsonObject { put("handle", "10"); put("type", "Dimension") }, JsonNull)))
                put("resampling", 14)
                putJsonObject("targetSRS") { put("handle", "11"); put("type", "SpatialReference") }
                put("options", JsonArray(listOf(JsonPrimitive("IGNORED=original"))))
            }, request.getValue("parameters"))
            source.getResampled(listOf(null, null)).close()
            assertEquals(buildJsonObject {
                put("newDimensions", JsonArray(listOf(JsonNull, JsonNull))); put("resampling", 0)
                put("targetSRS", JsonNull); put("options", JsonArray(emptyList()))
            }, calls.requests.last { it.getValue("method").jsonPrimitive.content.startsWith("getResampled_") }.getValue("parameters"))
        } finally { engine.close() }
    }

    @Test fun foreignClosedFakeHandlesAndNulOptionsFailBeforeTransport() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls); val other = RuntimeEngine(Calls())
        try {
            val source = BundledMDArray(engine, "7", null)
            val dimension = BundledDimension(engine, "10", null)
            val srs = BundledSpatialReference(engine, "11", null)
            val foreignDimension = BundledDimension(other, "10", null)
            val foreignSrs = BundledSpatialReference(other, "11", null)
            val fakeDimension = object : Dimension by dimension {}
            val fakeSrs = object : SpatialReference by srs {}
            assertFails { source.getResampled(listOf(fakeDimension, null)) }
            assertFails { source.getResampled(listOf(null, null), targetSRS = fakeSrs) }
            assertFailsWith<IllegalArgumentException> { source.getResampled(listOf(foreignDimension, null)) }
            assertFailsWith<IllegalArgumentException> { source.getResampled(listOf(null, null), targetSRS = foreignSrs) }
            assertFailsWith<IllegalArgumentException> { source.getResampled(listOf(null, null), options = listOf("X=\u0000")) }
            assertTrue(calls.requests.isEmpty())
            dimension.close(); srs.close(); val before = calls.requests.size
            assertFailsWith<IllegalStateException> { source.getResampled(listOf(dimension, null)) }
            assertFailsWith<IllegalStateException> { source.getResampled(listOf(null, null), targetSRS = srs) }
            assertEquals(before, calls.requests.size)
        } finally { engine.close(); other.close() }
    }

    @Test fun independentOwnedViewUsesUnsignedIdAndSourceLifetime() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null); val sibling = BundledMDArray(engine, "8", source)
            calls.reply = """{"result":{"value":"18446744073709551615"},"allocatedHandles":["18446744073709551615"]}"""
            val view = source.getResampled(listOf(null, null))
            assertEquals("array:18446744073709551615", view.getName())
            view.close(); view.close()
            assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
            assertEquals(listOf("18446744073709551615"), calls.closed)
        } finally { engine.close() }
        val second = Calls(); val runtime = RuntimeEngine(second)
        try {
            val ancestor = BundledGroup(runtime, "4", null); val source = BundledMDArray(runtime, "7", ancestor)
            val view = source.getResampled(listOf(null, null)); val child = BundledDimension(runtime, "43", view as RuntimeHandle)
            ancestor.close(); val before = second.requests.size
            assertFailsWith<IllegalStateException> { view.getName() }
            assertFailsWith<IllegalStateException> { child.getName() }
            assertEquals(before, second.requests.size)
        } finally { runtime.close() }
    }

    @Test fun malformedResultsAndLedgersReleaseOnlyIdentifiableFreshAllocations() = runTest {
        val bad = listOf("null", "true", "42", "{}", "[]", "\"0\"", "\"-1\"", "\"+42\"", "\"042\"", "\"18446744073709551616\"", "\"7\"", "\"8\"")
            .map { """{"result":{"value":$it},"allocatedHandles":["7","8","42","43"]}""" } + listOf(
                """{"result":{},"allocatedHandles":["42","43"]}""",
                """{"result":true,"allocatedHandles":["42","43"]}""",
                """{"error":{},"allocatedHandles":["42","43"]}""",
                """{"allocatedHandles":["42","43"]}""",
                """{"result":{"value":"42"},"allocatedHandles":["42","43"]}""",
                """{"result":{"value":"42"},"allocatedHandles":["42","42","043",44,"43"]}""")
        for (reply in bad) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                val source = BundledMDArray(engine, "7", null); val sibling = BundledMDArray(engine, "8", source)
                calls.reply = reply
                assertFails(reply) { source.getResampled(listOf(null, null)) }
                assertEquals(listOf("43", "42"), calls.closed, reply)
                assertEquals("array:7", source.getName()); assertEquals("array:8", sibling.getName())
            } finally { engine.close() }
        }
        for (ledger in listOf("null", "{}", "[]")) {
            val calls = Calls(); val engine = RuntimeEngine(calls)
            try {
                calls.reply = """{"result":{"value":"42"},"allocatedHandles":$ledger}"""
                assertFails { BundledMDArray(engine, "7", null).getResampled(listOf(null, null)) }
                assertEquals(listOf("42"), calls.closed)
            } finally { engine.close() }
        }
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            calls.reply = """{"result":{"value":"42"}}"""
            assertFails { BundledMDArray(engine, "7", null).getResampled(listOf(null, null)) }
            assertEquals(listOf("42"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun earlierOrdinaryAndPreviouslyReleasedIdsCannotBeClaimedOrClosed() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            assertEquals(JsonPrimitive("41"), engine.invoke("Group", "1", "openArray_String_List_String", emptyMap()))
            calls.reply = """{"result":{"value":"41"},"allocatedHandles":["41","42"]}"""
            assertFails { source.getResampled(listOf(null, null)) }
            assertEquals(listOf("42"), calls.closed); calls.closed.clear()
            calls.reply = """{"result":{"value":"42"},"allocatedHandles":["42"]}"""
            assertFails { source.getResampled(listOf(null, null)) }
            assertTrue(calls.closed.isEmpty())
            calls.reply = """{"result":{"value":"43"},"allocatedHandles":["43"]}"""
            source.getResampled(listOf(null, null)).use { assertEquals("array:43", it.getName()) }
            assertEquals(listOf("43"), calls.closed); assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    @Test fun queuedAndCompletionCancellationPreserveSourceAndCloseFreshViewOnce() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }; entered.await()
            val queued = async(start = CoroutineStart.UNDISPATCHED) { source.getResampled(listOf(null, null)) }
            queued.cancel(); queued.join(); finish.complete(Unit); held.await(); calls.beforeName = {}
            assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            val nativeEntered = CompletableDeferred<Unit>(); val nativeFinish = CompletableDeferred<Unit>()
            calls.beforeOwned = { nativeEntered.complete(Unit); nativeFinish.await() }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.getResampled(listOf(null, null)) }
            nativeEntered.await(); pending.cancel(); nativeFinish.complete(Unit); pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            assertEquals(listOf("42"), calls.closed); assertEquals("array:7", source.getName())
            calls.beforeOwned = {}; calls.closed.clear()
            source.getResampled(listOf(null, null)).close(); assertEquals(listOf("43"), calls.closed)
        } finally { engine.close() }
    }

    @Test fun nativeObserverAndCleanupFailuresKeepPrimaryFailure() = runTest {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val source = BundledMDArray(engine, "7", null)
            calls.reply = """{"error":"native primary","allocatedHandles":["7","42"]}"""; calls.closeFailures += "42"
            val native = assertFailsWith<GdalException> { source.getResampled(listOf(null, null)) }
            assertEquals("native primary", native.message); assertEquals("cleanup:42", native.suppressedExceptions.single().message)
            assertEquals(listOf("42"), calls.closed); calls.closed.clear()
            calls.reply = """{"result":{"value":"43"},"allocatedHandles":["43"]}"""; calls.closeFailures += "43"
            val expected = IllegalStateException("observer primary")
            val observer = assertFailsWith<IllegalStateException> {
                withGdalProgress({ if (it.fraction == 1.0) throw expected; true }) { source.getResampled(listOf(null, null)) }
            }
            assertTrue(observer === expected || observer.cause === expected)
            assertEquals("cleanup:43", expected.suppressedExceptions.single().message)
            assertEquals(listOf("43"), calls.closed); calls.closed.clear(); calls.closeFailures.clear()
            calls.reply = """{"result":{"value":"44"},"allocatedHandles":["44"]}"""
            assertFailsWith<CancellationException> {
                withGdalProgress({ it.fraction < 1.0 }) { source.getResampled(listOf(null, null)) }
            }
            assertEquals(listOf("44"), calls.closed); assertEquals("array:7", source.getName())
        } finally { engine.close() }
    }

    // This exercises the real native parser in ordinary runtime CI; the standalone wire runner
    // compiles it but deliberately excludes it from its fake-transport execution.
    @Test fun malformedNativeArgumentsCannotAllocateViewsOrChangeSource() = runTest {
        val transport = createTransport(GdalOptions()); val engine = RuntimeEngine(transport)
        try {
            requireNotNull(BundledGdalRuntime(engine).getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { dataset ->
                    requireNotNull(dataset.getRootGroup()).use { group ->
                        requireNotNull(group.createDimension("y", 1)).use { y ->
                            requireNotNull(group.createDimension("x", 1)).use { x ->
                                requireNotNull(group.createArray("anchor", listOf(y, x), GdalDataType.Int16)).use { source ->
                                    assertTrue(source.writeValues(longArrayOf(0, 0), longArrayOf(1, 1), listOf(MDValue.Numeric(7))))
                                    val id = (source as RuntimeHandle).runtimeId
                                    val valid = buildJsonObject {
                                        put("newDimensions", JsonArray(listOf(JsonNull, JsonNull))); put("resampling", 0)
                                        put("targetSRS", JsonNull); put("options", JsonArray(emptyList()))
                                    }
                                    fun changed(key: String, value: JsonElement) = JsonObject(valid + (key to value))
                                    val wrong = listOf<JsonElement>(JsonNull, JsonPrimitive(true), JsonPrimitive(42), JsonPrimitive("42"), JsonObject(emptyMap()))
                                    val cases = wrong.map { changed("newDimensions", it) } +
                                        listOf(7, -1, 8, 2147483647).map { changed("resampling", JsonPrimitive(it)) } +
                                        listOf(JsonPrimitive(true), JsonPrimitive(0.0), JsonPrimitive("0"), JsonNull).map { changed("resampling", it) } +
                                        wrong.filterNot { it is JsonNull }.map { changed("targetSRS", it) } +
                                        wrong.map { changed("options", it) } + listOf(
                                            JsonObject(valid - "targetSRS"), JsonObject(valid - "newDimensions"),
                                            changed("newDimensions", JsonArray(listOf(JsonNull))),
                                            changed("newDimensions", JsonArray(listOf(buildJsonObject { put("type", "MDArray"); put("handle", id) }, JsonNull))),
                                            changed("newDimensions", JsonArray(listOf(buildJsonObject { put("type", "Dimension"); put("handle", id) }, JsonNull))),
                                            changed("newDimensions", JsonArray(listOf(buildJsonObject { put("type", "Dimension"); put("handle", "0") }, JsonNull))),
                                            changed("newDimensions", JsonArray(listOf(buildJsonObject { put("type", "Dimension"); put("handle", "01") }, JsonNull))),
                                            changed("newDimensions", JsonArray(listOf(buildJsonObject { put("type", "Dimension"); put("handle", "18446744073709551616") }, JsonNull))),
                                            changed("targetSRS", buildJsonObject { put("type", "SpatialReference"); put("handle", id) }),
                                            changed("options", JsonArray(listOf(JsonPrimitive("X=\u0000")))),
                                            changed("options", JsonArray(listOf(JsonPrimitive(1)))))
                                    for (parameters in cases) {
                                        val request = buildJsonObject {
                                            put("op", "runtime"); putJsonObject("args") {
                                                put("type", "MDArray"); put("handle", id)
                                                put("method", "getResampled_List_Dimension_RasterResampling_SpatialReference_List_String")
                                                put("parameters", parameters)
                                            }
                                        }
                                        val reply = Json.parseToJsonElement(transport.call(request.toString())).jsonObject
                                        assertNotNull(reply["error"], request.toString())
                                        assertTrue(reply["allocatedHandles"]?.jsonArray.orEmpty().isEmpty(), request.toString())
                                        assertEquals(listOf(MDValue.Numeric(7)), source.readValues(longArrayOf(0, 0), longArrayOf(1, 1)))
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
}
