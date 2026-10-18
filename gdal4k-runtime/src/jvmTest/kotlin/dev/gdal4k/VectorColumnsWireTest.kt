package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class VectorColumnsWireTest {
    @Test fun nativeNonemptyInputFailsAndReleasesTheArrowStream(): Unit = runBlocking {
        val transport = createTransport(GdalOptions()); assertTrue(transport.supportsBinaryColumns)
        val engine = RuntimeEngine(transport); val runtime = BundledGdalRuntime(engine)
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver -> requireNotNull(driver.createVector("")).use { ds ->
                requireNotNull(ds.createLayer("records", geometryType = 100)).use { layer ->
                    layer.openColumnStream(1).use { stream ->
                        val id = (stream as RuntimeHandle).runtimeId
                        val request = """{"op":"runtime","args":{"type":"FeatureColumnStream","handle":"$id","method":"read","parameters":{}}}"""
                        val reply = transport.callBinary(request, byteArrayOf(1), null)
                        assertNotNull(Json.parseToJsonElement(reply.json).jsonObject["error"])
                        assertTrue(reply.bytes.isEmpty())
                    }
                    layer.resetReading()
                    layer.openColumnStream(1).use { assertNull(it.read()) }
                }
            } }
        } finally { engine.close() }
    }
}
