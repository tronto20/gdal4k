package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class BinaryRasterApiTest {
    private suspend fun <T> memory(type: Int, width: Int = 8, height: Int = 4, bands: Int = 1, body: suspend (Dataset) -> T): T =
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("",width,height,bands,type)).use { body(it) }
        }

    @Test fun integerBitsAndCallerBuffersSurviveRoundTrips() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Byte) { ds ->
            val input=ByteArray(32) { (it*31).toByte() }; val original=input.copyOf()
            assertEquals(0,ds.writeRaster(input)); assertContentEquals(original,input)
            val out=ByteArray(40) { 12 }; assertEquals(0,ds.readRaster(out))
            assertContentEquals(input,out.copyOf(32)); assertTrue(out.drop(32).all { it==12.toByte() })
        }
        memory(GdalDataType.Int16) { ds ->
            val input=ShortArray(32) { (it*2111-32000).toShort() }; ds.writeRaster(input)
            val out=ShortArray(32); ds.readRaster(out); assertContentEquals(input,out)
        }
        memory(GdalDataType.Int32) { ds ->
            val input=IntArray(32) { if(it%2==0) Int.MIN_VALUE+it else Int.MAX_VALUE-it }; ds.writeRaster(input)
            val out=IntArray(32); ds.readRaster(out); assertContentEquals(input,out)
        }
        memory(GdalDataType.Int64) { ds ->
            val input=LongArray(32) { when(it%3) {0->Long.MIN_VALUE+it;1->Long.MAX_VALUE-it;else->9007199254740993L+it} }
            ds.writeRaster(input); val out=LongArray(32); ds.readRaster(out); assertContentEquals(input,out)
        }
    }
    @Test fun floatingPointBitsIncludeNonFiniteAndSignedZero() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float32) { ds ->
            val bits=intArrayOf(0,Int.MIN_VALUE,0x7f800000,0xff800000.toInt(),0x7fc01234,0x00000001,0x3f800000,0xbf800000.toInt())
            val input=FloatArray(32) { Float.fromBits(bits[it%bits.size]) }; ds.writeRaster(input)
            val out=FloatArray(32); ds.readRaster(out)
            assertContentEquals(input.map { it.toRawBits() },out.map { it.toRawBits() })
        }
        memory(GdalDataType.Float64) { ds ->
            val bits=longArrayOf(0,Long.MIN_VALUE,0x7ff0000000000000L,-4503599627370496L,0x7ff8000000001234L,1,0x3ff0000000000000L,-4616189618054758400L)
            val input=DoubleArray(32) { Double.fromBits(bits[it%bits.size]) }; ds.writeRaster(input)
            val out=DoubleArray(32); ds.readRaster(out)
            assertContentEquals(input.map { it.toRawBits() },out.map { it.toRawBits() })
        }
    }
    @Test fun datasetBandsWindowsAndTrailingCapacity() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float64,512,256,2) { ds ->
            val input=DoubleArray(512*256*2) { (it%1009)*0.125 }; val original=input.copyOf()
            ds.writeRaster(input); assertContentEquals(original,input)
            val out=DoubleArray(12) {-99.0}
            ds.readRaster(out,RasterIOOptions(xOffset=3,yOffset=2,xSize=2,ySize=2,bands=intArrayOf(2,1)))
            val expected=doubleArrayOf(input[131072+1027],input[131072+1028],input[131072+1539],input[131072+1540],input[1027],input[1028],input[1539],input[1540])
            assertContentEquals(expected,out.copyOf(8)); assertTrue(out.drop(8).all {it == -99.0})
            requireNotNull(ds.getRasterBand(1)).use { band ->
                val b=DoubleArray(4); band.readRaster(BandRasterIOOptions(xOffset=3,yOffset=2,xSize=2,ySize=2),b)
                assertContentEquals(expected.copyOfRange(4,8),b)
            }
        }
    }
    @Test fun invalidReadLeavesDestinationAndRuntimeUsable() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float64) { ds ->
            val out=DoubleArray(32) {17.0}
            assertFails { ds.readRaster(out,RasterIOOptions(xOffset=8)) }
            assertTrue(out.all {it==17.0})
            assertFails { ds.readRaster(DoubleArray(1)) }
            assertEquals(0,ds.readRaster(out)); assertTrue(out.all {it==0.0})
        }
    }
    @Test fun packingCancellationDoesNotDispatchWriteOrDetachCaller() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float64,512,512) { ds ->
            val input=DoubleArray(512*512) {42.0}
            val job=launch(start=CoroutineStart.UNDISPATCHED) { ds.writeRaster(input) }
            yield() // Allow one conversion chunk to run before cancellation.
            job.cancelAndJoin(); assertTrue(input.all {it==42.0})
            val out=DoubleArray(input.size); assertEquals(0,ds.readRaster(out)); assertTrue(out.all {it==0.0})
        }
    }
    @Test fun nativeReadCancellationDoesNotCommitDestination() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float64,512,512) { ds ->
            val out=DoubleArray(512*512) {17.0}
            assertFailsWith<CancellationException> { withGdalProgress({false}) {ds.readRaster(out)} }
            assertTrue(out.all {it==17.0})
            assertEquals(0,ds.readRaster(out)); assertTrue(out.all {it==0.0})
        }
    }
    @Test fun rawBlockCancellationDoesNotCommitDestination() = runTest(timeout=120.seconds) {
        memory(GdalDataType.Float64) { ds ->
            requireNotNull(ds.getRasterBand(1)).use { band ->
                val out=ByteArray(8*8) {17}
                assertFailsWith<CancellationException> {withGdalProgress({false}) {band.readBlock(0,0,out)}}
                assertTrue(out.all {it==17.toByte()})
                assertEquals(0,band.readBlock(0,0,out));assertTrue(out.all {it==0.toByte()})
            }
        }
    }

}
