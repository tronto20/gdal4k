package dev.gdal4k

import dev.gdal4k.internal.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class CoordinateArraysControlTest {
    @Test fun versionOneRasterTransportKeepsJsonCoordinateFallback()=runTest {
        var binary=false;var json=false
        val transport=object:GdalTransport {
            override val supportsBinaryRaster=true
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {json=true;return """{"result":{"value":[[3.0,4.0]]}}"""}
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {binary=true;error("Unexpected binary coordinate operation")}
            override suspend fun close() {}
        }
        val engine=RuntimeEngine(transport);val point=doubleArrayOf(1.0,2.0);BundledCoordinateTransformation(engine,"1",null).transformPoints(arrayOf(point))
        assertContentEquals(doubleArrayOf(3.0,4.0),point);assertTrue(json);assertFalse(binary);engine.close()
    }
    @Test fun malformedBinaryResponseCannotPartiallyChangeDestination()=runTest {
        for(bad in listOf("truncated","count","dimensions")) {
            val transport=object:GdalTransport {
                override val supportsBinaryCoordinates=true
                override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String=error("Unexpected JSON path")
                override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {
                    val data=doubleArrayOf(if(bad=="dimensions")3.0 else 2.0,111.0,222.0,0.0,0.0)
                    val bytes=packRaster(data)
                    return GdalBinaryReply("""{"result":{"value":${if(bad=="count")2 else 1}}}""",if(bad=="truncated")bytes.copyOf(39) else bytes)
                }
                override suspend fun close() {}
            }
            val engine=RuntimeEngine(transport);val point=doubleArrayOf(1.0,2.0)
            assertFails {BundledCoordinateTransformation(engine,"1",null).transformPoints(arrayOf(point))};assertContentEquals(doubleArrayOf(1.0,2.0),point);engine.close()
        }
    }
    @Test fun queuedCancellationNeverTransfersCoordinates()=runTest {
        val entered=CompletableDeferred<Unit>();val release=CompletableDeferred<Unit>();var calls=0
        val transport=object:GdalTransport {
            override val supportsBinaryCoordinates=true
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {entered.complete(Unit);release.await();return """{"result":{"value":0}}"""}
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {calls++;error("Queued call should be cancelled")}
            override suspend fun close() {}
        }
        val engine=RuntimeEngine(transport);val first=launch {engine.invoke("Handle","1","wait",emptyMap())};entered.await()
        val point=doubleArrayOf(1.0,2.0);val queued=launch(start=CoroutineStart.UNDISPATCHED) {BundledCoordinateTransformation(engine,"2",null).transformPoints(arrayOf(point))}
        queued.cancelAndJoin();release.complete(Unit);first.join();assertEquals(0,calls);assertContentEquals(doubleArrayOf(1.0,2.0),point);engine.close()
    }
}
