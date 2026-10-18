package dev.gdal4k

import dev.gdal4k.internal.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.*
import kotlin.test.*

class BinaryRasterControlTest {
    @Test fun rawCodecPreservesEveryTypeWithoutPartialDecode() = runTest {
        val values=longArrayOf(Long.MIN_VALUE,Long.MAX_VALUE,9007199254740993L)
        val packed=packRaster(values); val actual=LongArray(3); unpackRaster(packed,actual); assertContentEquals(values,actual)
        val target=DoubleArray(65536) {17.0}; val bytes=ByteArray(target.size*8)
        val job=launch(start=CoroutineStart.UNDISPATCHED) {unpackRaster(bytes,target)}
        yield() // A platform bulk commit can finish atomically while scalar decoding still yields.
        val finished=job.isCompleted
        job.cancelAndJoin()
        assertTrue(target.all {it==if(finished)0.0 else 17.0})
        assertFails {unpackRaster(ByteArray(7),LongArray(1))}
        assertFails {unpackRaster(ByteArray(16),LongArray(1))}
    }
    @Test fun binaryPayloadNeverBuildsJsonPerSample() = runTest {
        var calls=0
        val transport=object:GdalTransport {
            override val supportsBinaryRaster=true
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String = error("Unexpected JSON raster call")
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {
                calls++;assertTrue(request.length<512);assertFalse("\"buffer\":" in request)
                assertEquals(512*1024,requireNotNull(input).size)
                return GdalBinaryReply("{\"result\":{\"value\":{\"code\":0}}}",ByteArray(0))
            }
            override suspend fun close() {}
        }
        val dataset=BundledDataset(RuntimeEngine(transport),"1",null)
        dataset.writeRaster(DoubleArray(65536));assertEquals(1,calls)
    }
    @Test fun legacyTransportStillReceivesJsonArray() = runTest {
        var calls=0
        val transport=object:GdalTransport {
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {
                calls++;val args=Json.parseToJsonElement(request).jsonObject.getValue("args").jsonObject.getValue("parameters").jsonObject
                assertEquals(3,args.getValue("buffer").jsonArray.size)
                return "{\"result\":{\"value\":{\"code\":0}}}"
            }
            override suspend fun close() {}
        }
        BundledDataset(RuntimeEngine(transport),"1",null).writeRaster(byteArrayOf(-1,0,1));assertEquals(1,calls)
    }
    @Test fun blockCallsCarryCancellationWithoutAnObserver() = runTest {
        val transport=object:GdalTransport {
            override val supportsBinaryRaster=true
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String=error("Unexpected JSON block call")
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {
                assertNotNull(control);assertNull(input)
                return GdalBinaryReply("{\"result\":{\"value\":{\"code\":0}}}",byteArrayOf(1,2,3))
            }
            override suspend fun close() {}
        }
        val out=ByteArray(3)
        BundledBand(RuntimeEngine(transport),"1",null).readBlock(0,0,out)
        assertContentEquals(byteArrayOf(1,2,3),out)
    }

    @Test fun queuedCancellationDoesNotPrepareOrRetainAStagingBuffer() = runTest {
        val entered=CompletableDeferred<Unit>();val release=CompletableDeferred<Unit>();var prepared=0
        val transport=object:GdalTransport {
            override val supportsBinaryRaster=true
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {
                entered.complete(Unit);release.await();return "{\"result\":{\"value\":1}}"
            }
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply=error("Cancelled queued call was dispatched")
            override suspend fun close() {}
        }
        val engine=RuntimeEngine(transport)
        val first=async {engine.invoke("Dataset","1","getRasterXSize",emptyMap())}
        entered.await()
        val queued=launch(start=CoroutineStart.UNDISPATCHED) {
            engine.invoke("Dataset","1","writeRaster_ByteArray_RasterIOOptions",emptyMap(),binary=GdalBinaryCall(
                prepare={prepared++;ByteArray(1024*1024)},complete={_,_->}))
        }
        queued.cancelAndJoin();assertEquals(0,prepared)
        release.complete(Unit);assertEquals(1,first.await().jsonPrimitive.int)
    }

}
