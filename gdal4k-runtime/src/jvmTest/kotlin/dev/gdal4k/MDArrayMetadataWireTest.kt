package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class MDArrayMetadataWireTest {
    private class Calls : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        var response = "null"
        var failure: String? = null
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val args = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            requests += args
            if (args.getValue("method").jsonPrimitive.content == "close") return """{"result":{"value":null}}"""
            return failure ?: """{"result":{"value":$response}}"""
        }
        override suspend fun close() {}
    }

    @Test fun unsignedBlocksAndSignedAxisMappingRemainExactAndDetached(): Unit = runBlocking {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val array = BundledMDArray(engine, "array", null)
            calls.response = """["0","9007199254740993","18446744073709551615"]"""
            val blocks = array.getBlockSize()
            assertEquals(listOf(0uL, 9_007_199_254_740_993uL, ULong.MAX_VALUE), blocks)
            assertEquals("getBlockSize", calls.requests.last().getValue("method").jsonPrimitive.content)
            assertEquals(JsonObject(emptyMap()), calls.requests.last().getValue("parameters"))
            val srs = BundledSpatialReference(engine, "srs", null)
            calls.response = "[-2,1]"
            val mapping = srs.getDataAxisToSRSAxisMapping(); mapping[0] = 2
            assertContentEquals(intArrayOf(-2, 1), srs.getDataAxisToSRSAxisMapping())
            calls.response = """["COMPRESS=DEFLATE","VALUE=a=b","LABEL=영상"]"""
            assertEquals(listOf("COMPRESS=DEFLATE", "VALUE=a=b", "LABEL=영상"), array.getStructuralInfo())
            assertEquals(listOf(0uL, 9_007_199_254_740_993uL, ULong.MAX_VALUE), blocks)
            for (invalid in listOf("""["-1"]""", """["18446744073709551616"]""", "[1.5]", "{}")) {
                calls.response = invalid
                assertFails(invalid) { array.getBlockSize() }
            }
            for (invalid in listOf("[2147483648]", "[1.5]", "{}")) {
                calls.response = invalid
                assertFails(invalid) { srs.getDataAxisToSRSAxisMapping() }
            }
        } finally { engine.close() }
    }

    @Test fun extendedNumericMetadataPreservesPresenceStorageCodeAndNonFiniteValues(): Unit = runBlocking {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val array = BundledMDArray(engine, "array", null)
            assertNull(array.getScaleEx()); assertNull(array.getOffsetEx())
            calls.response = """{"value":0,"storageType":0}"""
            assertEquals(MDNumericMetadata(0.0, GdalDataType.Unknown), array.getScaleEx())
            calls.response = """{"value":1,"storageType":6}"""
            assertEquals(MDNumericMetadata(1.0, GdalDataType.Float32), array.getOffsetEx())
            calls.response = """{"value":"-0.0","storageType":7}"""
            assertEquals((-0.0).toBits(), requireNotNull(array.getOffsetEx()).value.toBits())
            calls.response = """{"value":"NaN","storageType":0}"""
            assertTrue(requireNotNull(array.getScaleEx()).value.isNaN())
            for (value in listOf("Infinity", "-Infinity")) {
                calls.response = """{"value":"$value","storageType":7}"""
                assertEquals(MDNumericMetadata(value.toDouble(), GdalDataType.Float64), array.getScaleEx())
            }
            for (invalid in listOf("{}", "[]", "true", """{"value":1}""", """{"storageType":7}""",
                """{"value":"invalid","storageType":7}""", """{"value":1,"storageType":2147483648}""")) {
                calls.response = invalid
                assertFails(invalid) { array.getScaleEx() }
                assertFails(invalid) { array.getOffsetEx() }
            }
            calls.failure = """{"error":"metadata lookup failed"}"""
            assertEquals("metadata lookup failed", assertFailsWith<GdalException> { array.getScaleEx() }.message)
            assertEquals("metadata lookup failed", assertFailsWith<GdalException> { array.getOffsetEx() }.message)
        } finally { engine.close() }
    }

    @Test fun spatialReferenceUsesIndependentLifetimeAndClosesExactlyOnce(): Unit = runBlocking {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            val group = BundledGroup(engine, "group", null)
            val array = BundledMDArray(engine, "array", group)
            assertNull(array.getSpatialRef())
            calls.response = "\"crs-copy\""
            val srs = requireNotNull(array.getSpatialRef())
            array.close(); group.close()
            calls.response = "[-2,1]"
            assertContentEquals(intArrayOf(-2, 1), srs.getDataAxisToSRSAxisMapping())
            srs.close(); srs.close()
            assertFails { srs.getDataAxisToSRSAxisMapping() }
            val closed = calls.requests.filter { it.getValue("method").jsonPrimitive.content == "close" }
                .map { it.getValue("handle").jsonPrimitive.content }
            assertEquals(listOf("array", "group", "crs-copy"), closed)
        } finally { engine.close() }
    }

    @Test fun failedCrsResultReleasesAllocatedHandleBeforePropagatingFailure(): Unit = runBlocking {
        val calls = Calls(); val engine = RuntimeEngine(calls)
        try {
            calls.failure = """{"error":"Cannot obtain multidimensional spatial reference","allocatedHandles":["unclaimed-crs"]}"""
            val array = BundledMDArray(engine, "array", null)
            assertEquals("Cannot obtain multidimensional spatial reference", assertFailsWith<GdalException> { array.getSpatialRef() }.message)
            assertEquals(listOf("getSpatialRef", "close"), calls.requests.map { it.getValue("method").jsonPrimitive.content })
            assertEquals("unclaimed-crs", calls.requests.last().getValue("handle").jsonPrimitive.content)
        } finally { engine.close() }
    }
}
