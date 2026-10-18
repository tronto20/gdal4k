package dev.gdal4k

import dev.gdal4k.runtime.*
import dev.gdal4k.internal.*
import kotlinx.coroutines.*
import kotlin.test.*
import java.lang.management.ManagementFactory

/** Same engine/JVM/data; capability masking is confined to this A/B benchmark. No timing threshold. */
class RasterTransportBenchmark {
    @Test fun doubleRasterRoundTrip() = runBlocking {
        runCase("Float64")
        runCase("Byte")
    }
    private suspend fun runCase(kind: String) {
        val management = ManagementFactory.getThreadMXBean() as? com.sun.management.ThreadMXBean
        val size = 1024 * 1024
        val input: Any = if (kind=="Float64") DoubleArray(size) { (it % 10007 - 5000) * 0.125 } else ByteArray(size) {(it%251).toByte()}
        val output: Any = if (kind=="Float64") DoubleArray(size) else ByteArray(size)
        val sampleBytes=if(kind=="Float64")8 else 1
        val transports = listOf(createTransport(GdalOptions()), createTransport(GdalOptions()))
        assertTrue(transports.all {it.supportsBinaryRaster})
        val wire=LongArray(2)
        val engines = listOf(RuntimeEngine(object:GdalTransport by transports[0] {
            override val supportsBinaryRaster=false
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {
                val response=transports[0].call(request,attachment,control)
                wire[0]+=request.length+response.length
                return response
            }
        }),RuntimeEngine(object:GdalTransport by transports[1] {
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {
                val response=transports[1].callBinary(request,input,control)
                wire[1]+=request.length+response.json.length+(input?.size?:0)+response.bytes.size
                return response
            }
        }))
        val datasets = engines.map { engine ->
            requireNotNull(BundledGdalRuntime(engine).getDriver("MEM")).use { driver ->
                requireNotNull(driver.create("",1024,1024,1,if(kind=="Float64")GdalDataType.Float64 else GdalDataType.Byte))
            }
        }
        suspend fun roundtrip(dataset: Dataset) {
            if(input is DoubleArray) {assertEquals(0,dataset.writeRaster(input));assertEquals(0,dataset.readRaster(output as DoubleArray))}
            else {assertEquals(0,dataset.writeRaster(input as ByteArray));assertEquals(0,dataset.readRaster(output as ByteArray))}
        }
        try {
            for (dataset in datasets) roundtrip(dataset)
            for (trial in 0..2) for (index in if(trial%2==0) listOf(0,1) else listOf(1,0)) {
                System.gc()
                wire[index]=0
                val allocated = management?.getTotalThreadAllocatedBytes() ?: -1L
                val start = System.nanoTime()
                repeat(3) {
                    roundtrip(datasets[index])
                }
                val elapsed = System.nanoTime() - start
                val bytes = management?.getTotalThreadAllocatedBytes()?.minus(allocated) ?: -1L
                if(input is DoubleArray)assertContentEquals(input,output as DoubleArray) else assertContentEquals(input as ByteArray,output as ByteArray)
                val protocol=if(index==0) "json" else "binary"
                println("GDAL4K_RASTER_TRANSPORT_BENCHMARK {\"kind\":\"$kind\",\"protocol\":\"$protocol\",\"trial\":$trial,\"samples\":$size,\"sampleBytes\":$sampleBytes,\"roundTrips\":3,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$bytes,\"wireBytes\":${wire[index]},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
            }
        } finally {
            datasets.forEach {it.close()}
            engines.forEach {it.close()}
        }
    }
    @Test fun cancellationLatency() = runBlocking {
        for (binary in listOf(false,true)) {
            val transport=createTransport(GdalOptions())
            val engine=RuntimeEngine(object:GdalTransport by transport {override val supportsBinaryRaster=binary})
            try {
                requireNotNull(BundledGdalRuntime(engine).getDriver("MEM")).use { driver ->
                    requireNotNull(driver.create("",1024,1024,1,GdalDataType.Float64)).use { dataset ->
                        val input=DoubleArray(1024*1024) {42.0}
                        var start=System.nanoTime()
                        val packing=launch(start=CoroutineStart.UNDISPATCHED) {dataset.writeRaster(input)}
                        val callerBlockedNanos=System.nanoTime()-start
                        yield()
                        start=System.nanoTime();packing.cancelAndJoin()
                        val packingNanos=System.nanoTime()-start
                        val output=ByteArray(4096*4096) {17}
                        var requested=0L;var fraction=-1.0
                        start=System.nanoTime()
                        assertFailsWith<CancellationException> {
                            withGdalProgress({event -> requested=System.nanoTime();fraction=event.fraction;false}) {
                                dataset.readRaster(output,RasterIOOptions(bufferWidth=4096,bufferHeight=4096,resampling=RasterResampling.Cubic))
                            }
                        }
                        val end=System.nanoTime()
                        assertTrue(requested>0);assertTrue(output.all {it==17.toByte()})
                        val protocol=if(binary) "binary" else "json"
                        println("GDAL4K_RASTER_CANCEL_BENCHMARK {\"protocol\":\"$protocol\",\"packingCallerBlockedNanos\":$callerBlockedNanos,\"packingCancelToCompletionNanos\":$packingNanos,\"nativeReadTotalNanos\":${end-start},\"nativeCancelToCompletionNanos\":${end-requested},\"fractionAtCancel\":$fraction,\"outputBytes\":${output.size},\"destinationUnchanged\":true}")
                    }
                }
            } finally {engine.close()}
        }
    }
}
