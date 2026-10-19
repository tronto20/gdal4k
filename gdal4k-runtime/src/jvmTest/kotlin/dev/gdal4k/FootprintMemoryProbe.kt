package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.nio.file.Files
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Functional diagnostics on a deterministic valid-data ring, without speed thresholds. */
object FootprintMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]; val side = args[1].toInt()
        require(mode in listOf("memory", "direct") && side in 64..1024 && side % 4 == 0)
        val engine = RuntimeEngine(createTransport(GdalOptions(libraryPath = args[2])))
        val runtime = BundledGdalRuntime(engine)
        val directory = Files.createTempDirectory("gdal4k-footprint-")
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.create("", side, side, 1, GdalDataType.Byte)).use { source ->
                    val pixels = ByteArray(side * side) { index ->
                        val x = index % side; val y = index / side
                        if (x in side / 4 until 3 * side / 4 && y in side / 4 until 3 * side / 4) 0 else 1
                    }
                    check(source.writeRaster(pixels) == 0)
                    check(source.setGeoTransform(doubleArrayOf(100.0, 2.0, 0.5, 50.0, 0.25, -3.0)) == 0)
                    runtime.createSpatialReference("EPSG:3857").use { check(source.setSpatialRef(it) == 0) }
                    requireNotNull(source.getRasterBand(1)).use { check(it.setNoDataValue(0.0) == 0) }
                    val validPixels = side.toLong() * side * 3 / 4
                    val expectedArea = validPixels * 6.125 // independent affine determinant, not an envelope
                    var sequence = 0
                    var outputBytes = 0L
                    suspend fun inspect(output: Dataset): Long = output.use { ds ->
                        requireNotNull(ds.getLayer(0)).use { layer ->
                            check(layer.getFeatureCount(true) == 1L)
                            requireNotNull(layer.getNextFeature()).use { feature ->
                                check(feature.getFieldIndex("location") == -1)
                                requireNotNull(feature.getGeometryRef()).use { multi ->
                                    check(multi.getArea() == expectedArea)
                                    requireNotNull(multi.getGeometryRef(0)).use { check(it.getGeometryCount() == 2) }
                                }
                            }
                        }
                        validPixels
                    }
                    suspend fun operation(): Long {
                        if (mode == "memory") {
                            val files = source.footprint()
                            outputBytes = files.files.sumOf { it.bytes.size.toLong() }
                            return inspect(runtime.openDataset(files))
                        }
                        val path = directory.resolve("result-${sequence++}.gpkg")
                        try {
                            source.footprintTo(path.toString())
                            outputBytes = Files.size(path)
                            return inspect(runtime.openDataset(path.toString()))
                        } finally {
                            requireNotNull(runtime.getDriver("GPKG")).use { it.delete(path.toString()) }
                        }
                    }
                    check(operation() == validPixels)
                    System.gc()
                    val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
                    val heap = ManagementFactory.getMemoryMXBean()
                    val sampling = AtomicBoolean(true); val peak = AtomicLong(heap.heapMemoryUsage.used)
                    val sampler = Thread({ while (sampling.get()) {
                        peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                        LockSupport.parkNanos(2_000_000)
                    } }, "footprint-heap-probe").apply { isDaemon = true }
                    val before = mx.getTotalThreadAllocatedBytes(); sampler.start()
                    val begin = System.nanoTime(); var checksum = 0L
                    val elapsed = try { repeat(3) { checksum += operation() }; System.nanoTime() - begin }
                        finally { sampling.set(false); sampler.join() }
                    val allocated = mx.getTotalThreadAllocatedBytes() - before
                    check(checksum == validPixels * 3 && outputBytes > 0)
                    println("GDAL4K_FOOTPRINT_MEMORY {\"mode\":\"$mode\",\"side\":$side,\"rounds\":3,\"validPixels\":$validPixels,\"area\":$expectedArea,\"rings\":2,\"checksum\":\"$checksum\",\"outputBytes\":$outputBytes,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
                }
            }
        } finally {
            try { engine.close() } finally {
                // Only this probe's own isolated output directory, including failed partial outputs.
                Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) } }
            }
        }
    }
}
