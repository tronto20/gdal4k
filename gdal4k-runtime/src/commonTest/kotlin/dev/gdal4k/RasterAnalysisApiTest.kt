package dev.gdal4k

import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.math.sqrt
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class RasterAnalysisApiTest {
    private suspend fun <T> raster(values: DoubleArray, width: Int = 3, block: suspend (Dataset, Band) -> T): T =
        requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", width, values.size / width, 1, GdalDataType.Float64)).use { ds ->
                assertEquals(0, ds.setGeoTransform(doubleArrayOf(100.0, 2.0, 0.0, 200.0, 0.0, -2.0)))
                Gdal4k.runtime().createSpatialReference("EPSG:3857").use { assertEquals(0, ds.setSpatialRef(it)) }
                requireNotNull(ds.getRasterBand(1)).use { band ->
                    assertEquals(0, band.writeRaster(values)); block(ds, band)
                }
            }
        }
    private suspend fun values(band: Band): DoubleArray =
        DoubleArray(band.getXSize() * band.getYSize()).also { assertEquals(0, band.readRaster(it)) }

    @Test fun sieveConnectivityAndInPlace() = runTest(timeout = 120.seconds) {
        val original = doubleArrayOf(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
        raster(original) { _, source -> raster(DoubleArray(9)) { _, target ->
            assertEquals(0, source.sieveTo(target, 2))
            assertContentEquals(DoubleArray(9), values(target))
            assertContentEquals(original, values(source))
            assertEquals(0, source.sieveTo(target, 2, connectedness = 8))
            assertContentEquals(original, values(target))
            assertEquals(0, source.sieveTo(source, 2, connectedness = 8))
            assertContentEquals(original, values(source))
        } }
    }
    @Test fun sieveMasksAndNoData() = runTest(timeout = 120.seconds) {
        val input = DoubleArray(9) { 2.0 }.also { it[4] = 9.0 }
        raster(input) { _, source -> raster(DoubleArray(9)) { _, target ->
            assertEquals(0, source.setNoDataValue(9.0))
            assertEquals(0, source.sieveTo(target, 2))
            assertEquals(9.0, values(target)[4])
            assertEquals(0, source.sieveTo(target, 2, useDefaultMask = false))
            assertEquals(2.0, values(target)[4])
            raster(DoubleArray(9) { if (it == 4) 0.0 else 255.0 }) { _, mask ->
                assertEquals(0, source.sieveTo(target, 2, mask = mask, useDefaultMask = false))
                assertEquals(9.0, values(target)[4])
            }
        } }
    }
    @Test fun proximityPixelGeoTargetsAndLimit() = runTest(timeout = 120.seconds) {
        raster(DoubleArray(9) { if (it == 4) 7.0 else 0.0 }) { _, source -> raster(DoubleArray(9)) { _, target ->
            assertEquals(0, source.proximityTo(target))
            val pixels = values(target)
            assertEquals(0.0, pixels[4]); assertEquals(1.0, pixels[1]); assertEquals(sqrt(2.0), pixels[0], 0.00001)
            assertEquals(0, source.proximityTo(target, "VALUES=7", "DISTUNITS=GEO"))
            assertEquals(2.0, values(target)[1])
            assertEquals(0, source.proximityTo(target, arguments = listOf("MAXDIST=0.5", "NODATA=-9")))
            assertEquals(-9.0, values(target)[0]); assertEquals(0.0, values(target)[4])
            assertEquals(0, source.setNoDataValue(0.0))
            assertEquals(0, source.proximityTo(target, "USE_INPUT_NODATA=YES", "NODATA=-99"))
            assertEquals(-99.0, values(target)[0])
        } }
    }
    @Test fun fillDefaultAndExplicitMasksPreserveMetadata() = runTest(timeout = 120.seconds) {
        raster(DoubleArray(9) { if (it == 4) -9999.0 else 10.0 }) { _, band ->
            assertEquals(0, band.setNoDataValue(-9999.0))
            assertEquals(0, band.fillNoData())
            assertContentEquals(DoubleArray(9) { 10.0 }, values(band))
            assertEquals(-9999.0, band.getNoDataValue())
            val input = DoubleArray(9) { if (it == 4) 123.0 else 10.0 }
            assertEquals(0, band.writeRaster(input))
            raster(DoubleArray(9) { if (it == 4) 0.0 else 255.0 }) { _, mask ->
                assertEquals(0, band.fillNoData("INTERPOLATION=NEAREST", mask = mask, maxSearchDistance = 0.0))
                assertContentEquals(DoubleArray(9) { 10.0 }, values(band))
                assertContentEquals(DoubleArray(9) { if (it == 4) 0.0 else 255.0 }, values(mask))
            }
            assertEquals(0, band.writeRaster(DoubleArray(9) { if (it == 4) -9999.0 else 10.0 }))
            assertEquals(0, band.fillNoData(maxSearchDistance = 10.0, smoothingIterations = 1, arguments = emptyList()))
            assertEquals(10.0, values(band)[4])
        }
    }
    @Test fun contourLevelsCrsAndIndependentOutput() = runTest(timeout = 120.seconds) {
        val files = raster(DoubleArray(9) { (it % 3).toDouble() }) { ds, _ ->
            ds.contour(100.0, "FIXED_LEVELS=0.5,1.5")
        }
        Gdal4k.runtime().openDataset(files).use { ds -> requireNotNull(ds.getLayer(0)).use { layer ->
            assertEquals(2, layer.getFeatureCount().toInt())
            val elevations = mutableListOf<Double>()
            repeat(2) {
                requireNotNull(layer.getNextFeature()).use { feature ->
                    val elevation = feature.getFieldAsDouble("elevation"); elevations += elevation
                    requireNotNull(feature.getGeometryRef()).use { geometry ->
                        requireNotNull(geometry.getSpatialReference()).use { assertTrue(it.exportToWkt().contains("3857")) }
                        val bounds = geometry.getEnvelope()
                        assertEquals(101.0 + elevation * 2, bounds[0], 0.00001)
                        assertEquals(bounds[0], bounds[1], 0.00001)
                    }
                }
            }
            assertEquals(listOf(0.5, 1.5), elevations.sorted())
        } }
    }
    @Test fun contourDirectOutputAndEmptyLayer() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = datasetOutputPath("analysis-contours.gpkg")
        requireNotNull(runtime.getDriver("GPKG")).use { driver -> try {
            raster(DoubleArray(9) { (it % 3).toDouble() }) { ds, _ ->
                ds.contourTo(path, 1.0, base = 0.5)
                assertFails { ds.contourTo(path, 1.0) }
            }
            runtime.openDataset(path).use { ds -> requireNotNull(ds.getLayer(0)).use { assertEquals(2, it.getFeatureCount().toInt()) } }
            val empty = raster(DoubleArray(9) { -9999.0 }) { ds, band ->
                band.setNoDataValue(-9999.0); ds.contour(1.0)
            }
            runtime.openDataset(empty).use { ds -> requireNotNull(ds.getLayer(0)).use { assertEquals(0L, it.getFeatureCount()) } }
        } finally { driver.delete(path) } }
    }
    @Test fun readOnlyTargetsAndUnsupportedBandTypes() = runTest(timeout = 120.seconds) {
        raster(DoubleArray(9) { 2.0 }) { ds, source ->
            Gdal4k.runtime().openDataset(ds.translate(driver = "GTiff")).use { readOnly ->
                requireNotNull(readOnly.getRasterBand(1)).use { target ->
                    assertFails { source.sieveTo(target, 2) }
                    assertFails { source.proximityTo(target) }
                    assertFails { target.fillNoData() }
                    assertContentEquals(DoubleArray(9) { 2.0 }, values(target))
                }
            }
            requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
                for (type in listOf(GdalDataType.CFloat64, GdalDataType.UInt64)) {
                    requireNotNull(driver.create("", 3, 3, 1, type)).use { typed ->
                        requireNotNull(typed.getRasterBand(1)).use { band ->
                            assertFails { band.sieveTo(source, 2) }
                            if (type == GdalDataType.CFloat64) {
                                assertFails { band.fillNoData() }
                                assertFails { band.proximityTo(source) }
                                assertFails { typed.contour(1.0) }
                            }
                        }
                    }
                }
            }
        }
    }
    @Test fun sieveUsesFreshMasksAfterNoDataChanges() = runTest(timeout = 120.seconds) {
        raster(DoubleArray(9) { if (it == 4) 9.0 else 2.0 }) { _, source -> raster(DoubleArray(9)) { _, target ->
            assertEquals(0, source.sieveTo(target, 2)); assertContentEquals(DoubleArray(9) { 2.0 }, values(target))
            assertEquals(0, source.setNoDataValue(9.0)); val oldMask = requireNotNull(source.getMaskBand())
            assertEquals(0, source.sieveTo(target, 2)); assertEquals(9.0, values(target)[4])
            assertEquals(0, source.setNoDataValue(2.0)); assertFails { oldMask.getXSize() }; oldMask.close()
            requireNotNull(source.getMaskBand()).use { assertContentEquals(DoubleArray(9) { if (it == 4) 255.0 else 0.0 }, values(it)) }
            assertEquals(0, source.deleteNoDataValue())
            assertEquals(0, source.sieveTo(target, 2)); assertContentEquals(DoubleArray(9) { 2.0 }, values(target))
        } }
    }
    @Test fun invalidArgumentsAndClosedHandlesLeaveInputsUsable() = runTest(timeout = 120.seconds) {
        raster(DoubleArray(9) { 2.0 }) { ds, band -> raster(DoubleArray(9)) { _, target ->
            assertFails { band.sieveTo(target, 0) }; assertFails { band.sieveTo(target, 2, 6) }
            assertFails { band.sieveTo(target, 2, mask = target) }
            assertFails { band.proximityTo(band) }
            for (option in listOf("BAD=1", "MAXDIST=-1", "VALUES=1,", "VALUES=2147483648", "NODATA=NaN", "DISTUNITS=x", "VALUES=1\u0000"))
                assertFails { band.proximityTo(target, option) }
            for (option in listOf("BAD=1", "NODATA=Inf", "INTERPOLATION=x", "TEMP_FILE_DRIVER=missing")) assertFails { band.fillNoData(option) }
            assertFails { band.fillNoData(maxSearchDistance = -1.0) }; assertFails { band.fillNoData(maxSearchDistance = Double.NaN) }
            assertFails { band.fillNoData(smoothingIterations = -1) }; assertFails { band.fillNoData(mask = band) }
            raster(DoubleArray(4), 2) { _, wrong ->
                assertFails { band.sieveTo(wrong, 2) }; assertFails { band.proximityTo(wrong) }; assertFails { band.fillNoData(mask = wrong) }
            }
            for (interval in listOf(0.0, -1.0, Double.POSITIVE_INFINITY)) assertFails { ds.contour(interval) }
            for (option in listOf("ID_FIELD=5", "FIXED_LEVELS=1,", "LEVEL_EXP_BASE=1", "-dsco")) assertFails { ds.contour(1.0, option) }
            assertFails { ds.contour(1.0, band = 0) }; assertFails { ds.contour(1.0, driver = "MEM") }
            assertFails { ds.contourTo("", 1.0) }; assertFails { ds.contour(1.0, name = "../bad.gpkg") }
            assertContentEquals(DoubleArray(9) { 2.0 }, values(band))
            target.close(); assertFails { band.proximityTo(target) }
            band.close(); assertFails { band.fillNoData() }
        } }
    }
    @Test fun unsignedProximityTargetsUseScopedScratchAndPreserveInputs() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val input = DoubleArray(9) { if (it == 4) 1.0 else 0.0 }
        val expected = DoubleArray(9) { if (it == 4) 0.0 else 1.0 }
        raster(input) { _, source ->
            for (type in listOf(GdalDataType.Byte, GdalDataType.UInt16, GdalDataType.UInt32)) {
                requireNotNull(runtime.getDriver("MEM")).use { driver ->
                    requireNotNull(driver.create("", 3, 3, 1, type)).use { ds -> requireNotNull(ds.getRasterBand(1)).use { target ->
                        repeat(2) {
                            assertEquals(0, source.proximityTo(target), "MEM $type")
                            assertContentEquals(expected, values(target))
                        }
                    } }
                }
                val name = "unsigned-proximity-$type.tif"
                val web = GdalPlatform.current().osName == "web"
                // Raw Driver.create uses GDAL's filesystem; host/OPFS outputs use translateTo.
                val path = if (web) "/vsimem/$datasetOutputPrefix-$name" else datasetOutputPath(name)
                requireNotNull(runtime.getDriver("GTiff")).use { driver -> try {
                    requireNotNull(driver.create(path, 3, 3, 1, type)).use { ds -> requireNotNull(ds.getRasterBand(1)).use { target ->
                        assertEquals(0, source.proximityTo(target), "GTiff $type")
                        assertContentEquals(expected, values(target))
                    } }
                    runtime.openDataset(path).use { ds ->
                        requireNotNull(ds.getRasterBand(1)).use { target ->
                            assertEquals(type, target.getRasterDataType()); assertContentEquals(expected, values(target))
                        }
                        if (web) {
                            val exported = datasetOutputPath("export-$name")
                            try {
                                ds.translateTo(exported, driver = "GTiff")
                                runtime.openDataset(exported).use { reopened -> requireNotNull(reopened.getRasterBand(1)).use { target ->
                                    assertEquals(type, target.getRasterDataType()); assertContentEquals(expected, values(target))
                                } }
                            } finally { driver.delete(exported) }
                        }
                    }
                } finally { driver.delete(path) } }
            }
            assertContentEquals(input, values(source))
        }
    }

}
