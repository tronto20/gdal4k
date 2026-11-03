package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArrayPlanningWireTest {
    private class NativeBudgetCalls : GdalTransport {
        private val bridge = JniBridge()
        private val context: Long
        var replacement: JsonElement? = null
        var lastPlanningReply: JsonObject? = null
        init {
            loadBundledLibrary(null)
            context = bridge.create(byteArrayOf(), byteArrayOf())
            check(context != 0L)
        }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val root = Json.parseToJsonElement(request).jsonObject
            val args = root.getValue("args").jsonObject
            val method = args.getValue("method").jsonPrimitive.content
            val bad = replacement
            val payload = if (method == "getProcessingChunkSize_ULong" && bad != null) {
                val parameters = args.getValue("parameters").jsonObject
                JsonObject(root + ("args" to JsonObject(args + ("parameters" to
                    JsonObject(parameters + ("maxChunkMemory" to bad))))))
            } else root
            val reply = bridge.execute(context, payload.toString().encodeToByteArray()).decodeToString()
            if (method == "getProcessingChunkSize_ULong") lastPlanningReply = Json.parseToJsonElement(reply).jsonObject
            return reply
        }
        override suspend fun close() { bridge.destroy(context) }
    }

    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        var value = "\"1\""
        var ledger: String? = "[]"
        var error: String? = null
        var includeValueWithError = false
        var closeError: String? = null
        var beforePlanning: suspend () -> Unit = {}
        var beforeName: suspend () -> Unit = {}
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            requests += args
            return when (val method = args.getValue("method").jsonPrimitive.content) {
                "getTotalElementsCount", "getProcessingChunkSize_ULong" -> {
                    assertNotNull(control)
                    beforePlanning()
                    val fields = mutableMapOf<String, JsonElement>()
                    if (error == null || includeValueWithError) fields["result"] = buildJsonObject { put("value", Json.parseToJsonElement(value)) }
                    error?.let { fields["error"] = JsonPrimitive(it) }
                    ledger?.let { fields["allocatedHandles"] = Json.parseToJsonElement(it) }
                    JsonObject(fields).toString()
                }
                "getCoordinateVariables" -> """{"result":{"value":["900"]},"allocatedHandles":["900"]}"""
                "getName" -> { beforeName(); """{"result":{"value":"array"},"allocatedHandles":[]}""" }
                "close" -> closeError?.let { buildJsonObject { put("error", it) }.toString() }
                    ?: """{"result":{"value":null},"allocatedHandles":[]}"""
                else -> error("Unexpected method $method")
            }
        }
        fun closedIds() = requests.filter { it.getValue("method").jsonPrimitive.content == "close" }
            .map { it.getValue("handle").jsonPrimitive.content }
        override suspend fun close() {}
    }

    private suspend fun fixture(block: suspend (Calls, BundledMDArray) -> Unit) {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try { block(calls, BundledMDArray(engine, "7", null)) } finally { engine.close() }
    }

    @Test fun fullWidthCountsAndChunksAreDetachedDataWithExactBudgets() = runTest {
        fixture { calls, source ->
            val counts = listOf(0uL, 1uL, 9_007_199_254_740_993uL, 9_223_372_036_854_775_808uL, ULong.MAX_VALUE)
            for (count in counts) {
                calls.value = JsonPrimitive(count.toString()).toString()
                assertEquals(count, source.getTotalElementsCount())
                assertEquals(JsonObject(emptyMap()), calls.requests.last().getValue("parameters"))
            }
            calls.value = JsonArray(counts.map { JsonPrimitive(it.toString()) }).toString()
            var snapshot = emptyList<ULong>()
            for (budget in listOf(0uL, 4_294_967_295uL, 4_294_967_296uL, 9_223_372_036_854_775_808uL, ULong.MAX_VALUE)) {
                snapshot = source.getProcessingChunkSize(budget)
                assertEquals(counts, snapshot)
                assertEquals(buildJsonObject { put("maxChunkMemory", budget.toString()) }, calls.requests.last().getValue("parameters"))
            }
            // Counts must never become the watermark used to validate fresh owned handles.
            val coordinate = source.getCoordinateVariables().single()
            assertEquals("array", coordinate.getName()); coordinate.close()
            source.close(); assertEquals(counts, snapshot)
            assertEquals(listOf("900", "7"), calls.closedIds())
        }
    }

    @Test fun onlyCanonicalStringUInt64ValuesAndListsAreAccepted() = runTest {
        fixture { calls, source ->
            val bad = listOf("", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "1e2", "18446744073709551616")
                .map { JsonPrimitive(it).toString() } + listOf("0", "1e2", "1.5", "true", "null", "{}", "[]")
            for (item in bad) {
                calls.value = item; assertFails(item) { source.getTotalElementsCount() }
                calls.value = "[$item]"; assertFails(item) { source.getProcessingChunkSize(32uL) }
            }
            for (wrongList in listOf("null", "{}", "\"1\"", "1")) {
                calls.value = wrongList; assertFails { source.getProcessingChunkSize(0uL) }
            }
            calls.value = "[]"; assertEquals(emptyList(), source.getProcessingChunkSize(0uL))
            assertEquals(emptyList(), calls.closedIds())
            calls.value = "\"0\""; assertEquals(0uL, source.getTotalElementsCount())
        }
    }

    @Test fun ordinaryRepliesRequireEmptyLedgersAndCleanupOnlyFreshDeclaredHandles() = runTest {
        fixture { calls, source ->
            calls.value = "\"18446744073709551615\""
            for (ledger in listOf(null, "null", "{}", "false", "[\"7\"]", "[7]", "[\"01\"]")) {
                calls.ledger = ledger
                assertFails { source.getTotalElementsCount() }
                assertEquals(emptyList(), calls.closedIds())
            }
            calls.ledger = "[\"8\",\"8\",\"7\",\"0\",\"01\",9]"
            calls.value = "[\"18446744073709551615\",\"7\"]"
            assertFails { source.getProcessingChunkSize(0uL) }
            assertEquals(listOf("8"), calls.closedIds())
            calls.ledger = "[]"; calls.value = "\"1\""
            assertEquals(1uL, source.getTotalElementsCount()); assertEquals("array", source.getName())
            val coordinate = source.getCoordinateVariables().single(); coordinate.close()
            assertEquals(listOf("8", "900"), calls.closedIds())
        }
    }

    @Test fun errorsAndMalformedValuesNeverTreatCountsAsCleanupIds() = runTest {
        fixture { calls, source ->
            calls.value = "\"18446744073709551615\""; calls.error = "native planning failure"
            assertEquals("native planning failure", assertFailsWith<GdalException> { source.getTotalElementsCount() }.message)
            assertFailsWith<GdalException> { source.getProcessingChunkSize(0uL) }
            calls.error = null; calls.value = "[\"1\",\"18446744073709551615\",null]"
            assertFails { source.getProcessingChunkSize(0uL) }
            assertEquals(emptyList(), calls.closedIds())
            val coordinate = source.getCoordinateVariables().single(); coordinate.close()
            assertEquals(listOf("900"), calls.closedIds())
        }
    }

    @Test fun nativeErrorsOutrankLedgerAndCleanupFailuresWithoutClosingCounts() = runTest {
        fixture { calls, source ->
            calls.value = "\"18446744073709551615\""; calls.includeValueWithError = true
            calls.error = "native planning failure"; calls.ledger = "[\"8\",\"8\",\"7\"]"
            calls.closeError = "fresh handle cleanup failed"
            val failure = assertFailsWith<GdalException> { source.getTotalElementsCount() }
            assertEquals("native planning failure", failure.message)
            val primary = (failure.cause as? GdalException) ?: failure
            assertEquals("native planning failure", primary.message)
            assertTrue(primary.suppressedExceptions.any { it.message?.contains("invalid detached allocation ledger") == true })
            assertTrue(primary.suppressedExceptions.any { it.message == "fresh handle cleanup failed" })
            assertEquals(listOf("8"), calls.closedIds())
            calls.error = null; calls.closeError = null; calls.ledger = "[]"; calls.value = "\"1\""
            assertEquals(1uL, source.getTotalElementsCount()); assertEquals("array", source.getName())
            val coordinate = source.getCoordinateVariables().single(); coordinate.close()
            assertEquals(listOf("8", "900"), calls.closedIds())
        }
    }

    @Test fun queuedAndCompletionCancellationPreserveOwnersAndNeverCloseCountPayloads() = runTest {
        fixture { calls, source ->
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val queued = async(start = CoroutineStart.UNDISPATCHED) { source.getTotalElementsCount() }
            queued.cancel(); queued.join(); finish.complete(Unit); held.await()
            assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            calls.beforeName = {}
            val started = CompletableDeferred<Unit>(); val done = CompletableDeferred<Unit>()
            calls.beforePlanning = { started.complete(Unit); done.await() }
            calls.value = "[\"18446744073709551615\"]"
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.getProcessingChunkSize(0uL) }
            started.await(); pending.cancel(); done.complete(Unit); pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            assertEquals(emptyList(), calls.closedIds())
            val corruptStarted = CompletableDeferred<Unit>(); val corruptDone = CompletableDeferred<Unit>()
            calls.beforePlanning = { corruptStarted.complete(Unit); corruptDone.await() }
            calls.ledger = "[\"8\"]"
            calls.error = "native failure after cancellation"
            val corrupt = async(start = CoroutineStart.UNDISPATCHED) { source.getProcessingChunkSize(0uL) }
            corruptStarted.await(); corrupt.cancel(); corruptDone.complete(Unit); corrupt.join()
            assertFailsWith<CancellationException> { corrupt.await() }
            assertEquals(listOf("8"), calls.closedIds())
            calls.ledger = "[]"; calls.error = null
            calls.beforePlanning = {}; calls.value = "\"1\""
            assertEquals(1uL, source.getTotalElementsCount()); assertEquals("array", source.getName())
        }
    }

    /** Real dispatcher fixture; excluded from the standalone fake-transport verification. */
    @Test fun malformedNativeBudgetsCannotChangeSource(): Unit = runBlocking {
        val calls = NativeBudgetCalls(); val engine = RuntimeEngine(calls)
        try {
            val runtime = BundledGdalRuntime(engine)
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { dataset ->
                    requireNotNull(dataset.getRootGroup()).use { group ->
                        requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { scalar ->
                            val invalid = listOf<JsonElement>(JsonNull, JsonPrimitive(1), JsonPrimitive(true),
                                JsonArray(emptyList()), JsonObject(emptyMap())) +
                                listOf("", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "1e2",
                                    "18446744073709551616", "1\u0000x").map(::JsonPrimitive)
                            for (value in invalid) {
                                calls.replacement = value
                                assertFailsWith<GdalException>(value.toString()) { scalar.getProcessingChunkSize(0uL) }
                                assertEquals(JsonArray(emptyList()), assertNotNull(calls.lastPlanningReply).getValue("allocatedHandles"))
                                assertEquals(1uL, scalar.getTotalElementsCount())
                            }
                            calls.replacement = null
                            assertEquals(emptyList(), scalar.getProcessingChunkSize(0uL))
                            assertEquals(emptyList(), scalar.getProcessingChunkSize(ULong.MAX_VALUE))
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
}
