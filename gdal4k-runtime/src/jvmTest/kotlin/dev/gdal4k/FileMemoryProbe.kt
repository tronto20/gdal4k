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

/** Instrumented existing file APIs. Each process runs one case; no timing threshold. */
object FileMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]
        val side = args[1].toInt()
        require(mode in listOf("memory", "bundle", "direct") && side in 32..4096)
        val transport = createTransport(GdalOptions(libraryPath = args[2]))
        var jsonCalls = 0L
        var binaryCalls = 0L
        var bridgeBytes = 0L
        var largestJsonBytes = 0L
        fun utf8Bytes(text: String): Long {
            var size = 0L
            var index = 0
            while (index < text.length) {
                val char = text[index++]
                size += when {
                    char.code < 128 -> 1
                    char.code < 2048 -> 2
                    char.isHighSurrogate() && index < text.length && text[index].isLowSurrogate() -> { index++; 4 }
                    else -> 3
                }
            }
            return size
        }
        fun countJson(request: String, response: String) {
            val a = utf8Bytes(request); val b = utf8Bytes(response)
            largestJsonBytes = maxOf(largestJsonBytes, a, b)
            bridgeBytes += a + b
        }
        val engine = RuntimeEngine(object : GdalTransport by transport {
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                val reply = transport.call(request, attachment, control)
                jsonCalls++; countJson(request, reply)
                return reply
            }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply {
                val reply = transport.callBinary(request, input, control)
                binaryCalls++; countJson(request, reply.json)
                bridgeBytes += (input?.size ?: 0) + reply.bytes.size
                return reply
            }
        })
        val runtime = BundledGdalRuntime(engine)
        val pixels = ByteArray(side * side) { ((it * 31 + it / 251) and 255).toByte() }
        fun checksum(bytes: ByteArray): Long {
            var value = 1125899906842597L
            for (byte in bytes) value = value * 31 + (byte.toInt() and 255)
            return value
        }
        val directory = Files.createTempDirectory("gdal4k-file-probe-")
        val memoryPath = "/vsimem/file-memory-probe.bin"
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.create("", side, side, 1, 1)).use { source ->
                    requireNotNull(source.getRasterBand(1)).use { band ->
                        check(band.writeRaster(pixels) == 0)
                    }
                    val fixture = source.translate("GTiff", "fixture.tif", emptyList())
                    val fixtureBytes = fixture.mainFile().bytes
                    val expected = checksum(if (mode == "memory") pixels else fixtureBytes)
                    runtime.openDataset(fixture).use { input ->
                        suspend fun operation(round: Int): Long = when (mode) {
                            "memory" -> {
                                runtime.writeMemoryFile(memoryPath, pixels)
                                val output = runtime.readMemoryFile(memoryPath)
                                check(output.contentEquals(pixels))
                                checksum(output)
                            }
                            "bundle" -> runtime.openDataset(fixture).use { dataset ->
                                val output = dataset.translate("GTiff", "copied.tif", emptyList())
                                check(output.files.size == 1 && output.mainFile().bytes.contentEquals(fixtureBytes))
                                checksum(output.mainFile().bytes)
                            }
                            else -> {
                                // Existing direct-to-path control. OS readback is included so all
                                // cases return and validate a complete caller-owned byte buffer.
                                val path = directory.resolve("direct-$round.tif")
                                input.translateTo(path.toString(), "GTiff", emptyList())
                                val output = Files.readAllBytes(path)
                                check(output.contentEquals(fixtureBytes))
                                checksum(output)
                            }
                        }
                        check(operation(-1) == expected)
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
                        }, "file-heap-probe").apply { isDaemon = true }
                        jsonCalls = 0; binaryCalls = 0; bridgeBytes = 0; largestJsonBytes = 0
                        val before = mx.getTotalThreadAllocatedBytes()
                        sampler.start()
                        val start = System.nanoTime()
                        var result = 0L
                        val elapsed = try {
                            repeat(3) { round -> result += operation(round) }
                            System.nanoTime() - start
                        } finally { sampling.set(false); sampler.join() }
                        val allocated = mx.getTotalThreadAllocatedBytes() - before
                        check(result == expected * 3)
                        val count = if (mode == "memory") pixels.size else fixtureBytes.size
                        println("GDAL4K_FILE_MEMORY {\"mode\":\"$mode\",\"side\":$side,\"payloadBytes\":$count,\"rounds\":3,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"checksum\":\"$result\",\"jsonCalls\":$jsonCalls,\"binaryCalls\":$binaryCalls,\"bridgeBytes\":$bridgeBytes,\"largestJsonBytes\":$largestJsonBytes,\"javaVersion\":\"${System.getProperty("java.version")}\"}")
                    }
                }
            }
        } finally {
            try { runtime.deleteMemoryFile(memoryPath) } finally { engine.close() }
            Files.list(directory).use { paths -> paths.forEach { Files.delete(it) } }
            Files.delete(directory)
        }
    }
}
