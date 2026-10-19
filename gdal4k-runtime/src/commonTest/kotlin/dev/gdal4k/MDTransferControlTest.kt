package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.MDValue
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class MDTransferControlTest {
    private fun frame(vararg components: String?): ByteArray {
        val result = ArrayList<Byte>()
        for (text in components) {
            val size = text?.length ?: -1
            for (i in 0..3) result.add((size ushr (i * 8)).toByte())
            text?.forEach { result.add(it.code.toByte()) }
        }
        return result.toByteArray()
    }
    private fun metadata(count: Long) = buildJsonObject { put("binaryMDValues", true); put("count", count) }
    @Test fun numericFrameKeepsExactStringsAndComplexComponents() = runTest {
        assertEquals(listOf(MDValue.Numeric("18446744073709551615"), MDValue.Numeric("-0", "NaN")),
            decodeMDValues(metadata(2), frame("18446744073709551615", null, "-0", "NaN")))
        assertEquals(listOf(MDValue.Text("한글")), decodeMDValues(Json.parseToJsonElement("""[{"kind":"text","text":"한글"}]"""), byteArrayOf()))
    }
    @Test fun malformedFramesRejectCountsLengthsTrailingAndNonAscii() = runTest {
        val valid = frame("2", null)
        for (bytes in listOf(valid.copyOf(3), valid + byteArrayOf(0), frame(null, null), frame("é", null), byteArrayOf(-1, -1, -1, 127, 0, 0, 0, 0))) {
            assertFails { decodeMDValues(metadata(1), bytes) }
        }
        assertFails { decodeMDValues(metadata(-1), valid) }; assertFails { decodeMDValues(metadata(2147483648), valid) }
        assertFails { decodeMDValues(JsonArray(emptyList()), valid) }
    }
    @Test fun oldEngineKeepsNumericAndStructuredJsonPaths() = runTest {
        var calls = 0
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryFiles = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                calls++; val a = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
                val method = a.getValue("method").jsonPrimitive.content; val p = a.getValue("parameters").jsonObject
                return when {
                    method == "write_LongArray_LongArray_DoubleArray" -> { assertEquals("[1.25]", p["values"].toString()); """{"result":{"value":true}}""" }
                    method == "read_LongArray_LongArray" -> """{"result":{"value":[1.25]}}"""
                    method.startsWith("writeValues") -> { assertEquals("9223372036854775807", p.getValue("values").jsonArray[0].jsonObject.getValue("real").jsonPrimitive.content); """{"result":{"value":true}}""" }
                    else -> """{"result":{"value":[{"kind":"numeric","real":"9223372036854775807","imaginary":null}]}}"""
                }
            }
            override suspend fun close() {}
        })
        val a = BundledMDArray(engine, "1", null); val value = listOf(MDValue.Numeric(Long.MAX_VALUE))
        assertTrue(a.write(longArrayOf(), longArrayOf(), doubleArrayOf(1.25))); assertContentEquals(doubleArrayOf(1.25), a.read(longArrayOf(), longArrayOf()))
        assertTrue(a.writeValues(longArrayOf(), longArrayOf(), value)); assertEquals(value, a.readValues(longArrayOf(), longArrayOf()))
        assertEquals(4, calls); engine.close()
    }
    @Test fun binaryNumericWriteKeepsStringsAndStructuredWriteFallsBack() = runTest {
        var binary = 0; var json = 0
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryMD = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String { json++; return """{"result":{"value":true}}""" }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply {
                binary++; assertContentEquals(frame("18446744073709551615", null, "-0", "NaN"), input)
                return GdalBinaryReply("""{"result":{"value":true}}""", byteArrayOf())
            }
            override suspend fun close() {}
        })
        val a = BundledAttribute(engine, "1", null)
        assertTrue(a.writeValues(listOf(MDValue.Numeric("18446744073709551615"), MDValue.Numeric("-0", "NaN"))))
        assertTrue(a.writeValues(listOf(MDValue.Text("한글")))); assertEquals(1, binary); assertEquals(1, json); engine.close()
    }
    @Test fun queuedCancellationNeverStartsNumericPackingOrTransport() = runTest {
        val entered = CompletableDeferred<Unit>(); val release = CompletableDeferred<Unit>(); var binary = 0
        val engine = RuntimeEngine(object : GdalTransport {
            override val supportsBinaryMD = true
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String { entered.complete(Unit); release.await(); return """{"result":{"value":null}}""" }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply { binary++; error("unexpected") }
            override suspend fun close() {}
        })
        val first = launch { engine.invoke("Handle", "0", "wait", emptyMap()) }; entered.await()
        // Invalid ASCII would throw during packing if packing ran before acquiring the lock.
        val queued = launch(start = CoroutineStart.UNDISPATCHED) { BundledAttribute(engine, "1", null).writeValues(listOf(MDValue.Numeric("é"))) }
        queued.cancel(); release.complete(Unit); first.join(); queued.join(); assertEquals(0, binary); assertTrue(queued.isCancelled); engine.close()
    }
}
