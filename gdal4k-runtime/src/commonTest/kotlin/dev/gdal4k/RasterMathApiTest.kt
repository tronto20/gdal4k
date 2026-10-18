package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.job
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import kotlin.math.sqrt
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class RasterMathApiTest {
    private suspend fun <T> band(values: DoubleArray, width: Int = 3, type: Int = GdalDataType.Float64, block: suspend (Band) -> T): T =
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", width, values.size / width, 1, type)).use { ds ->
                requireNotNull(ds.getRasterBand(1)).use { b -> b.writeRaster(values); block(b) }
            }
        }
    private suspend fun pixels(band: Band): DoubleArray = DoubleArray(band.getXSize() * band.getYSize()).also { assertEquals(0, band.readRaster(it)) }
    @Test fun multiInputCalculationHandlesPartialWindows() = runTest(timeout = 120.seconds) {
        val original = DoubleArray(15) { it.toDouble() }
        band(original, 5) { first -> band(DoubleArray(15) { 2.0 }, 5) { second -> band(DoubleArray(15), 5) { target ->
            rasterCalculate(listOf(first, second), target, RasterChunkSize(2, 2)) { it[0] * it[1] + 1 }
            assertContentEquals(DoubleArray(15) { it * 2.0 + 1 }, pixels(target))
            assertContentEquals(original, pixels(first)); assertContentEquals(DoubleArray(15) { 2.0 }, pixels(second))
        } } }
    }
    @Test fun masksAndNonfiniteValuesPropagateToNoData() = runTest(timeout = 120.seconds) {
        band(doubleArrayOf(-9999.0, 1.0, Double.NaN, 2.0, Double.POSITIVE_INFINITY, 3.0)) { source ->
            source.setNoDataValue(-9999.0)
            band(DoubleArray(6)) { target ->
                rasterCalculate(listOf(source), target, RasterChunkSize(2, 1), -99.0) { if (it[0] == 2.0) Double.POSITIVE_INFINITY else it[0] + 1 }
                assertContentEquals(doubleArrayOf(-99.0, 2.0, -99.0, -99.0, -99.0, 4.0), pixels(target))
                assertEquals(-99.0, target.getNoDataValue()); assertEquals(-9999.0, source.getNoDataValue())
            }
        }
    }
    @Test fun reclassificationSortsDisjointRangesAndUsesHalfOpenBounds() = runTest(timeout = 120.seconds) {
        band(doubleArrayOf(-1.0, 0.0, 1.0, 2.0, 3.0, 4.0)) { source -> band(DoubleArray(6)) { target ->
            source.reclassifyTo(target, listOf(RasterClass(2.0, 4.0, 20.0), RasterClass(0.0, 2.0, 10.0)), RasterChunkSize(2, 1), -99.0)
            assertContentEquals(doubleArrayOf(-99.0, 10.0, 10.0, 20.0, 20.0, -99.0), pixels(target))
            assertContentEquals(doubleArrayOf(-1.0, 0.0, 1.0, 2.0, 3.0, 4.0), pixels(source))
        } }
    }
    @Test fun inPlaceProcessingUsesOriginalNoDataUntilCompletion() = runTest(timeout = 120.seconds) {
        band(doubleArrayOf(0.0, 1.0, -99.0, 2.0, 3.0, 4.0)) { source ->
            source.setNoDataValue(-99.0)
            source.reclassifyTo(source, listOf(RasterClass(Double.NEGATIVE_INFINITY, 1.0, 5.0), RasterClass(1.0, Double.POSITIVE_INFINITY, 10.0)), RasterChunkSize(1, 1), -1.0)
            assertContentEquals(doubleArrayOf(5.0, 10.0, -1.0, 10.0, 10.0, 10.0), pixels(source))
            assertEquals(-1.0, source.getNoDataValue())
        }
    }
    @Test fun zonalStatisticsUseRawMaskedSamplesAndPopulationDeviation() = runTest(timeout = 120.seconds) {
        band(doubleArrayOf(1.0, 3.0, 10.0, 4.0, -9999.0, 6.0)) { source ->
            source.setNoDataValue(-9999.0); source.setScale(100.0); source.setOffset(1000.0)
            band(doubleArrayOf(0.0, 0.0, 1.0, 1.0, 1.0, 1.0)) { zones ->
                val result = source.zonalStatistics(zones, RasterChunkSize(2, 1))
                assertEquals(RasterZoneStatistics(2, 1.0, 3.0, 4.0, 2.0, 1.0), result[0L])
                val one = requireNotNull(result[1L]); assertEquals(3L, one.count); assertEquals(4.0, one.minimum); assertEquals(10.0, one.maximum)
                assertEquals(20.0, one.sum); assertEquals(20.0 / 3, one.mean, 0.000001); assertEquals(sqrt(56.0 / 9), one.standardDeviation, 0.000001)
                zones.setNoDataValue(1.0); assertEquals(setOf(0L), source.zonalStatistics(zones).keys)
                zones.setNoDataValue(0.0); assertEquals(setOf(1L), source.zonalStatistics(zones).keys)
                source.fill(-9999.0); assertTrue(source.zonalStatistics(zones).isEmpty())
            }
        }
    }
    @Test fun zoneLabelsAndNumericOverflowAreExplicit() = runTest(timeout = 120.seconds) {
        band(doubleArrayOf(1.0, 2.0, 3.0)) { source ->
            band(doubleArrayOf(0.5, 0.0, 1.0)) { zones -> assertFails { source.zonalStatistics(zones) } }
            band(doubleArrayOf(-2147483648.0, 4294967295.0, 0.0)) { zones ->
                val result = source.zonalStatistics(zones); assertEquals(setOf(-2147483648L, 4294967295L, 0L), result.keys)
            }
        }
        band(doubleArrayOf(1e308, -1e308, 1e308)) { source -> band(DoubleArray(3)) { zones -> assertFails { source.zonalStatistics(zones) } } }
    }
    @Test fun failuresAndCancellationPreserveCallerHandles() = runTest(timeout = 120.seconds) {
        band(DoubleArray(9) { it.toDouble() }) { source -> band(DoubleArray(9)) { target ->
            source.setNoDataValue(-99.0)
            assertFailsWith<IllegalStateException> { rasterCalculate(listOf(source), target, RasterChunkSize(1, 1)) { error("callback failed") } }
            assertEquals(3, source.getXSize()); assertEquals(3, target.getXSize())
            val job = launch {
                val self = currentCoroutineContext().job
                rasterCalculate(listOf(source), target, RasterChunkSize(1, 1)) { self.cancel(); it[0] }
            }
            job.join(); assertTrue(job.isCancelled)
            assertContentEquals(DoubleArray(9) { it.toDouble() }, pixels(source))
            rasterCalculate(listOf(source), target) { it[0] + 1 }
            assertContentEquals(DoubleArray(9) { it + 1.0 }, pixels(target))
        } }
    }
    @Test fun noDataChangesRefreshGeneratedMasksAndPreserveExplicitMasks() = runTest(timeout = 120.seconds) {
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 3, 1, 1, GdalDataType.Float64)).use { ds -> requireNotNull(ds.getRasterBand(1)).use { source ->
                source.writeRaster(doubleArrayOf(1.0, -99.0, 2.0))
                val firstMask = requireNotNull(source.getMaskBand())
                assertEquals(0, source.setNoDataValue(-99.0)); assertFails { firstMask.getXSize() }; firstMask.close()
                val noDataMask = requireNotNull(source.getMaskBand()); assertContentEquals(doubleArrayOf(255.0, 0.0, 255.0), pixels(noDataMask))
                assertFails { source.setNoDataValueAsInt64(1) }; assertEquals(3, noDataMask.getXSize())
                source.reclassifyTo(source, listOf(RasterClass(0.0, 2.0, 10.0), RasterClass(2.0, 3.0, 20.0)), outputNoData = -1.0)
                assertFails { noDataMask.getXSize() }; noDataMask.close()
                Gdal4k.runtime().openDataset(ds.polygonize()).use { result -> requireNotNull(result.getLayer(0)).use { assertEquals(2L, it.getFeatureCount()) } }
                val finalMask = requireNotNull(source.getMaskBand()); assertEquals(0, source.deleteNoDataValue())
                assertFails { finalMask.getXSize() }; finalMask.close()
                requireNotNull(source.getMaskBand()).use { assertContentEquals(doubleArrayOf(255.0, 255.0, 255.0), pixels(it)) }
                assertEquals(0, source.createMaskBand(0))
                val explicit = requireNotNull(source.getMaskBand()); explicit.writeRaster(byteArrayOf(-1, 0, -1))
                assertEquals(0, source.setNoDataValue(20.0)); assertFails { explicit.getXSize() }; explicit.close()
                requireNotNull(source.getMaskBand()).use { assertContentEquals(doubleArrayOf(255.0, 0.0, 255.0), pixels(it)) }
            } }
        }
    }
    @Test fun invalidTypesRangesDimensionsAndNoDataFailBeforeWriting() = runTest(timeout = 120.seconds) {
        assertFails { RasterChunkSize(0, 1) }
        band(DoubleArray(9)) { source -> band(DoubleArray(9) { 7.0 }) { target ->
            assertFails { rasterCalculate(emptyList(), target) { 1.0 } }
            for (ranges in listOf(emptyList(), listOf(RasterClass(0.0, 2.0, 1.0), RasterClass(1.0, 3.0, 2.0)), listOf(RasterClass(1.0, 1.0, 0.0)), listOf(RasterClass(0.0, 1.0, Double.NaN))))
                assertFails { source.reclassifyTo(target, ranges) }
            band(DoubleArray(4), 2) { wrong -> assertFails { rasterCalculate(listOf(source), wrong) { it[0] } }; assertFails { source.zonalStatistics(wrong) } }
            for (type in listOf(GdalDataType.Int64, GdalDataType.UInt64, GdalDataType.CFloat64)) band(DoubleArray(9), type = type) { unsupported ->
                assertFails { rasterCalculate(listOf(unsupported), target) { it[0] } }; assertFails { rasterCalculate(listOf(source), unsupported) { it[0] } }
            }
            band(DoubleArray(9), type = GdalDataType.Byte) { integer ->
                assertFails { rasterCalculate(listOf(source), integer) { 1.0 } }
                assertFails { rasterCalculate(listOf(source), integer, outputNoData = -1.0) { 1.0 } }
                rasterCalculate(listOf(source), integer, outputNoData = 255.0) { 2.0 }; assertContentEquals(DoubleArray(9) { 2.0 }, pixels(integer))
            }
            assertContentEquals(DoubleArray(9) { 7.0 }, pixels(target))
            target.close(); assertFails { rasterCalculate(listOf(source), target) { it[0] } }
        } }
    }
}
