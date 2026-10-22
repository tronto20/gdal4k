package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import dev.gdal4k.runtime.*
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Current-engine conversion diagnostics. The three result forms are not a before/after comparison. */
object MultiDimTranslateMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        val mode = args[0]; val side = args[1].toInt()
        require(mode in listOf("memory", "files", "direct") && side in 16..256)
        val fixture = Path.of(args[3]).toAbsolutePath()
        require(!Files.exists(fixture))
        val rows = side + 7; val columns = side + 11; val times = 3
        fun scaledValue(t: Int, y: Int, x: Int): Long = t * 1_000_000L + y * 1_000L + x + 1
        // A real, closed file gives GDAL's selected-array view a stable name to reopen.
        val xml = buildString {
            append("<VRTDataset><Group name=\"/\"><Dimension name=\"time\" size=\"$times\"/>")
            append("<Dimension name=\"y\" size=\"$rows\"/><Dimension name=\"x\" size=\"$columns\"/>")
            append("<Array name=\"cube\"><DataType>Float64</DataType><DimensionRef ref=\"/time\"/>")
            append("<DimensionRef ref=\"/y\"/><DimensionRef ref=\"/x\"/><InlineValues>")
            for (t in 0 until times) for (y in 0 until rows) for (x in 0 until columns) {
                append(scaledValue(t, y, x) / 4.0); append(' ')
            }
            append("</InlineValues></Array></Group></VRTDataset>")
        }.toByteArray(Charsets.UTF_8)
        Files.write(fixture, xml, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
        val fixtureSha = MessageDigest.getInstance("SHA-256").digest(xml).joinToString("") { "%02x".format(it) }
        val engine = RuntimeEngine(createTransport(GdalOptions(libraryPath = args[2])))
        val runtime = BundledGdalRuntime(engine)
        val directory = Files.createTempDirectory("gdal4k-md-translate-")
        val md = DatasetOpenOptions(kind = DatasetKind.Multidimensional)
        // GDAL applies transpose before view: (time,y,x) -> (time,x,y) -> (x,y).
        val options = listOf("-array", "name=cube,dstname=selected,transpose=[0,2,1],view=[1,:,:]")
        val elements = rows * columns
        var expectedChecksum = 0L
        for (x in 0 until columns) for (y in 0 until rows) {
            expectedChecksum += (x.toLong() * rows + y + 1) * scaledValue(1, y, x)
        }
        var outputBytes = 0L; var sequence = 0
        fun inspectValues(values: DoubleArray): Long {
            check(values.size == elements)
            var checksum = 0L
            for (x in 0 until columns) for (y in 0 until rows) {
                val index = x * rows + y; val scaled = scaledValue(1, y, x)
                check(values[index].toRawBits() == (scaled / 4.0).toRawBits())
                checksum += (index + 1L) * scaled
            }
            check(checksum == expectedChecksum)
            return checksum
        }
        suspend fun inspectMemory(output: Dataset): Long = output.use { dataset ->
            requireNotNull(dataset.getRootGroup()).use { group ->
                check(group.getArrayNames() == listOf("selected"))
                requireNotNull(group.openArray("selected")).use { array ->
                    check(array.getDataType() == GdalDataType.Float64)
                    val shape = mutableListOf<Long>()
                    for (dimension in array.getDimensions()) dimension.use { shape.add(it.getSize()) }
                    check(shape == listOf(columns.toLong(), rows.toLong()))
                    inspectValues(array.read(longArrayOf(0, 0), longArrayOf(columns.toLong(), rows.toLong())))
                }
            }
        }
        suspend fun inspectRaster(output: Dataset): Long = output.use { dataset ->
            check(dataset.getRasterXSize() == rows && dataset.getRasterYSize() == columns)
            check(dataset.getRasterCount() == 1)
            requireNotNull(dataset.getRasterBand(1)).use { band -> check(band.getRasterDataType() == GdalDataType.Float64) }
            inspectValues(dataset.readRaster())
        }
        try {
            // Validate all three original planes independently before warming the timed conversion.
            runtime.openDataset(fixture.toString(), md).use { source ->
                requireNotNull(source.getRootGroup()).use { group ->
                    requireNotNull(group.openArray("cube")).use { array ->
                        check(array.getDataType() == GdalDataType.Float64)
                        val values = array.read(longArrayOf(0, 0, 0), longArrayOf(times.toLong(), rows.toLong(), columns.toLong()))
                        check(values.size == times * rows * columns)
                        for (t in 0 until times) for (y in 0 until rows) for (x in 0 until columns) {
                            check(values[(t * rows + y) * columns + x].toRawBits() == (scaledValue(t, y, x) / 4.0).toRawBits())
                        }
                    }
                }
            }
            suspend fun operation(): Long {
                if (mode == "memory") {
                    val output = runtime.openDataset(fixture.toString(), md).use { it.multiDimTranslate(options) }
                    return inspectMemory(output) // Input handle is already closed.
                }
                if (mode == "files") {
                    val files = runtime.openDataset(fixture.toString(), md).use {
                        it.multiDimTranslateFiles(driver = "GTiff", name = "converted.tif", arguments = options)
                    }
                    outputBytes = files.files.sumOf { it.bytes.size.toLong() }
                    return inspectRaster(runtime.openDataset(files))
                }
                val path = directory.resolve("converted-${sequence++}.tif")
                try {
                    runtime.openDataset(fixture.toString(), md).use {
                        it.multiDimTranslateTo(path.toString(), driver = "GTiff", arguments = options)
                    }
                    outputBytes = Files.size(path)
                    return inspectRaster(runtime.openDataset(path.toString()))
                } finally { Files.deleteIfExists(path) }
            }
            check(operation() == expectedChecksum)
            System.gc()
            val mx = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
            val heap = ManagementFactory.getMemoryMXBean()
            val sampling = AtomicBoolean(true); val peak = AtomicLong(heap.heapMemoryUsage.used)
            val sampler = Thread({ while (sampling.get()) {
                peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                LockSupport.parkNanos(2_000_000)
            } }, "md-translate-heap-probe").apply { isDaemon = true }
            val before = mx.getTotalThreadAllocatedBytes(); sampler.start()
            val begin = System.nanoTime(); var checksum = 0L
            val elapsed = try { repeat(3) { checksum += operation() }; System.nanoTime() - begin }
                finally { sampling.set(false); sampler.join() }
            val allocated = mx.getTotalThreadAllocatedBytes() - before
            check(checksum == expectedChecksum * 3)
            check(mode == "memory" || outputBytes > 0)
            println("GDAL4K_MD_TRANSLATE_MEMORY {\"mode\":\"$mode\",\"side\":$side,\"times\":$times,\"sourceRows\":$rows,\"sourceColumns\":$columns,\"outputRows\":$columns,\"outputColumns\":$rows,\"elements\":$elements,\"rounds\":3,\"checksum\":\"$checksum\",\"logicalResultBytes\":${elements * 8L},\"fileOutputBytes\":$outputBytes,\"fixtureBytes\":${xml.size},\"fixtureSha256\":\"$fixtureSha\",\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
        } finally {
            try { engine.close() } finally {
                Files.walk(directory).use { paths -> paths.sorted(Comparator.reverseOrder()).forEach { Files.deleteIfExists(it) } }
            }
        }
    }
}
