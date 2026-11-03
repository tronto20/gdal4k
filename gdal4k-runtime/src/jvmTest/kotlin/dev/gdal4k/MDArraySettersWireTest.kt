package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArraySettersWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        val applied = mutableListOf<String>()
        val closeFailures = mutableSetOf<String>()
        var value = "true"
        var ledger: String? = "[]"
        var error: String? = null
        var beforeName: suspend () -> Unit = {}
        var beforeMutation: suspend () -> Unit = {}
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            requests += args
            return when (val method = args.getValue("method").jsonPrimitive.content) {
                "setSpatialRef_SpatialReference", "setScale_Double_Int", "setOffset_Double_Int" -> {
                    assertNotNull(control)
                    applied += method
                    beforeMutation()
                    mutationReply()
                }
                "setScale_Double", "setOffset_Double" -> mutationReply()
                "getCoordinateVariables" -> """{"result":{"value":["900"]},"allocatedHandles":["900"]}"""
                "getAuthorityCode_String" -> """{"result":{"value":"4326"},"allocatedHandles":[]}"""
                "getName" -> { beforeName(); """{"result":{"value":"array"},"allocatedHandles":[]}""" }
                "close" -> {
                    val id = args.getValue("handle").jsonPrimitive.content
                    if (id in closeFailures) buildJsonObject { put("error", "cleanup:$id") }.toString()
                    else """{"result":{"value":null},"allocatedHandles":[]}"""
                }
                else -> error("Unexpected method $method")
            }
        }
        private fun mutationReply(): String {
            val fields = mutableMapOf<String, JsonElement>("result" to buildJsonObject {
                put("value", Json.parseToJsonElement(value))
            })
            error?.let { fields["error"] = JsonPrimitive(it) }
            ledger?.let { fields["allocatedHandles"] = Json.parseToJsonElement(it) }
            return JsonObject(fields).toString()
        }
        fun closedIds() = requests.filter { it.getValue("method").jsonPrimitive.content == "close" }
            .map { it.getValue("handle").jsonPrimitive.content }
        override suspend fun close() {}
    }

    private suspend fun fixture(block: suspend (Calls, BundledMDArray, BundledSpatialReference) -> Unit) {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try { block(calls, BundledMDArray(engine, "7", null), BundledSpatialReference(engine, "11", null)) }
        finally { engine.close() }
    }

    @Test fun newFullSignaturesPreserveLegacyRoutesAndEncodeNullableSrsAndDoubleValues() = runTest {
        fixture { calls, source, srs ->
            assertTrue(source.setScale(2.5)); assertTrue(source.setOffset(-1.25))
            assertEquals(listOf("setScale_Double", "setOffset_Double"), calls.requests.map {
                it.getValue("method").jsonPrimitive.content
            })
            assertEquals(buildJsonObject { put("scale", 2.5) }, calls.requests[0].getValue("parameters"))
            assertEquals(buildJsonObject { put("offset", -1.25) }, calls.requests[1].getValue("parameters"))
            assertTrue(source.setScale(2.5, GdalDataType.Float32))
            assertEquals("setScale_Double_Int", calls.requests.last().getValue("method").jsonPrimitive.content)
            assertEquals(buildJsonObject { put("scale", 2.5); put("storageType", GdalDataType.Float32) }, calls.requests.last().getValue("parameters"))
            calls.value = "false"
            assertFalse(source.setOffset(-1.25, GdalDataType.Unknown))
            assertEquals("setOffset_Double_Int", calls.requests.last().getValue("method").jsonPrimitive.content)
            assertEquals(buildJsonObject { put("offset", -1.25); put("storageType", 0) }, calls.requests.last().getValue("parameters"))
            calls.value = "true"
            for ((number, text) in listOf(Double.NaN to "NaN", Double.POSITIVE_INFINITY to "Infinity", Double.NEGATIVE_INFINITY to "-Infinity")) {
                assertTrue(source.setScale(number, GdalDataType.Float64))
                assertEquals(JsonPrimitive(text), calls.requests.last().getValue("parameters").jsonObject.getValue("scale"))
            }
            assertTrue(source.setSpatialRef(null))
            assertEquals("setSpatialRef_SpatialReference", calls.requests.last().getValue("method").jsonPrimitive.content)
            assertEquals(buildJsonObject { put("spatialRef", JsonNull) }, calls.requests.last().getValue("parameters"))
            assertTrue(source.setSpatialRef(srs))
            assertEquals(buildJsonObject { putJsonObject("spatialRef") { put("type", "SpatialReference"); put("handle", "11") } }, calls.requests.last().getValue("parameters"))
            source.close()
            assertEquals("4326", srs.getAuthorityCode("GEOGCS"))
            assertEquals(listOf("7"), calls.closedIds())
        }
    }

    @Test fun invalidNewEnumsAndClosedForeignFakeInputsFailBeforeTransport() = runTest {
        fixture { calls, source, srs ->
            for (code in listOf(-1, 17, Int.MAX_VALUE, Int.MIN_VALUE)) {
                assertFailsWith<IllegalArgumentException> { source.setScale(2.0, code) }
                assertFailsWith<IllegalArgumentException> { source.setOffset(2.0, code) }
            }
            val other = RuntimeEngine(Calls())
            try {
                val foreign = BundledSpatialReference(other, "11", null)
                val fake = object : SpatialReference by srs {}
                assertFailsWith<IllegalArgumentException> { source.setSpatialRef(foreign) }
                assertFails { source.setSpatialRef(fake) }
                assertTrue(calls.requests.isEmpty())
            } finally { other.close() }
            srs.close(); val before = calls.requests.size
            assertFails { source.setSpatialRef(srs) }
            assertEquals(before, calls.requests.size)
            source.close(); val closed = calls.requests.size
            assertFails { source.setSpatialRef(null) }
            assertFails { source.setScale(1.0, GdalDataType.Float32) }
            assertFails { source.setOffset(1.0, GdalDataType.Float32) }
            assertEquals(closed, calls.requests.size)
        }
    }

    @Test fun newAcceptanceResultsRequireActualBooleansWithoutChangingLegacyDecoding() = runTest {
        fixture { calls, source, _ ->
            for (bad in listOf("null", "0", "1", "\"true\"", "\"false\"", "\"18446744073709551615\"", "[]", "{}")) {
                calls.value = bad
                assertFails(bad) { source.setSpatialRef(null) }
                assertFails(bad) { source.setScale(2.0, GdalDataType.Float32) }
                assertFails(bad) { source.setOffset(2.0, GdalDataType.Float32) }
            }
            calls.value = "\"true\""
            assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(2.0))
            assertTrue(calls.closedIds().isEmpty())
            val coordinate = source.getCoordinateVariables().single(); coordinate.close()
            assertEquals(listOf("900"), calls.closedIds())
        }
    }

    @Test fun ordinaryNewSettersRequireEmptyLedgersAndCleanupOnlyFreshDeclaredIds() = runTest {
        fixture { calls, source, srs ->
            for (bad in listOf(null, "null", "{}", "false", "[\"7\",\"11\"]", "[12]", "[\"01\"]")) {
                calls.ledger = bad
                assertFails { source.setSpatialRef(srs) }
                assertFails { source.setScale(2.0, GdalDataType.Float32) }
                assertTrue(calls.closedIds().isEmpty())
            }
            // Old one-argument calls retain their existing ordinary-response behavior.
            calls.ledger = null; assertTrue(source.setScale(2.0)); assertTrue(source.setOffset(2.0))
            calls.ledger = "[\"12\",\"12\",\"7\",\"11\",\"0\",13]"
            assertFails { source.setOffset(2.0, GdalDataType.Float32) }
            assertEquals(listOf("12"), calls.closedIds())
            calls.ledger = "[]"; assertTrue(source.setSpatialRef(srs))
            assertEquals("array", source.getName()); assertEquals("4326", srs.getAuthorityCode("GEOGCS"))
            val coordinate = source.getCoordinateVariables().single(); coordinate.close()
            assertEquals(listOf("12", "900"), calls.closedIds())
        }
    }

    @Test fun nativeErrorsKeepOriginalPrimaryAndAllLedgerCleanupFailures() = runTest {
        fixture { calls, source, srs ->
            calls.error = "native metadata failure"
            calls.ledger = "[\"12\",\"13\",\"12\",\"7\",\"11\"]"
            calls.closeFailures += listOf("12", "13")
            val failure = assertFailsWith<GdalException> { source.setSpatialRef(srs) }
            assertEquals("native metadata failure", failure.message)
            val primary = (failure.cause as? GdalException) ?: failure
            assertEquals("native metadata failure", primary.message)
            assertEquals(3, primary.suppressedExceptions.size)
            assertContains(primary.suppressedExceptions[0].message.orEmpty(), "invalid detached allocation ledger")
            assertEquals(listOf("cleanup:13", "cleanup:12"), primary.suppressedExceptions.drop(1).map { it.message })
            assertEquals(listOf("13", "12"), calls.closedIds())
            calls.error = null; calls.ledger = "[]"; calls.closeFailures.clear()
            assertTrue(source.setScale(2.0, GdalDataType.Unknown))
            assertEquals("array", source.getName()); assertEquals("4326", srs.getAuthorityCode("GEOGCS"))
        }
    }

    @Test fun queuedAndCompletionCancellationAreBoundaryChecksWithoutMutationRollback() = runTest {
        fixture { calls, source, srs ->
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }; entered.await()
            val queued = async(start = CoroutineStart.UNDISPATCHED) { source.setSpatialRef(srs) }
            queued.cancel(); queued.join(); finish.complete(Unit); held.await()
            assertTrue(calls.applied.isEmpty()); calls.beforeName = {}
            val applied = CompletableDeferred<Unit>(); val complete = CompletableDeferred<Unit>()
            calls.beforeMutation = { applied.complete(Unit); complete.await() }
            calls.ledger = "[\"12\"]"; calls.error = "native failure after cancellation"
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.setScale(2.0, GdalDataType.Float32) }
            applied.await(); pending.cancel(); complete.complete(Unit); pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            assertEquals(listOf("setScale_Double_Int"), calls.applied)
            assertEquals(listOf("12"), calls.closedIds())
            calls.beforeMutation = {}; calls.ledger = "[]"; calls.error = null
            assertTrue(source.setOffset(1.0, GdalDataType.Float64))
            assertEquals("array", source.getName()); assertEquals("4326", srs.getAuthorityCode("GEOGCS"))
        }
    }

    private class NativeCalls : GdalTransport {
        private val bridge = JniBridge()
        private val context: Long
        var targetMethod = "setScale_Double_Int"
        var replacement: Pair<String, JsonElement>? = null
        var lastSetterReply: JsonObject? = null
        init { loadBundledLibrary(null); context = bridge.create(byteArrayOf(), byteArrayOf()); check(context != 0L) }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val root = Json.parseToJsonElement(request).jsonObject
            val args = root.getValue("args").jsonObject
            val method = args.getValue("method").jsonPrimitive.content
            val bad = replacement
            val payload = if (method == targetMethod && bad != null) {
                val parameters = args.getValue("parameters").jsonObject
                JsonObject(root + ("args" to JsonObject(args + ("parameters" to JsonObject(parameters + bad)))))
            } else root
            val text = bridge.execute(context, payload.toString().encodeToByteArray()).decodeToString()
            if (method in setOf("setScale_Double_Int", "setOffset_Double_Int", "setSpatialRef_SpatialReference"))
                lastSetterReply = Json.parseToJsonElement(text).jsonObject
            return text
        }
        override suspend fun close() { bridge.destroy(context) }
    }

    /** Real JNI dispatcher preparation, excluded from standalone fake-transport executions. */
    @Test fun malformedNativeSetterArgumentsCannotChangeMetadata(): Unit = runBlocking {
        val calls = NativeCalls(); val engine = RuntimeEngine(calls)
        try {
            val runtime = BundledGdalRuntime(engine)
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { dataset ->
                    requireNotNull(dataset.getRootGroup()).use { group ->
                        requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { source ->
                            assertTrue(source.write(longArrayOf(), longArrayOf(), doubleArrayOf(3.0)))
                            assertTrue(source.setScale(2.5, GdalDataType.Float32))
                            assertTrue(source.setOffset(-1.25, GdalDataType.Float64))
                            runtime.createSpatialReference().use { srs ->
                                assertEquals(0, srs.importFromEPSG(4326)); assertTrue(source.setSpatialRef(srs))
                                val scale = source.getScaleEx(); val offset = source.getOffsetEx()
                                suspend fun unchanged() {
                                    assertEquals(scale, source.getScaleEx()); assertEquals(offset, source.getOffsetEx())
                                    assertContentEquals(doubleArrayOf(3.0), source.read(longArrayOf(), longArrayOf()))
                                    requireNotNull(source.getSpatialRef()).use { assertEquals("4326", it.getAuthorityCode("GEOGCS")) }
                                    assertEquals(JsonArray(emptyList()), assertNotNull(calls.lastSetterReply).getValue("allocatedHandles"))
                                }
                                val wrongTypes = listOf<JsonElement>(JsonNull, JsonPrimitive(true), JsonPrimitive("6"),
                                    JsonPrimitive(1.5), JsonArray(emptyList()), JsonObject(emptyMap()))
                                calls.targetMethod = "setScale_Double_Int"
                                for (value in wrongTypes + listOf(JsonPrimitive(-1), JsonPrimitive(17), JsonPrimitive(Int.MAX_VALUE))) {
                                    calls.replacement = "storageType" to value
                                    assertFailsWith<GdalException> { source.setScale(9.0, GdalDataType.Float32) }; unchanged()
                                }
                                val wrongValues = listOf<JsonElement>(JsonNull, JsonPrimitive(true), JsonArray(emptyList()),
                                    JsonObject(emptyMap()), Json.parseToJsonElement("1e999")) +
                                    listOf("", "2.5", "nan", "+Infinity", "NaN\u0000").map(::JsonPrimitive)
                                for (value in wrongValues) {
                                    calls.replacement = "scale" to value
                                    assertFailsWith<GdalException> { source.setScale(9.0, GdalDataType.Float32) }; unchanged()
                                }
                                calls.targetMethod = "setSpatialRef_SpatialReference"
                                val arrayId = (source as RuntimeHandle).runtimeId
                                val badSrs = listOf<JsonElement>(JsonPrimitive(1), JsonPrimitive("null"), JsonArray(emptyList()),
                                    buildJsonObject { put("type", "MDArray"); put("handle", arrayId) },
                                    buildJsonObject { put("type", "SpatialReference"); put("handle", arrayId) },
                                    buildJsonObject { put("type", "SpatialReference"); put("handle", "0") },
                                    buildJsonObject { put("type", "SpatialReference"); put("handle", "01") })
                                for (value in badSrs) {
                                    calls.replacement = "spatialRef" to value
                                    assertFailsWith<GdalException> { source.setSpatialRef(srs) }; unchanged()
                                }
                                calls.replacement = null
                                assertTrue(source.setSpatialRef(null)); assertNull(source.getSpatialRef())
                                assertTrue(source.setScale(Double.NaN, GdalDataType.Float32))
                                assertTrue(assertNotNull(source.getScaleEx()).value.isNaN())
                                assertContentEquals(doubleArrayOf(3.0), source.read(longArrayOf(), longArrayOf()))
                            }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
}
