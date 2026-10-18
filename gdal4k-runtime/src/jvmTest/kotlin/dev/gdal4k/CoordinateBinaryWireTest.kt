package dev.gdal4k

import dev.gdal4k.internal.*
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.*
import kotlin.test.*

class CoordinateBinaryWireTest {
    @Test fun malformedNativePayloadIsRejectedWithoutDamagingTheHandle():Unit=runBlocking {
        val transport=createTransport(GdalOptions());assertTrue(transport.supportsBinaryCoordinates)
        val engine=RuntimeEngine(transport);val runtime=BundledGdalRuntime(engine)
        try {runtime.createSpatialReference("EPSG:4326").use { s -> runtime.createSpatialReference("EPSG:3857").use { t ->
            s.setAxisMappingStrategy(0);t.setAxisMappingStrategy(0)
            requireNotNull(runtime.createCoordinateTransformation(s,t)).use { ct ->
                val id=(ct as RuntimeHandle).runtimeId
                suspend fun reject(count:Int,bytes:ByteArray) {
                    val request="""{"op":"runtime","args":{"type":"CoordinateTransformation","handle":"$id","method":"transformPoints_Array_DoubleArray","parameters":{"pointCount":$count}}}"""
                    val response=transport.callBinary(request,bytes,null);assertNotNull(Json.parseToJsonElement(response.json).jsonObject["error"])
                    val valid=arrayOf(doubleArrayOf(10.0,45.0));ct.transformPoints(valid);assertTrue(valid[0][0]>1_000_000)
                }
                val valid=packRaster(doubleArrayOf(2.0,10.0,45.0,0.0,0.0))
                reject(-1,ByteArray(0));reject(Int.MAX_VALUE,ByteArray(0));reject(1,valid.copyOf(39));reject(1,valid.copyOf(41));reject(1,packRaster(doubleArrayOf(5.0,10.0,45.0,0.0,0.0)))
            }
        }}} finally {engine.close()}
    }
    @Test fun binaryEntryPointDoesNotDispatchUnrelatedOperations():Unit=runBlocking {
        val transport=createTransport(GdalOptions())
        try {assertFails {transport.callBinary("""{"op":"runtime","args":{"type":"Handle","handle":"1","method":"close","parameters":{}}}""",ByteArray(0),null)}} finally {transport.close()}
    }
}
