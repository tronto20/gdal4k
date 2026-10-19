package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class MDTransferWireTest {
    private fun request(type: String, id: String, method: String, parameters: String) =
        """{"op":"runtime","args":{"type":"$type","handle":"$id","method":"$method","parameters":$parameters}}"""
    @Test fun malformedNumericFramesCannotPartiallyOverwriteArrayOrAttribute(): Unit = runBlocking {
        val transport = createTransport(GdalOptions()); assertTrue(transport.supportsBinaryMD)
        val engine = RuntimeEngine(transport); val runtime = BundledGdalRuntime(engine)
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { ds ->
                    requireNotNull(ds.getRootGroup()).use { group ->
                        val dimension = requireNotNull(group.createDimension("n", 1))
                        requireNotNull(group.createArray("values", listOf(dimension), GdalDataType.UInt64)).use { array ->
                            requireNotNull(array.createAttribute("id", longArrayOf(), MDDataType.Numeric(GdalDataType.UInt64))).use { attribute ->
                                val original = listOf(MDValue.Numeric("18446744073709551615"))
                                assertTrue(array.writeValues(longArrayOf(0), longArrayOf(1), original)); assertTrue(attribute.writeValues(original))
                                val valid = byteArrayOf(1, 0, 0, 0, 55, -1, -1, -1, -1)
                                for ((type, handle, method, shape) in listOf(
                                    listOf("MDArray", (array as RuntimeHandle).runtimeId, "writeValues_LongArray_LongArray_List_MDValue", "\"start\":[0],\"count\":[1],"),
                                    listOf("Attribute", (attribute as RuntimeHandle).runtimeId, "writeValues_List_MDValue", ""))) {
                                    for (bytes in listOf(valid.copyOf(3), valid + byteArrayOf(1), byteArrayOf(-1, -1, -1, 127, -1, -1, -1, -1))) {
                                        val reply = transport.callBinary(request(type, handle, method, "{$shape\"numericValueCount\":1}"), bytes, null)
                                        assertNotNull(Json.parseToJsonElement(reply.json).jsonObject["error"])
                                        assertEquals(original, array.readValues(longArrayOf(0), longArrayOf(1))); assertEquals(original, attribute.readValues())
                                    }
                                    val wrong = transport.callBinary(request(type, handle, method, "{$shape\"numericValueCount\":2}"), valid, null)
                                    assertNotNull(Json.parseToJsonElement(wrong.json).jsonObject["error"])
                                }
                            }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
    @Test fun float64FramesRequireExactShapeAndLengthBeforeWriting(): Unit = runBlocking {
        val transport = createTransport(GdalOptions()); assertTrue(transport.supportsBinaryMD)
        val engine = RuntimeEngine(transport); val runtime = BundledGdalRuntime(engine)
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { ds ->
                    requireNotNull(ds.getRootGroup()).use { group ->
                        requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { array ->
                            assertTrue(array.write(longArrayOf(), longArrayOf(), doubleArrayOf(12.5)))
                            val id = (array as RuntimeHandle).runtimeId
                            for ((count, bytes) in listOf("0" to ByteArray(8), "1" to ByteArray(7), "1" to ByteArray(9), "\"1\"" to ByteArray(8))) {
                                val reply = transport.callBinary(request("MDArray", id, "write_LongArray_LongArray_DoubleArray", """{"start":[],"count":[],"valueCount":$count}"""), bytes, null)
                                assertNotNull(Json.parseToJsonElement(reply.json).jsonObject["error"])
                                assertContentEquals(doubleArrayOf(12.5), array.read(longArrayOf(), longArrayOf()))
                            }
                            val reply = transport.callBinary(request("MDArray", id, "read_LongArray_LongArray", """{"start":[],"count":[],"valueCount":1}"""), byteArrayOf(0), null)
                            assertNotNull(Json.parseToJsonElement(reply.json).jsonObject["error"])
                            assertFails { transport.callBinary(request("MDArray", id, "getName", "{}"), null, null) }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
}
