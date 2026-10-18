package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.nio.file.Files
import java.nio.file.Path
import kotlin.math.min
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Standalone fresh-JVM probe. The Python harness records OS peak RSS for this process only. */
object RasterWindowMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode=args[0];val path=args[1];val width=2048;val height=2048;val tile=256;val rounds=3
        try {
            val runtime=Gdal4k.runtime()
            if(mode=="fixture") {
                check(!Files.exists(Path.of(path))) { "Benchmark refuses an existing fixture" }
                requireNotNull(runtime.getDriver("GTiff")).use { driver ->
                    requireNotNull(driver.create(path,width,height,1,GdalDataType.Float64,listOf("TILED=YES","BLOCKXSIZE=256","BLOCKYSIZE=256","COMPRESS=NONE"))).use { ds ->
                        requireNotNull(ds.getRasterBand(1)).use { check(it.fill(1.25)==0) }
                    }
                }
                println("GDAL4K_WINDOW_FIXTURE_READY");return@runBlocking
            }
            require(mode in listOf("full","allocate","reuse"))
            requireNotNull(runtime.openDataset(path)).use { ds -> requireNotNull(ds.getRasterBand(1)).use { band ->
                check(band.getXSize()==width && band.getYSize()==height)
                // Identical small warm-up; it does not preallocate a full-raster array.
                val warmup=DoubleArray(tile*tile);check(band.readRaster(BandRasterIOOptions(0,0,tile,tile),warmup)==0)
                check(warmup.all { it==1.25 });System.gc()
                val management=ManagementFactory.getThreadMXBean() as? com.sun.management.ThreadMXBean
                val heap=ManagementFactory.getMemoryMXBean()
                val heapBefore=heap.heapMemoryUsage.used
                val sampledHeap=AtomicLong(heapBefore);val sampling=AtomicBoolean(true)
                val sampler=Thread({
                    while(sampling.get()) {
                        val used=heap.heapMemoryUsage.used
                        var previous=sampledHeap.get()
                        while(used>previous && !sampledHeap.compareAndSet(previous,used)) previous=sampledHeap.get()
                        LockSupport.parkNanos(2_000_000)
                    }
                }, "window-heap-probe").apply { isDaemon=true }
                val before=management?.getTotalThreadAllocatedBytes() ?: -1L
                sampler.start()
                val start=System.nanoTime();var checksum=0.0;var samples=0L;var calls=0
                fun consume(data: DoubleArray,count: Int) { for(i in 0 until count)checksum+=data[i];samples+=count;calls++ }
                val elapsed=try { when(mode) {
                    "full" -> repeat(rounds) {
                        val buffer=DoubleArray(width*height);check(band.readRaster(buffer)==0);consume(buffer,buffer.size)
                    }
                    "allocate" -> repeat(rounds) {
                        var y=0
                        while(y<height) { val rows=min(tile,height-y);var x=0
                            while(x<width) { val columns=min(tile,width-x);val buffer=DoubleArray(columns*rows)
                                check(band.readRaster(BandRasterIOOptions(x,y,columns,rows),buffer)==0);consume(buffer,buffer.size);x+=columns }
                            y+=rows
                        }
                    }
                    "reuse" -> {
                        val buffer=DoubleArray(tile*tile)
                        repeat(rounds) { band.forEachRasterWindow(buffer,tile,tile) { window,data -> consume(data,window.width*window.height) } }
                    }
                }
                    System.nanoTime()-start
                } finally { sampling.set(false);sampler.join() }
                val heapAfter=heap.heapMemoryUsage.used
                val heapPeak=maxOf(sampledHeap.get(),heapAfter)
                val allocation=management?.getTotalThreadAllocatedBytes()?.let { if(before<0) -1L else it-before } ?: -1L
                check(samples==width.toLong()*height*rounds && checksum==samples*1.25)
                check(calls==if(mode=="full")rounds else rounds*(width/tile)*(height/tile))
                println("GDAL4K_WINDOW_MEMORY {\"mode\":\"$mode\",\"width\":$width,\"height\":$height,\"windowWidth\":$tile,\"windowHeight\":$tile,\"rounds\":$rounds,\"samples\":$samples,\"checksum\":$checksum,\"readCalls\":$calls,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocation,\"heapUsedBeforeBytes\":$heapBefore,\"heapUsedAfterBytes\":$heapAfter,\"sampledPeakHeapUsedBytes\":$heapPeak,\"heapSampleIntervalNanos\":2000000,\"javaVersion\":\"${System.getProperty("java.version")}\"}")
            } }
        } finally { Gdal4k.close() }
    }
}
