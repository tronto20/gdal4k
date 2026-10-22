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

/** Functional metadata-index diagnostics. This does not measure a pixel mosaic or GTI reader. */
object TileIndexMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]; val tiles = args[1].toInt(); val side = 64
        require(mode in listOf("memory", "direct") && tiles in 4..256)
        val engine = RuntimeEngine(createTransport(GdalOptions(libraryPath = args[2])))
        val runtime = BundledGdalRuntime(engine)
        val directory = Files.createTempDirectory("gdal4k-tile-index-")
        val prefix = "/vsimem/tile-index-probe"
        try {
            runtime.writeMemoryFile("$prefix/sentinel", byteArrayOf(1))
            val sources = (0 until tiles).map { "$prefix/tile-$it.tif" }
            requireNotNull(runtime.getDriver("GTiff")).use { driver ->
                runtime.createSpatialReference("EPSG:3857").use { crs ->
                    sources.forEachIndexed { i, path ->
                        requireNotNull(driver.create(path, side, side, 1, GdalDataType.Byte)).use { source ->
                            check(source.writeRaster(ByteArray(side * side) { (i + 1).toByte() }) == 0)
                            check(source.setGeoTransform(doubleArrayOf(100.0 + i * 200.0, 2.0, 0.5, 50.0, 0.25, -3.0)) == 0)
                            check(source.setSpatialRef(crs) == 0)
                        }
                    }
                }
            }
            val expectedArea = tiles * side.toDouble() * side * 6.125
            val expectedChecksum = tiles.toLong() * (tiles + 1) / 2 * side * side
            var sequence = 0; var outputBytes = 0L
            suspend fun inspect(output: Dataset): Long = output.use { ds ->
                requireNotNull(ds.getLayer(0)).use { layer ->
                    check(layer.getFeatureCount(true) == tiles.toLong())
                    var area = 0.0; var checksum = 0L
                    sources.forEachIndexed { i, path ->
                        requireNotNull(layer.getNextFeature()).use { feature ->
                            check(feature.getFieldAsString("location") == path)
                            requireNotNull(feature.getGeometryRef()).use { geometry ->
                                val a = geometry.getArea()
                                check(a == side * side * 6.125)
                                area += a; checksum += (i + 1).toLong() * side * side
                            }
                        }
                    }
                    check(layer.getNextFeature() == null && area == expectedArea && checksum == expectedChecksum)
                    checksum
                }
            }
            suspend fun operation(): Long {
                if (mode == "memory") {
                    val files = runtime.tileIndex(sources)
                    outputBytes = files.files.sumOf { it.bytes.size.toLong() }
                    return inspect(runtime.openDataset(files))
                }
                val path = directory.resolve("result-${sequence++}.gpkg")
                try {
                    runtime.tileIndexTo(sources, path.toString())
                    outputBytes = Files.size(path)
                    return inspect(runtime.openDataset(path.toString()))
                } finally { requireNotNull(runtime.getDriver("GPKG")).use { it.delete(path.toString()) } }
            }
            check(operation() == expectedChecksum)
            System.gc()
            val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
            val heap = ManagementFactory.getMemoryMXBean()
            val sampling = AtomicBoolean(true); val peak = AtomicLong(heap.heapMemoryUsage.used)
            val sampler = Thread({ while (sampling.get()) {
                peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                LockSupport.parkNanos(2_000_000)
            } }, "tile-index-heap-probe").apply { isDaemon = true }
            val before = mx.getTotalThreadAllocatedBytes(); sampler.start()
            val begin = System.nanoTime(); var checksum = 0L
            val elapsed = try { repeat(3) { checksum += operation() }; System.nanoTime() - begin }
                finally { sampling.set(false); sampler.join() }
            val allocated = mx.getTotalThreadAllocatedBytes() - before
            check(checksum == expectedChecksum * 3 && outputBytes > 0)
            println("GDAL4K_TILE_INDEX_MEMORY {\"mode\":\"$mode\",\"tiles\":$tiles,\"side\":$side,\"rounds\":3,\"area\":$expectedArea,\"checksum\":\"$checksum\",\"outputBytes\":$outputBytes,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
        } finally {
            try { runtime.deleteMemoryFile(prefix) } finally { try { engine.close() } finally {
                Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) } }
            } }
        }
    }
}
