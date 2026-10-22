package dev.gdal4k

import dev.gdal4k.internal.BundledGdalRuntime
import dev.gdal4k.internal.RuntimeEngine
import kotlinx.coroutines.runBlocking
import java.lang.management.ManagementFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

/** Functional diagnostics of one combined engine, without a before/after variant. */
object CombinedDriverMemoryProbe {
    @JvmStatic fun main(args: Array<String>): Unit = runBlocking {
        require(args.size == 1)
        val engine = RuntimeEngine(createTransport(GdalOptions(libraryPath = args[0])))
        val runtime = BundledGdalRuntime(engine)
        try {
            combinedRoundTrip(runtime)
            System.gc()
            val allocation = ManagementFactory.getThreadMXBean() as com.sun.management.ThreadMXBean
            check(allocation.isThreadAllocatedMemorySupported)
            if (!allocation.isThreadAllocatedMemoryEnabled) allocation.isThreadAllocatedMemoryEnabled = true
            val heap = ManagementFactory.getMemoryMXBean()
            val sampling = AtomicBoolean(true)
            val peak = AtomicLong(heap.heapMemoryUsage.used)
            val sampler = Thread({
                while (sampling.get()) {
                    peak.accumulateAndGet(heap.heapMemoryUsage.used, ::maxOf)
                    LockSupport.parkNanos(2_000_000)
                }
            }, "combined-driver-heap-probe").apply { isDaemon = true }
            val before = allocation.getTotalThreadAllocatedBytes()
            sampler.start()
            val begin = System.nanoTime()
            var completed = 0
            val elapsed = try {
                repeat(3) { combinedRoundTrip(runtime); completed++ }
                System.nanoTime() - begin
            } finally { sampling.set(false); sampler.join() }
            val allocated = allocation.getTotalThreadAllocatedBytes() - before
            check(completed == 3 && allocated > 0)
            println("GDAL4K_COMBINED_DRIVER_MEMORY {\"rounds\":3,\"completedRounds\":$completed,\"contextKey\":\"$datasetOutputPrefix\",\"elapsedNanos\":$elapsed,\"jvmAllocatedBytes\":$allocated,\"sampledPeakHeapUsedBytes\":${maxOf(peak.get(), heap.heapMemoryUsage.used)},\"javaVersion\":\"${System.getProperty("java.version")}\"}")
        } finally { engine.close() }
    }
}
