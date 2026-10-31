package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArrayStatisticsWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        var stats = """{"minimum":1,"maximum":5,"mean":3,"standardDeviation":1.4142135623730951,"validCount":"5"}"""
        var error: String? = null
        var beforeStats: suspend () -> Unit = {}
        var beforeName: suspend () -> Unit = {}
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            requests += args
            val method = args.getValue("method").jsonPrimitive.content
            return when (method) {
                "getStatistics_Boolean_Boolean", "computeStatistics_Boolean_List_String" -> {
                    val actual = assertNotNull(control)
                    actual.accept("""{"sequence":1,"fraction":0,"message":"start"}""")
                    beforeStats()
                    actual.accept("""{"sequence":2,"fraction":1,"message":"done"}""")
                    error?.let { buildJsonObject { put("error", it) }.toString() }
                        ?: """{"result":{"value":$stats},"allocatedHandles":[]}"""
                }
                "getCoordinateVariables" -> """{"result":{"value":["42"]},"allocatedHandles":["42"]}"""
                "getName" -> {
                    beforeName()
                    """{"result":{"value":"array"},"allocatedHandles":[]}"""
                }
                "close" -> """{"result":{"value":null},"allocatedHandles":[]}"""
                else -> error("Unexpected method: $method")
            }
        }
        override suspend fun close() {}
    }

    private suspend fun fixture(block: suspend (Calls, BundledMDArray) -> Unit) {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try { block(calls, BundledMDArray(engine, "7", null)) } finally { engine.close() }
    }

    @Test fun signaturesPreserveNullOnlyForLookupAndReturnDetachedSnapshots() = runTest {
        fixture { calls, source ->
            calls.stats = "null"
            assertNull(source.getStatistics())
            assertEquals("getStatistics_Boolean_Boolean", calls.requests.last().getValue("method").jsonPrimitive.content)
            assertEquals(buildJsonObject { put("approximate", false); put("force", false) }, calls.requests.last().getValue("parameters"))
            assertFails { source.computeStatistics() }
            calls.stats = """{"minimum":1,"maximum":5,"mean":3,"standardDeviation":0,"validCount":"5"}"""
            val snapshot = assertNotNull(source.getStatistics(approximate = true, force = true))
            assertEquals(buildJsonObject { put("approximate", true); put("force", true) }, calls.requests.last().getValue("parameters"))
            assertEquals(snapshot, source.computeStatistics(true, listOf("UPDATE_METADATA=YES")))
            assertEquals(buildJsonObject { put("approximate", true); put("options", JsonArray(listOf(JsonPrimitive("UPDATE_METADATA=YES")))) }, calls.requests.last().getValue("parameters"))
            source.close()
            assertEquals(MDStatistics(1.0, 5.0, 3.0, 0.0, 5uL), snapshot)
        }
    }

    @Test fun fullUnsignedCountsAreDataAndNeverBecomeHandleWatermarks() = runTest {
        fixture { calls, source ->
            for (count in listOf("0", "9007199254740993", "9223372036854775808", "18446744073709551615")) {
                calls.stats = """{"minimum":0,"maximum":0,"mean":0,"standardDeviation":0,"validCount":"$count"}"""
                assertEquals(count.toULong(), source.computeStatistics().validCount)
                assertEquals(count.toULong(), assertNotNull(source.getStatistics()).validCount)
            }
            // If a count were observed as an allocated handle, this fresh batch would be rejected.
            val coordinates = source.getCoordinateVariables()
            assertEquals(1, coordinates.size); assertEquals("array", coordinates.single().getName())
            coordinates.single().close(); assertEquals("array", source.getName())
        }
    }

    @Test fun zeroCountSentinelsAndCanonicalNonfiniteMomentsAreValid() = runTest {
        fixture { calls, source ->
            calls.stats = """{"minimum":1.7976931348623157E308,"maximum":-1.7976931348623157E308,"mean":0,"standardDeviation":0,"validCount":"0"}"""
            assertEquals(MDStatistics(Double.MAX_VALUE, -Double.MAX_VALUE, 0.0, 0.0, 0uL), source.computeStatistics())
            calls.stats = """{"minimum":"-Infinity","maximum":"Infinity","mean":"NaN","standardDeviation":"NaN","validCount":"2"}"""
            val stats = source.computeStatistics()
            assertEquals(Double.NEGATIVE_INFINITY, stats.minimum); assertEquals(Double.POSITIVE_INFINITY, stats.maximum)
            assertTrue(stats.mean.isNaN()); assertTrue(stats.standardDeviation.isNaN()); assertEquals(2uL, stats.validCount)
        }
    }

    @Test fun malformedCountsObjectsAndMomentsAreRejectedWithoutCachedAbsence() = runTest {
        fixture { calls, source ->
            val good = Json.parseToJsonElement(calls.stats).jsonObject
            val badCounts = listOf("-1", "+1", "00", "01", "1.0", "1e2", "18446744073709551616", "", " 1", "1 ")
            val bad = mutableListOf("true", "0", "[]", "\"snapshot\"")
            for (count in badCounts) bad += JsonObject(good + ("validCount" to JsonPrimitive(count))).toString()
            for (count in listOf<JsonElement>(JsonPrimitive(1), JsonPrimitive(true), JsonNull, JsonObject(emptyMap()))) {
                bad += JsonObject(good + ("validCount" to count)).toString()
            }
            for (moment in listOf<JsonElement>(JsonPrimitive("1"), JsonPrimitive("nan"), JsonPrimitive("+Infinity"), JsonPrimitive(true), JsonNull, JsonArray(emptyList()), Json.parseToJsonElement("1e999"))) {
                bad += JsonObject(good + ("mean" to moment)).toString()
            }
            for (key in good.keys) bad += JsonObject(good - key).toString()
            bad += JsonObject(good + ("unexpected" to JsonPrimitive(0))).toString()
            for (reply in bad) {
                calls.stats = reply
                assertFails(reply) { source.getStatistics() }
                assertFails(reply) { source.computeStatistics() }
            }
            calls.stats = good.toString(); assertEquals(5uL, source.computeStatistics().validCount)
            calls.error = "native computation failed"
            assertEquals("native computation failed", assertFailsWith<GdalException> { source.getStatistics() }.message)
            assertEquals("native computation failed", assertFailsWith<GdalException> { source.computeStatistics() }.message)
            calls.error = null; assertEquals("array", source.getName())
        }
    }

    @Test fun optionsAreValidatedAndSnapshottedBeforeQueuedWork() = runTest {
        fixture { calls, source ->
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val options = mutableListOf("UPDATE_METADATA=NO")
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.computeStatistics(options = options) }
            options[0] = "UPDATE_METADATA=YES"
            val before = calls.requests.size
            assertFailsWith<IllegalArgumentException> { source.computeStatistics(options = listOf("BAD\u0000VALUE")) }
            @Suppress("UNCHECKED_CAST")
            val invalid = listOf(true) as List<String>
            assertFails { source.computeStatistics(options = invalid) }
            assertEquals(before, calls.requests.size)
            finish.complete(Unit); held.await(); pending.await()
            val args = calls.requests.last().getValue("parameters").jsonObject
            assertEquals(JsonArray(listOf(JsonPrimitive("UPDATE_METADATA=NO"))), args.getValue("options"))
        }
    }

    @Test fun queuedAndCompletionCancellationKeepSourceAndSiblingUsable() = runTest {
        fixture { calls, source ->
            val entered = CompletableDeferred<Unit>(); val finish = CompletableDeferred<Unit>()
            calls.beforeName = { entered.complete(Unit); finish.await() }
            val held = async(start = CoroutineStart.UNDISPATCHED) { source.getName() }
            entered.await()
            val queued = async(start = CoroutineStart.UNDISPATCHED) { source.getStatistics(force = true) }
            queued.cancel(); queued.join(); finish.complete(Unit); held.await()
            assertEquals(listOf("getName"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            calls.beforeName = {}
            val statsEntered = CompletableDeferred<Unit>(); val statsFinish = CompletableDeferred<Unit>()
            calls.beforeStats = { statsEntered.complete(Unit); statsFinish.await() }
            val pending = async(start = CoroutineStart.UNDISPATCHED) { source.computeStatistics() }
            statsEntered.await(); pending.cancel(); statsFinish.complete(Unit); pending.join()
            assertFailsWith<CancellationException> { pending.await() }
            calls.beforeStats = {}
            val sibling = source.getCoordinateVariables().single()
            assertEquals("array", source.getName()); assertEquals("array", sibling.getName())
            assertEquals(5uL, source.computeStatistics().validCount); sibling.close()
        }
    }

    @Test fun observersReceiveBothOperationsAndTheirPrimaryFailuresSurvive() = runTest {
        fixture { calls, source ->
            val events = mutableListOf<GdalProgress>()
            withGdalProgress({ events += it; true }) { source.getStatistics(force = true); source.computeStatistics() }
            assertEquals(setOf("MDArray.getStatistics", "MDArray.computeStatistics"), events.map { it.operation }.toSet())
            assertTrue(events.all { it.fraction in 0.0..1.0 })
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.getStatistics(force = true) } }
            val expected = IllegalArgumentException("statistics observer failure")
            calls.error = "native interrupt"
            val actual = assertFailsWith<IllegalArgumentException> { withGdalProgress({ throw expected }) { source.computeStatistics() } }
            assertTrue(actual === expected || actual.cause === expected)
            calls.error = null; assertEquals(5uL, source.computeStatistics().validCount)
            assertEquals("array", source.getName())
        }
    }
}
