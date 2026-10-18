package dev.gdal4k

import dev.gdal4k.runtime.*
import dev.gdal4k.RasterWindow
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class RasterWindowsApiTest {
    private suspend fun <T> memory(type: Int = GdalDataType.Float64, body: suspend (Dataset, Band) -> T): T =
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 7, 5, 1, type)).use { ds ->
                requireNotNull(ds.getRasterBand(1)).use { band -> body(ds, band) }
            }
        }

    @Test fun clippedWindowsCoverSubregionInOrderAndReuseTheCallerBuffer() = runTest(timeout=120.seconds) {
        memory { ds, band ->
            ds.writeRaster(DoubleArray(35) { it.toDouble() })
            val buffer=DoubleArray(10) { -99.0 }; val seen=mutableListOf<RasterWindow>(); val values=mutableListOf<Double>()
            band.forEachRasterWindow(buffer, 3, 2, RasterWindow(1, 1, 5, 3)) { window, data ->
                assertSame(buffer, data); seen += window
                val count=window.width*window.height
                for (y in 0 until window.height) for (x in 0 until window.width) {
                    val index=y*window.width+x
                    assertEquals(((window.y+y)*7+window.x+x).toDouble(), data[index]); values += data[index]
                }
                assertTrue(data.drop(count).all { it == -99.0 })
                data.fill(-99.0) // Callback may reuse/modify this storage after consuming a window.
                yield()
            }
            assertEquals(listOf(RasterWindow(1,1,3,2),RasterWindow(4,1,2,2),RasterWindow(1,3,3,1),RasterWindow(4,3,2,1)),seen)
            assertEquals(15, values.size); assertEquals(15, values.distinct().size)
        }
    }
    @Test fun allPrimitiveTypesPreserveTheirRawValuesAndInt64Precision() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Byte) { ds, b ->
            val input=ByteArray(35) { (it*7).toByte() }; ds.writeRaster(input)
            b.forEachRasterWindow(ByteArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x],a[y*w.width+x]) }
        }
        memory(GdalDataType.Int16) { ds, b ->
            val input=ShortArray(35) { (it*1800-32000).toShort() }; ds.writeRaster(input)
            b.forEachRasterWindow(ShortArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x],a[y*w.width+x]) }
        }
        memory(GdalDataType.Int32) { ds, b ->
            val input=IntArray(35) { Int.MIN_VALUE+it }; ds.writeRaster(input)
            b.forEachRasterWindow(IntArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x],a[y*w.width+x]) }
        }
        memory(GdalDataType.Int64) { ds, b ->
            val input=LongArray(35) { if(it%2==0)Long.MIN_VALUE+it else 9007199254740993L+it }; ds.writeRaster(input)
            b.forEachRasterWindow(LongArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x],a[y*w.width+x]) }
        }
        memory(GdalDataType.Float32) { ds, b ->
            val input=FloatArray(35) { if(it%2==0) -0.0f else it*0.125f }; ds.writeRaster(input)
            b.forEachRasterWindow(FloatArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x].toRawBits(),a[y*w.width+x].toRawBits()) }
        }
        memory { ds, b ->
            val input=DoubleArray(35) { if(it%2==0) -0.0 else it*0.125 }; ds.writeRaster(input)
            b.forEachRasterWindow(DoubleArray(6),3,2) { w,a -> for(y in 0 until w.height)for(x in 0 until w.width)assertEquals(input[(w.y+y)*7+w.x+x].toRawBits(),a[y*w.width+x].toRawBits()) }
        }
    }
    @Test fun invalidShapeCapacityAndBoundsDoNotInvokeCallback() = runTest(timeout=120.seconds) {
        memory { _,band ->
            var called=false
            assertFailsWith<IllegalArgumentException> { band.forEachRasterWindow(DoubleArray(6),0,2) { _,_->called=true } }
            assertFailsWith<IllegalArgumentException> { band.forEachRasterWindow(DoubleArray(5),3,2) { _,_->called=true } }
            assertFailsWith<IllegalArgumentException> { band.forEachRasterWindow(DoubleArray(6),3,2,RasterWindow(6,0,2,1)) { _,_->called=true } }
            assertFailsWith<IllegalArgumentException> { band.forEachRasterWindow(DoubleArray(6),Int.MAX_VALUE,Int.MAX_VALUE) { _,_->called=true } }
            assertFalse(called)
            var count=0
            band.forEachRasterWindow(DoubleArray(35),Int.MAX_VALUE,Int.MAX_VALUE) { _,_->count++ }
            assertEquals(1,count)
        }
    }
    @Test fun rawNoDataScaleOffsetAndMetadataArePreserved() = runTest(timeout=120.seconds) {
        memory { ds, b ->
            ds.writeRaster(DoubleArray(35) { if(it==9) -9999.0 else it.toDouble() }); b.setNoDataValue(-9999.0); b.setScale(2.0); b.setOffset(10.0)
            var noDataCount=0
            b.forEachRasterWindow(DoubleArray(6),3,2) { w,a -> for(i in 0 until w.width*w.height)if(a[i]==-9999.0)noDataCount++ }
            assertEquals(1,noDataCount); assertEquals(-9999.0,b.getNoDataValue()); assertEquals(2.0,b.getScale()); assertEquals(10.0,b.getOffset())
        }
    }
    @Test fun callbackFailureAndCancellationLeaveCallerHandlesOpen() = runTest(timeout=120.seconds) {
        memory { ds, b ->
            val expected=IllegalStateException("consumer stopped")
            assertSame(expected,assertFailsWith<IllegalStateException> { b.forEachRasterWindow(DoubleArray(6),3,2) { _,_->throw expected } })
            val entered=CompletableDeferred<Unit>(); var calls=0
            val job=launch { b.forEachRasterWindow(DoubleArray(6),3,2) { _,_->calls++;entered.complete(Unit);awaitCancellation() } }
            entered.await(); job.cancelAndJoin(); assertEquals(1,calls)
            assertEquals(7,b.getXSize());assertEquals(5,ds.getRasterYSize())
        }
    }
    @Test fun cancellationBetweenWindowsPreventsAnotherCallbackAndClosedBandFails() = runTest(timeout=120.seconds) {
        memory { _,b ->
            var calls=0
            val job=launch { b.forEachRasterWindow(DoubleArray(6),3,2) { _,_->calls++;currentCoroutineContext().cancel() } };job.join();assertEquals(1,calls)
            b.close();assertFails { b.forEachRasterWindow(DoubleArray(6),3,2) { _,_->calls++ } };assertEquals(1,calls)
        }
    }
}
