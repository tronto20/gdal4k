package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class MDJsonSubtypeTest {
    private class Calls(override val supportsMDJsonStringSubtype: Boolean = false) : GdalTransport {
        val requests = mutableListOf<JsonObject>()
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            requests += Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject
            return """{"result":{"value":null}}"""
        }
        override suspend fun close() {}
    }

    @Test fun oldTransportRefusesJsonSchemasBeforeAnyGeneratedWrapperRequest(): Unit = runBlocking {
        val calls = Calls()
        val engine = RuntimeEngine(calls)
        try {
            val group = BundledGroup(engine, "group", null)
            val array = BundledMDArray(engine, "array", null)
            val nested = MDDataType.Compound("outer", listOf(MDComponent("payload",
                MDDataType.Compound("inner", listOf(MDComponent("json", MDDataType.JsonText()))))))
            for (schema in listOf(MDDataType.JsonText(), nested)) {
                assertFailsWith<IllegalStateException> { group.createArray("json", emptyList(), schema) }
                assertFailsWith<IllegalStateException> { group.createAttribute("json", longArrayOf(), schema) }
                assertFailsWith<IllegalStateException> { array.createAttribute("json", longArrayOf(), schema) }
                assertTrue(calls.requests.isEmpty())
            }
            // The same actual wrappers still send legacy Text requests.
            assertNull(group.createArray("plain", emptyList(), MDDataType.Text()))
            assertNull(group.createAttribute("plain", longArrayOf(), MDDataType.Text()))
            assertNull(array.createAttribute("plain", longArrayOf(), MDDataType.Text()))
            assertEquals(3, calls.requests.size)
            for (request in calls.requests) {
                val type = request.getValue("parameters").jsonObject.getValue("dataType").jsonObject
                assertEquals("text", type.getValue("kind").jsonPrimitive.content)
                assertFalse(type.containsKey("subType"))
            }
        } finally { engine.close() }
    }

    @Test fun capableGeneratedWrapperCarriesExactJsonMetadataAndLength(): Unit = runBlocking {
        val calls = Calls(supportsMDJsonStringSubtype = true)
        val engine = RuntimeEngine(calls)
        try {
            val group = BundledGroup(engine, "group", null)
            val schema = MDDataType.JsonText(9_007_199_254_740_993)
            assertNull(group.createArray("json", emptyList(), schema))
            val encoded = calls.requests.single().getValue("parameters").jsonObject.getValue("dataType")
            assertEquals(1, encoded.jsonObject.getValue("subType").jsonPrimitive.int)
            assertEquals("9007199254740993", encoded.jsonObject.getValue("maxLength").jsonPrimitive.content)
            assertEquals(schema, decodeMDType(encoded))
        } finally { engine.close() }
    }

    @Test fun subtypeDecodingIsStrictAndRecursiveWithoutInferringFromValues() {
        fun schema(subtype: String?) = Json.parseToJsonElement(
            """{"kind":"text","maxLength":"3"${subtype?.let { ",\"subType\":$it" } ?: ""}}"""
        )
        assertEquals(MDDataType.Text(3), decodeMDType(schema(null)))
        assertEquals(MDDataType.Text(3), decodeMDType(schema("0")))
        assertEquals(MDDataType.Text(3), decodeMDType(schema("-0")))
        assertEquals(MDDataType.JsonText(3), decodeMDType(schema("1")))
        for (invalid in listOf("null", "true", "false", "1.0", "1e0", "0e0", "1E0", "-0e1", "\"1\"", "2", "-1", "2147483648", "{}", "[]")) {
            assertFailsWith<IllegalArgumentException>(invalid) { decodeMDType(schema(invalid)) }
            val compound = buildJsonObject {
                put("kind", "compound"); put("name", "record")
                putJsonArray("components") { addJsonObject { put("name", "payload"); put("type", schema(invalid)) } }
            }
            assertFailsWith<IllegalArgumentException>(invalid) { decodeMDType(compound) }
        }
        val plain = MDDataType.Text(3)
        assertEquals(3L, plain.component1())
        assertEquals(MDDataType.Text(5), plain.copy(maxLength = 5))
        assertNotEquals<MDDataType>(plain, MDDataType.JsonText(3))
        assertFailsWith<IllegalArgumentException> { MDDataType.JsonText(-1) }
    }
}
