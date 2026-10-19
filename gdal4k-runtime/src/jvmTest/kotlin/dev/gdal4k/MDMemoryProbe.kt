package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Existing MD APIs on a deterministic MEM driver fixture; diagnostic, without timing thresholds. */
object MDMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]; val side = args[1].toInt()
        require(mode in listOf("double-slice", "int64-values", "uint64-attribute") && side in 16..1024)
        val transport = createTransport(GdalOptions(libraryPath = args[2]))
        var jsonCalls = 0L; var binaryCalls = 0L; var bridgeBytes = 0L; var largestJsonBytes = 0L
        fun utf8Bytes(text: String): Long {
            var size = 0L; var index = 0
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
            bridgeBytes += a + b; largestJsonBytes = maxOf(largestJsonBytes, a, b)
        }
        val engine = RuntimeEngine(object : GdalTransport by transport {
            override suspend fun call(request: String, attachment: Any?, control: GdalCallControl?): String {
                val reply = transport.call(request, attachment, control)
                jsonCalls++; countJson(request, reply); return reply
            }
            override suspend fun callBinary(request: String, input: ByteArray?, control: GdalCallControl?): GdalBinaryReply {
                val reply = transport.callBinary(request, input, control)
                binaryCalls++; countJson(request, reply.json)
                bridgeBytes += (input?.size ?: 0) + reply.bytes.size
                return reply
            }
        })
        fun checksum(values: Iterable<String>): Long {
            var result = 1125899906842597L
            for (text in values) { for (char in text) result = result * 31 + char.code; result = result * 31 + 255 }
            return result
        }
        val runtime = BundledGdalRuntime(engine)
        try {
            requireNotNull(runtime.getDriver("MEM")).use { driver ->
                requireNotNull(driver.createMultidimensional("")).use { dataset ->
                    requireNotNull(dataset.getRootGroup()).use { group ->
                        // A nonzero, rectangular window exercises row-major dimension handling.
                        val dimensions = listOf(requireNotNull(group.createDimension("y", side + 7L)),
                            requireNotNull(group.createDimension("x", side + 11L)))
                        val start = longArrayOf(3, 5); val count = longArrayOf(side.toLong(), side.toLong())
                        val valuesCount = if (mode == "uint64-attribute") minOf(1024, side * side) else side * side
                        val doubles = DoubleArray(valuesCount) { (it - valuesCount / 2) * 0.125 }
                        val exact = List(valuesCount) { index -> MDValue.Numeric(when (mode) {
                            "uint64-attribute" -> (ULong.MAX_VALUE - index.toULong()).toString()
                            else -> (if (index % 2 == 0) Long.MAX_VALUE - index else Long.MIN_VALUE + index).toString()
                        }) }
                        val expected = checksum(if (mode == "double-slice") doubles.map { it.toRawBits().toString() } else exact.map { it.real })
                        val array = requireNotNull(group.createArray("samples", dimensions,
                            if (mode == "double-slice") GdalDataType.Float64 else GdalDataType.Int64))
                        array.use {
                            val attribute = if (mode == "uint64-attribute") requireNotNull(array.createAttribute(
                                "ids", longArrayOf(valuesCount.toLong()), MDDataType.Numeric(GdalDataType.UInt64))) else null
                            try {
                                suspend fun operation(): Long = when (mode) {
                                    "double-slice" -> {
                                        check(array.write(start, count, doubles))
                                        val result = array.read(start, count)
                                        check(result.size == doubles.size && result.indices.all { result[it].toRawBits() == doubles[it].toRawBits() })
                                        checksum(result.map { it.toRawBits().toString() })
                                    }
                                    "int64-values" -> {
                                        check(array.writeValues(start, count, exact)); val result = array.readValues(start, count)
                                        check(result == exact); checksum(result.map { (it as MDValue.Numeric).real })
                                    }
                                    else -> {
                                        val a = requireNotNull(attribute)
                                        check(a.writeValues(exact)); val result = a.readValues()
                                        check(result == exact); checksum(result.map { (it as MDValue.Numeric).real })
                                    }
                                }
                                check(operation() == expected)
                                System.gc()
                                val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
                                val heap = ManagementFactory.getMemoryMXBean()
                                val sampling = AtomicBoolean(true); val peak = AtomicLong(heap.heapMemoryUsage.used)
                                val sampler = Thread({ while (sampling.get()) {
                                    peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                                    LockSupport.parkNanos(2_000_000)
                                } }, "md-heap-probe").apply { isDaemon = true }
                                jsonCalls = 0; binaryCalls = 0; bridgeBytes = 0; largestJsonBytes = 0
                                val before = mx.getTotalThreadAllocatedBytes(); sampler.start()
                                val begin = System.nanoTime(); var checksum = 0L
                                val elapsed = try {
                                    repeat(3) { checksum += operation() }; System.nanoTime() - begin
                                } finally { sampling.set(false); sampler.join() }
                                val allocated = mx.getTotalThreadAllocatedBytes() - before
                                check(checksum == expected * 3)
                                println("GDAL4K_MD_MEMORY {\"mode\":\"$mode\",\"side\":$side,\"elements\":$valuesCount,\"payloadBytes\":${valuesCount * 8},\"rounds\":3,\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"checksum\":\"$checksum\",\"jsonCalls\":$jsonCalls,\"binaryCalls\":$binaryCalls,\"bridgeBytes\":$bridgeBytes,\"largestJsonBytes\":$largestJsonBytes,\"javaVersion\":\"${System.getProperty("java.version")}\"}")
                            } finally { attribute?.close() }
                        }
                    }
                }
            }
        } finally { engine.close() }
    }
}
