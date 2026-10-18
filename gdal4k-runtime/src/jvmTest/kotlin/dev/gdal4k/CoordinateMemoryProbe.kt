package dev.gdal4k

import dev.gdal4k.runtime.*
import dev.gdal4k.internal.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport
import kotlin.math.abs

/** Fresh-JVM diagnostic; use the exact same compiled probe for baseline and changed engines. */
object CoordinateMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode=args[0];val count=args[1].toInt();val dimensions=args[2].toInt()
        require(mode in listOf("before","json-batch","binary-batch"));require(dimensions==2 || dimensions==4)
        val transport=createTransport(GdalOptions(libraryPath=args[3]))
        var jsonCalls=0;var binaryCalls=0;var bridgeBytes=0L
        val engine=RuntimeEngine(object:GdalTransport by transport {
            override val supportsBinaryCoordinates=mode=="binary-batch" && transport.supportsBinaryCoordinates
            override suspend fun call(request:String,attachment:Any?,control:GdalCallControl?):String {
                val reply=transport.call(request,attachment,control);jsonCalls++;bridgeBytes+=request.length+reply.length;return reply
            }
            override suspend fun callBinary(request:String,input:ByteArray?,control:GdalCallControl?):GdalBinaryReply {
                val reply=transport.callBinary(request,input,control);binaryCalls++;bridgeBytes+=request.length+reply.json.length+(input?.size?:0)+reply.bytes.size;return reply
            }
        })
        val runtime=BundledGdalRuntime(engine)
        try {
            runtime.createSpatialReference("EPSG:4326").use { source ->
                runtime.createSpatialReference("EPSG:3857").use { target ->
                    source.setAxisMappingStrategy(0);target.setAxisMappingStrategy(0)
                    requireNotNull(runtime.createCoordinateTransformation(source,target)).use { transform ->
                        fun input(n:Int)=Array(n) { i ->
                            val lon=-100.0+(i%1000)*0.1;val lat=-40.0+(i%800)*0.1
                            if(dimensions==2)doubleArrayOf(lon,lat) else doubleArrayOf(lon,lat,i*0.25,2020.0)
                        }
                        transform.transformPoints(input(128));System.gc()
                        val mx=ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
                        val heap=ManagementFactory.getMemoryMXBean();val sampling=AtomicBoolean(true);val peak=AtomicLong(heap.heapMemoryUsage.used)
                        val sampler=Thread({while(sampling.get()) {peak.accumulateAndGet(heap.heapMemoryUsage.used,::maxOf);LockSupport.parkNanos(2_000_000)}},"coordinate-heap-probe").apply {isDaemon=true}
                        jsonCalls=0;binaryCalls=0;bridgeBytes=0
                        val before=mx.getTotalThreadAllocatedBytes();sampler.start();val start=System.nanoTime();var checksum=0.0
                        val elapsed=try { repeat(3) {
                            val points=input(count);transform.transformPoints(points)
                            for(i in points.indices) {
                                val lon=-100.0+(i%1000)*0.1;val lat=-40.0+(i%800)*0.1
                                val x=lon*20037508.342789244/180.0
                                val y=kotlin.math.ln(kotlin.math.tan((90.0+lat)*kotlin.math.PI/360.0))*6378137.0
                                check(abs(points[i][0]-x)<1e-6 && abs(points[i][1]-y)<1e-6)
                                if(dimensions==4)check(points[i][2]==i*0.25 && points[i][3]==2020.0)
                                checksum+=points[i][0]+points[i][1]
                            }
                        };System.nanoTime()-start } finally {sampling.set(false);sampler.join()}
                        val allocated=mx.getTotalThreadAllocatedBytes()-before
                        println("GDAL4K_COORDINATE_MEMORY {\"mode\":\"$mode\",\"points\":$count,\"dimensions\":$dimensions,\"rounds\":3,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(),heap.heapMemoryUsage.used)},\"checksum\":$checksum,\"jsonCalls\":$jsonCalls,\"binaryCalls\":$binaryCalls,\"bridgeBytes\":$bridgeBytes,\"binaryCapability\":${transport.supportsBinaryCoordinates},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
                    }
                }
            }
        } finally {engine.close()}
    }
}
