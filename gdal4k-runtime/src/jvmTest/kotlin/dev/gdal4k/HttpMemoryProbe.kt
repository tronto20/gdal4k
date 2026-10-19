package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.net.HttpURLConnection
import java.net.URI
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Fresh-process download/local-open control and future range comparison; no timing thresholds. */
object HttpMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]; val url = URI(args[1]); val fixture = Path.of(args[2])
        val side = args[3].toInt(); val tile = 256
        require(mode in listOf("download-local", "range") && side >= tile * 4 && side % tile == 0)
        // This diagnostic never downloads from a user or production endpoint.
        require(url.scheme == "http" && url.host == "127.0.0.1" && url.rawUserInfo == null)
        val transport = createTransport(GdalOptions(libraryPath = args[4]))
        val engine = RuntimeEngine(transport)
        val runtime = BundledGdalRuntime(engine)
        val directory = Files.createTempDirectory("gdal4k-http-probe-")
        try {
            suspend fun read(path: String, range: Boolean): Long {
                val options = DatasetOpenOptions(httpInputPolicy = if (range) HttpInputPolicy(requireStrongETag = true) else null)
                return runtime.openDataset(path, options).use { dataset ->
                    requireNotNull(dataset.getRasterBand(1)).use { band ->
                        check(band.getXSize() == side && band.getYSize() == side)
                        var checksum = 1125899906842597L
                        // Three non-overlapping tiles, including the last tile, on both comparison sides.
                        for ((x, y) in listOf(tile to tile, (side / 2) to (side / 2), (side - tile) to (side - tile))) {
                            val pixels = ByteArray(tile * tile)
                            check(band.readRaster(BandRasterIOOptions(x, y, tile, tile), pixels) == 0)
                            for (index in pixels.indices) {
                                val px = x + index % tile; val py = y + index / tile
                                val expected = (px * 31 + py * 17 + (px xor py)) and 255
                                check((pixels[index].toInt() and 255) == expected)
                                checksum = checksum * 31 + expected
                            }
                        }
                        checksum
                    }
                }
            }
            // Warm GDAL/JVM with the same local fixture. Network remains cold in each fresh process.
            val expected = read(fixture.toString(), false)
            System.gc()
            val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
            val heap = ManagementFactory.getMemoryMXBean()
            val sampling = AtomicBoolean(true); val peak = AtomicLong(heap.heapMemoryUsage.used)
            val sampler = Thread({ while (sampling.get()) {
                peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                LockSupport.parkNanos(2_000_000)
            } }, "http-heap-probe").apply { isDaemon = true }
            val before = mx.getTotalThreadAllocatedBytes()
            sampler.start()
            val start = System.nanoTime()
            var downloaded = 0L
            val result: Long
            val elapsed: Long
            try {
                result = if (mode == "download-local") {
                    val target = directory.resolve("downloaded.tif")
                    val connection = url.toURL().openConnection() as HttpURLConnection
                    try {
                        connection.connectTimeout = 30_000; connection.readTimeout = 30_000
                        connection.instanceFollowRedirects = false
                        connection.setRequestProperty("Accept-Encoding", "identity")
                        check(connection.responseCode == 200)
                        check(connection.contentLengthLong == Files.size(fixture))
                        check(connection.getHeaderField("Content-Encoding").let { it == null || it == "identity" })
                        connection.inputStream.use { input -> Files.newOutputStream(target).use { output ->
                            // A bounded copy buffer: the control does not stage a complete file in heap.
                            val buffer = ByteArray(64 * 1024)
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                output.write(buffer, 0, count); downloaded += count
                            }
                        } }
                        check(downloaded == Files.size(fixture))
                        read(target.toString(), false)
                    } finally { connection.disconnect(); Files.deleteIfExists(target) }
                } else read(url.toString(), true)
                elapsed = System.nanoTime() - start
            } finally { sampling.set(false); sampler.join() }
            val allocated = mx.getTotalThreadAllocatedBytes() - before
            check(result == expected)
            println("GDAL4K_HTTP_MEMORY {\"mode\":\"$mode\",\"side\":$side,\"tile\":$tile,\"windows\":3,\"validatedPixels\":${3 * tile * tile},\"downloadedBytes\":$downloaded,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"checksum\":\"$result\",\"javaVersion\":\"${System.getProperty("java.version")}\"}")
        } finally {
            try { engine.close() } finally {
                Files.list(directory).use { paths -> paths.forEach { Files.delete(it) } }
                Files.delete(directory)
            }
        }
    }
}
