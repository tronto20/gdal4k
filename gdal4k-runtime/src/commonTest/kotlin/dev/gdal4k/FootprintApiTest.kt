package dev.gdal4k

import dev.gdal4k.GdalFiles
import dev.gdal4k.GdalFile
import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.math.*
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class FootprintApiTest {
    private suspend fun raster(values: IntArray, width: Int, nodata: Boolean = true): Dataset {
        val runtime = Gdal4k.runtime()
        return requireNotNull(runtime.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", width, values.size / width, 1, GdalDataType.Byte)).also { ds ->
                assertEquals(0, ds.writeRaster(values))
                assertEquals(0, ds.setGeoTransform(doubleArrayOf(100.0, 2.0, 0.0, 50.0, 0.0, -3.0)))
                runtime.createSpatialReference("EPSG:3857").use { assertEquals(0, ds.setSpatialRef(it)) }
                if (nodata) requireNotNull(ds.getRasterBand(1)).use { assertEquals(0, it.setNoDataValue(0.0)) }
            }
        }
    }

    private suspend fun area(files: GdalFiles): Double = Gdal4k.runtime().openDataset(files).use { ds ->
        requireNotNull(ds.getLayer(0)).use { layer ->
            var area = 0.0
            while (true) {
                val feature = layer.getNextFeature() ?: break
                feature.use { requireNotNull(it.getGeometryRef()).use { geometry -> area += geometry.getArea() } }
            }
            area
        }
    }

    @Test fun holeRotationCrsAndIndependentOutput() = runTest(timeout = 120.seconds) {
        val files = raster(intArrayOf(1, 1, 1, 1, 0, 1, 1, 1, 1), 3).use { source ->
            assertEquals(0, source.setGeoTransform(doubleArrayOf(100.0, 2.0, 0.5, 50.0, 0.25, -3.0)))
            source.footprint()
        }
        Gdal4k.runtime().openDataset(files).use { ds ->
            requireNotNull(ds.getLayer(0)).use { layer ->
                assertEquals(1L, layer.getFeatureCount(true))
                assertContentEquals(doubleArrayOf(100.0, 107.5, 41.0, 50.75), layer.getExtent(true))
                requireNotNull(layer.getNextFeature()).use { f ->
                    assertEquals(-1, f.getFieldIndex("location"))
                    requireNotNull(f.getGeometryRef()).use { multi ->
                        requireNotNull(multi.getSpatialReference()).use { assertTrue(it.exportToWkt().contains("3857")) }
                        assertEquals(49.0, multi.getArea(), 1e-8) // 8 valid pixels * abs(affine determinant)
                        assertTrue(multi.isValid())
                        requireNotNull(multi.getGeometryRef(0)).use { assertEquals(2, it.getGeometryCount()) }
                    }
                }
            }
        }
    }

    @Test fun emptyAllValidAndSplitDisconnectedRegions() = runTest(timeout = 120.seconds) {
        raster(IntArray(4), 2).use { source ->
            Gdal4k.runtime().openDataset(source.footprint()).use { ds ->
                requireNotNull(ds.getLayer(0)).use { assertEquals(0L, it.getFeatureCount(true)) }
            }
        }
        // A value of zero is valid unless a mask or NoData excludes it.
        raster(IntArray(4), 2, nodata = false).use { assertEquals(24.0, area(it.footprint())) }
        raster(intArrayOf(2, 0, 0, 9), 2).use { source ->
            assertEquals(12.0, area(source.footprint()))
            Gdal4k.runtime().openDataset(source.footprint("-split_polys")).use { ds ->
                requireNotNull(ds.getLayer(0)).use { assertEquals(2L, it.getFeatureCount(true)) }
            }
        }
    }

    @Test fun selectedBandsUnionIntersectionExplicitMasksAndNoDataOverride() = runTest(timeout = 120.seconds) {
        raster(intArrayOf(1, 0, 0, 0), 2).use { source ->
            assertEquals(0, source.addBand(GdalDataType.Byte))
            requireNotNull(source.getRasterBand(2)).use { band ->
                assertEquals(0, band.writeRaster(intArrayOf(0, 1, 0, 0)))
                assertEquals(0, band.setNoDataValue(0.0))
            }
            assertEquals(12.0, area(source.footprint()))
            assertEquals(0.0, area(source.footprint("-combine_bands", "intersection")))
            assertEquals(6.0, area(source.footprint("-b", "2")))
            assertEquals(18.0, area(source.footprint("-b", "1", "-srcnodata", "1")))
            requireNotNull(source.getRasterBand(1)).use { band ->
                assertEquals(0, band.createMaskBand(0))
                requireNotNull(band.getMaskBand()).use { mask ->
                    assertEquals(0, mask.writeRaster(intArrayOf(0, 0, 255, 255)))
                }
                assertEquals(12.0, area(source.footprint("-b", "1")))
                assertEquals(0.0, band.getNoDataValue()) // no input NoData mutation by -srcnodata
            }
        }
    }

    @Test fun pixelCoordinatesReprojectionAndNoSilentPointLimit() = runTest(timeout = 120.seconds) {
        raster(intArrayOf(1, 1, 1, 1), 2).use { source ->
            Gdal4k.runtime().openDataset(source.footprint("-t_cs", "pixel")).use { ds ->
                requireNotNull(ds.getLayer(0)).use { layer ->
                    assertContentEquals(doubleArrayOf(0.0, 2.0, 0.0, 2.0), layer.getExtent(true))
                    requireNotNull(layer.getNextFeature()).use { feature ->
                        requireNotNull(feature.getGeometryRef()).use { assertNull(it.getSpatialReference()) }
                    }
                }
            }
            Gdal4k.runtime().openDataset(source.footprint("-t_srs", "EPSG:4326")).use { ds ->
                requireNotNull(ds.getLayer(0)).use { layer ->
                    val expected = doubleArrayOf(100.0 / 6378137.0 * 180.0 / PI, 104.0 / 6378137.0 * 180.0 / PI,
                        atan(sinh(44.0 / 6378137.0)) * 180.0 / PI, atan(sinh(50.0 / 6378137.0)) * 180.0 / PI)
                    requireNotNull(layer.getExtent(true)).forEachIndexed { i, value -> assertEquals(expected[i], value, 1e-9) }
                }
            }
        }
        val width = 129
        val comb = IntArray(width * 3) { index -> if (index / width == 0 || index / width == 1 && index % 2 == 1) 1 else 0 }
        val valid = comb.count { it == 1 }
        raster(comb, width).use { assertEquals(valid * 6.0, area(it.footprint()), 1e-8) }
    }

    @Test fun directOutputsSidecarsExistingOutputAndCallerOwnership() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        raster(intArrayOf(1, 1, 1, 1), 2).use { source ->
            for ((format, extension) in listOf("GPKG" to "gpkg", "GeoJSON" to "geojson", "ESRI Shapefile" to "shp")) {
                val path = datasetOutputPath("footprint-direct.$extension")
                requireNotNull(runtime.getDriver(format)).use { driver ->
                    try {
                        source.footprintTo(path, driver = format, layerName = "valid_data")
                        runtime.openDataset(path).use { ds ->
                            requireNotNull(ds.getLayer(0)).use { layer ->
                                assertEquals(1L, layer.getFeatureCount(true))
                                requireNotNull(layer.getNextFeature()).use { f ->
                                    requireNotNull(f.getGeometryRef()).use { assertEquals(24.0, it.getArea()) }
                                }
                            }
                        }
                        assertFailsWith<GdalException> { source.footprintTo(path, driver = format) }
                        runtime.openDataset(path).use { assertEquals(1, it.getLayerCount()) }
                    } finally { driver.delete(path) }
                }
            }
            val shp = source.footprint(driver = "ESRI Shapefile", name = "valid.shp")
            assertTrue(shp.files.map { it.name }.containsAll(listOf("valid.shp", "valid.shx", "valid.dbf", "valid.prj")))
            assertEquals(24.0, area(shp))
            val input = IntArray(4); assertEquals(0, source.readRaster(input)); assertContentEquals(intArrayOf(1, 1, 1, 1), input)
        }
    }

    @Test fun cancellationCallbackFailureAndVirtualSourceLifetimes() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        val vrt = raster(IntArray(256 * 256) { if (it % 3 == 0) 0 else 1 }, 256).use { runtime.buildVRT(listOf(it)) }
        vrt.use { source ->
            assertFailsWith<CancellationException> { withGdalProgress({ false }) { source.footprint() } }
            val callbackFailure = IllegalStateException("footprint callback")
            assertSame(callbackFailure, assertFailsWith<IllegalStateException> {
                withGdalProgress({ throw callbackFailure }) { source.footprint() }
            })
            val files = source.footprint()
            assertTrue(area(files) > 0.0)
            assertEquals(256, source.getRasterXSize())
        }
    }

    @Test fun invalidOptionsFormatsPathsAndClosedOrVectorSources() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime()
        raster(intArrayOf(1, 0, 0, 1), 2).use { source ->
            for (format in listOf("MEM", "Memory", "VRT", "GTiff", "missing", "GPKG\u0000")) {
                assertFails { source.footprint(driver = format) }
            }
            for (args in listOf(listOf("-b", "0"), listOf("-b", "2"), listOf("-b"), listOf("-bad"),
                listOf("-of", "GPKG"), listOf("-lyr_name", "override"), listOf("-location_field_name", "leak"),
                listOf("-ovr", "99"), listOf("-t_cs", "invalid"), listOf("-t_srs", "invalid"),
                listOf("-combine_bands", "invalid"), listOf("-lco", "BAD\u0000=1"), listOf("unexpected.gpkg"))) {
                assertFails { source.footprint(arguments = args) }
            }
            assertFails { source.footprint(name = "../bad.gpkg") }
            assertFails { source.footprint(layerName = "") }
            assertFails { source.footprintTo("") }
            assertFails { source.footprintTo("bad\u0000path") }
            assertEquals(12.0, area(source.footprint()))
            source.close(); assertFails { source.footprint() }
        }
        val json = """{"type":"FeatureCollection","features":[]}"""
        runtime.openDataset(GdalFiles(GdalFile("empty.geojson", json.encodeToByteArray()))).use { vector ->
            assertFails { vector.footprint() }
        }
    }
}
