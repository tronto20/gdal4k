package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.math.sqrt
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class MDArrayStatisticsApiTest {
    private suspend fun memory(block: suspend (Group) -> Unit) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.createMultidimensional("")).use { dataset ->
                requireNotNull(dataset.getRootGroup()).use { block(it) }
            }
        }
    }

    private fun expected(stats: MDStatistics, low: Double, high: Double, mean: Double,
                         deviation: Double, count: ULong) {
        assertEquals(low, stats.minimum, 1e-12); assertEquals(high, stats.maximum, 1e-12)
        assertEquals(mean, stats.mean, 1e-12); assertEquals(deviation, stats.standardDeviation, 1e-12)
        assertEquals(count, stats.validCount)
    }

    @Test fun populationMomentsUseNativeMaskAndMemDoesNotCache() = runTest(timeout = 120.seconds) {
        memory { group ->
            requireNotNull(group.createDimension("y", 2)).use { y ->
                requireNotNull(group.createDimension("x", 3)).use { x ->
                    for (type in listOf(GdalDataType.Float32, GdalDataType.Float64)) {
                        val snapshot = requireNotNull(group.createArray("values$type", listOf(y, x), type)).use { array ->
                            assertTrue(array.write(longArrayOf(0, 0), longArrayOf(2, 3), doubleArrayOf(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)))
                            assertTrue(array.setNoDataValue(MDValue.Numeric(6)))
                            assertNull(array.getStatistics())
                            val stats = array.computeStatistics()
                            expected(stats, 1.0, 5.0, 3.0, sqrt(2.0), 5uL)
                            assertNull(array.getStatistics())
                            expected(assertNotNull(array.getStatistics(force = true)), 1.0, 5.0, 3.0, sqrt(2.0), 5uL)
                            stats
                        }
                        expected(snapshot, 1.0, 5.0, 3.0, sqrt(2.0), 5uL)
                    }
                }
            }
        }
    }

    @Test fun scalarAndNoValidSamplesAreSuccessfulSnapshots() = runTest(timeout = 120.seconds) {
        memory { group ->
            requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Int16)).use { array ->
                assertTrue(array.write(longArrayOf(), longArrayOf(), doubleArrayOf(7.0)))
                expected(array.computeStatistics(), 7.0, 7.0, 7.0, 0.0, 1uL)
                assertTrue(array.setNoDataValue(MDValue.Numeric(7)))
                expected(array.computeStatistics(), Double.MAX_VALUE, -Double.MAX_VALUE, 0.0, 0.0, 0uL)
                assertNull(array.getStatistics())
                expected(assertNotNull(array.getStatistics(force = true)), Double.MAX_VALUE, -Double.MAX_VALUE, 0.0, 0.0, 0uL)
            }
        }
    }

    @Test fun exactIntegerNoDataCountsRemainSeparateFromDoubleRoundedMoments() = runTest(timeout = 120.seconds) {
        memory { group ->
            requireNotNull(group.createDimension("n", 2)).use { n ->
                for ((type, values) in listOf(
                    GdalDataType.Int64 to listOf("9007199254740992", "9007199254740993"),
                    GdalDataType.UInt64 to listOf("18446744073709551614", "18446744073709551615"))) {
                    requireNotNull(group.createArray("integers$type", listOf(n), type)).use { array ->
                        assertTrue(array.writeValues(longArrayOf(0), longArrayOf(2), values.map { MDValue.Numeric(it) }))
                        assertTrue(array.setNoDataValue(MDValue.Numeric(values[1])))
                        val rounded = values[0].toDouble()
                        expected(array.computeStatistics(), rounded, rounded, rounded, 0.0, 1uL)
                        assertEquals(values.map { MDValue.Numeric(it) }, array.readValues(longArrayOf(0), longArrayOf(2)))
                    }
                }
            }
        }
    }

    @Test fun cfAndNanCriteriaAndPhysicalViewsUseTheirActualArrayValues() = runTest(timeout = 120.seconds) {
        memory { group ->
            requireNotNull(group.createDimension("n", 6)).use { n ->
                requireNotNull(group.createArray("masked", listOf(n), GdalDataType.Float64)).use { array ->
                    assertTrue(array.write(longArrayOf(0), longArrayOf(6), doubleArrayOf(1.0, 2.0, 3.0, Double.NaN, 5.0, 6.0)))
                    assertTrue(array.setNoDataValue(MDValue.Numeric(6)))
                    requireNotNull(array.createAttribute("valid_range", longArrayOf(2), MDDataType.Numeric(GdalDataType.Float64))).use {
                        assertTrue(it.writeValues(listOf(MDValue.Numeric(2), MDValue.Numeric(5))))
                    }
                    expected(array.computeStatistics(), 2.0, 5.0, 10.0 / 3.0, sqrt(14.0 / 9.0), 3uL)
                }
            }
            requireNotNull(group.createDimension("small", 3)).use { n ->
                requireNotNull(group.createArray("scaled", listOf(n), GdalDataType.Float64)).use { array ->
                    assertTrue(array.write(longArrayOf(0), longArrayOf(3), doubleArrayOf(1.0, 2.0, 3.0)))
                    assertTrue(array.setScale(2.0)); assertTrue(array.setOffset(3.0))
                    expected(array.computeStatistics(), 1.0, 3.0, 2.0, sqrt(2.0 / 3.0), 3uL)
                    array.getUnscaled().use { expected(it.computeStatistics(), 5.0, 9.0, 7.0, 2.0 * sqrt(2.0 / 3.0), 3uL) }
                }
            }
        }
    }

    @Test fun unsupportedTypesAndNonfiniteSamplesPreserveNativeMeaning() = runTest(timeout = 120.seconds) {
        memory { group ->
            for ((name, type) in listOf(
                "text" to MDDataType.Text(), "complex" to MDDataType.Numeric(GdalDataType.CFloat64),
                "record" to MDDataType.Compound("one", listOf(MDComponent("value", MDDataType.Numeric(GdalDataType.Int16)))))) {
                requireNotNull(group.createArray(name, emptyList(), type)).use { array ->
                    assertNull(array.getStatistics())
                    assertFailsWith<GdalException> { array.getStatistics(force = true) }
                    assertFailsWith<GdalException> { array.computeStatistics() }
                    assertEquals(name, array.getName())
                }
            }
            requireNotNull(group.createDimension("n", 2)).use { n ->
                requireNotNull(group.createArray("infinite", listOf(n), GdalDataType.Float64)).use { array ->
                    assertTrue(array.write(longArrayOf(0), longArrayOf(2), doubleArrayOf(1.0, Double.POSITIVE_INFINITY)))
                    val stats = array.computeStatistics()
                    assertEquals(2uL, stats.validCount); assertEquals(1.0, stats.minimum)
                    assertEquals(Double.POSITIVE_INFINITY, stats.maximum)
                    assertFalse(stats.mean.isFinite()); assertFalse(stats.standardDeviation.isFinite())
                }
            }
        }
    }

    @Test fun progressAndObserverFailuresKeepTheSourceUsable() = runTest(timeout = 120.seconds) {
        memory { group ->
            requireNotNull(group.createArray("scalar", emptyList(), GdalDataType.Float64)).use { array ->
                assertTrue(array.write(longArrayOf(), longArrayOf(), doubleArrayOf(4.0)))
                val events = mutableListOf<GdalProgress>()
                expected(withGdalProgress({ events += it; true }) { array.computeStatistics() }, 4.0, 4.0, 4.0, 0.0, 1uL)
                assertTrue(events.isNotEmpty()); assertTrue(events.all { it.operation == "MDArray.computeStatistics" && it.fraction in 0.0..1.0 })
                assertFailsWith<CancellationException> { withGdalProgress({ false }) { array.getStatistics(force = true) } }
                val primary = IllegalArgumentException("MD statistics observer")
                val error = assertFailsWith<IllegalArgumentException> { withGdalProgress({ throw primary }) { array.computeStatistics() } }
                assertTrue(error === primary || error.cause === primary)
                expected(array.computeStatistics(), 4.0, 4.0, 4.0, 0.0, 1uL)
                assertContentEquals(doubleArrayOf(4.0), array.read(longArrayOf(), longArrayOf()))
            }
        }
    }
}
