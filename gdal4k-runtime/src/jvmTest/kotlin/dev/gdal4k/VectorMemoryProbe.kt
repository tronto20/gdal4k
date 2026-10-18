package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Fresh-JVM diagnostic of the existing feature batch API, including value/geometry access.
 * Fixture loading/copying and warmup are outside the measured region. No timing threshold.
 */
object VectorMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val driverName = args[0]
        val count = args[1].toInt()
        require(driverName in listOf("GeoJSON", "MEM", "GPKG") && count > 0)
        val transport = createTransport(GdalOptions(libraryPath = args[2]))
        var calls = 0L
        var bridgeBytes = 0L
        fun utf8Size(text: String): Long {
            var bytes = 0L
            var index = 0
            while (index < text.length) {
                val char = text[index++]
                bytes += when {
                    char.code < 128 -> 1
                    char.code < 2048 -> 2
                    char.isHighSurrogate() && index < text.length && text[index].isLowSurrogate() -> { index++; 4 }
                    else -> 3
                }
            }
            return bytes
        }
        val engine = RuntimeEngine(object : GdalTransport by transport {
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                val reply = transport.call(request, attachment, control)
                calls++
                bridgeBytes += utf8Size(request) + utf8Size(reply)
                return reply
            }
        })
        val runtime = BundledGdalRuntime(engine)
        val features = (0 until count).joinToString(",") { i ->
            val id = if (i % 7 == 0) "null" else (9007199254740993L + i).toString()
            val text = if (i % 5 == 0) "null" else "\"행-$i\""
            val geometry = if (i % 11 == 0) "null" else """{"type":"Point","coordinates":[$i,1]}"""
            """{"type":"Feature","properties":{"id64":$id,"value":${i * 0.5},"label":$text},"geometry":$geometry}"""
        }
        val input = GdalFiles(GdalFile("vector-probe.geojson", """{"type":"FeatureCollection","features":[$features]}""".encodeToByteArray()))
        val path = if (driverName == "GPKG") "/vsimem/vector-memory-probe.gpkg" else ""
        try {
            runtime.openDataset(input, DatasetOpenOptions(kind = DatasetKind.Vector)).use { source ->
                suspend fun measure(layer: Layer) {
                    suspend fun read(limit: Int): Long {
                        layer.resetReading()
                        var row = 0
                        var checksum = 0L
                        layer.forEachFeatureBatch(batchSize = 256, reset = false) { batch ->
                            for (feature in batch) {
                                if (row < limit) {
                                    val i = row
                                    check(feature.isFieldSetAndNotNull(0) == (i % 7 != 0))
                                    if (i % 7 != 0) {
                                        val value = feature.getFieldAsInteger64(0)
                                        check(value == 9007199254740993L + i)
                                        checksum += value xor i.toLong()
                                    }
                                    check(feature.getFieldAsDouble(1) == i * 0.5)
                                    check(feature.isFieldSetAndNotNull(2) == (i % 5 != 0))
                                    if (i % 5 != 0) check(feature.getFieldAsString(2) == "행-$i")
                                    val geometry = feature.getGeometryRef()
                                    check((geometry != null) == (i % 11 != 0))
                                    geometry?.use {
                                        val bytes = it.exportToWkb()
                                        check(bytes.size == 21)
                                        val buffer = ByteBuffer.wrap(bytes).order(if (bytes[0] == 1.toByte()) ByteOrder.LITTLE_ENDIAN else ByteOrder.BIG_ENDIAN)
                                        check(buffer.getInt(1) == 1 && buffer.getDouble(5) == i.toDouble() && buffer.getDouble(13) == 1.0)
                                        checksum += bytes.sumOf { byte -> byte.toLong() and 255 }
                                    }
                                }
                                row++
                            }
                        }
                        check(row == count)
                        return checksum
                    }
                    read(minOf(128, count))
                    System.gc()
                    val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
                    val heap = ManagementFactory.getMemoryMXBean()
                    val sampling = AtomicBoolean(true)
                    val peak = AtomicLong(heap.heapMemoryUsage.used)
                    val sampler = Thread({
                        while (sampling.get()) {
                            peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                            LockSupport.parkNanos(2_000_000)
                        }
                    }, "vector-heap-probe").apply { isDaemon = true }
                    calls = 0; bridgeBytes = 0
                    val before = mx.getTotalThreadAllocatedBytes()
                    sampler.start()
                    val start = System.nanoTime()
                    var checksum = 0L
                    val elapsed = try {
                        repeat(3) { checksum += read(count) }
                        System.nanoTime() - start
                    } finally { sampling.set(false); sampler.join() }
                    val allocated = mx.getTotalThreadAllocatedBytes() - before
                    println("GDAL4K_VECTOR_MEMORY {\"mode\":\"features\",\"driver\":\"$driverName\",\"rows\":$count,\"rounds\":3,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"checksum\":\"$checksum\",\"jsonCalls\":$calls,\"bridgeBytes\":$bridgeBytes,\"javaVersion\":\"${System.getProperty("java.version")}\"}")
                }
                if (driverName == "GeoJSON") requireNotNull(source.getLayer(0)).use { measure(it) }
                else requireNotNull(runtime.getDriver(driverName)).use { driver ->
                    try {
                        requireNotNull(driver.createVector(path)).use { destination ->
                            requireNotNull(source.getLayer(0)).use { inputLayer ->
                                requireNotNull(destination.copyLayer(inputLayer, "records")).use { layer ->
                                    check(destination.flushCache() == 0)
                                    measure(layer)
                                }
                            }
                        }
                    } finally { if (path.isNotEmpty()) check(driver.delete(path) == 0) }
                }
            }
        } finally { engine.close() }
    }
}
