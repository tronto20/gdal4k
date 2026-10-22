package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

/** Capture the counter incremented only by the callback registered with GDALMultiDimTranslate.
 * Wrapper start/end, validation and binary checkpoints cannot satisfy this assertion.
 */
class MultiDimTranslateNativeCallbacksTest {
    private class NativeCalls : GdalTransport {
        private val bridge = JniBridge()
        private val context: Long
        val callbacks = mutableMapOf<String, Long>()
        private val operations = setOf("multiDimTranslate", "multiDimTranslateFiles", "multiDimTranslateTo")
        init { loadBundledLibrary(null); context = bridge.create(byteArrayOf(), byteArrayOf()); check(context != 0L) }
        override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
            val name = Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject.getValue("method").jsonPrimitive.content.substringBefore('_')
            if (name in operations) assertNotNull(control, "$name must retain native cancellation control")
            if (control == null) return bridge.execute(context, request.encodeToByteArray()).decodeToString()
            val token = bridge.controlCreate(); check(token != 0L)
            try {
                val response = bridge.executeControlled(context, request.encodeToByteArray(), token).decodeToString()
                val snapshot = bridge.controlSnapshot(token).decodeToString()
                if (name in operations) {
                    val nativeCalls = Json.parseToJsonElement(snapshot).jsonObject.getValue("multiDimTranslateProgressCalls").jsonPrimitive.long
                    assertTrue(nativeCalls > 0, "$name did not invoke its registered GDAL progress callback")
                    callbacks[name] = nativeCalls
                }
                control.accept(snapshot)
                return response
            } finally { bridge.controlDestroy(token) }
        }
        override suspend fun close() { bridge.destroy(context) }
    }
    @Test fun memoryFilesAndDirectConversionExecuteActualNativeCallbacks(): Unit = runBlocking {
        val calls = NativeCalls(); val engine = RuntimeEngine(calls); val runtime = BundledGdalRuntime(engine)
        val input = "/vsimem/multidimensional-callbacks/source.vrt"
        val output = "/vsimem/multidimensional-callbacks/output.tif"
        try {
            val xml = """<VRTDataset><Group name="/"><Dimension name="y" size="4"/><Dimension name="x" size="5"/><Array name="values"><DataType>Float64</DataType><DimensionRef ref="/y"/><DimensionRef ref="/x"/><InlineValues>${(0 until 20).joinToString(" ") { (it + 0.25).toString() }}</InlineValues></Array></Group></VRTDataset>"""
            runtime.writeMemoryFile(input, xml.encodeToByteArray())
            runtime.openDataset(input, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { source ->
                source.multiDimTranslate().use { result -> requireNotNull(result.getRootGroup()).use { group -> requireNotNull(group.openArray("values")).use { a ->
                    assertContentEquals(DoubleArray(20) { it + 0.25 }, a.read(longArrayOf(0, 0), longArrayOf(4, 5)))
                } } }
                runtime.openDataset(source.multiDimTranslateFiles("-array", "values")).use { assertEquals(5, it.getRasterXSize()) }
                source.multiDimTranslateTo(output, "-array", "values")
                runtime.openDataset(output).use { assertEquals(4, it.getRasterYSize()) }
            }
            assertEquals(setOf("multiDimTranslate", "multiDimTranslateFiles", "multiDimTranslateTo"), calls.callbacks.keys)
        } finally {
            try { runtime.deleteMemoryFile("/vsimem/multidimensional-callbacks") } finally { engine.close() }
        }
    }
}
