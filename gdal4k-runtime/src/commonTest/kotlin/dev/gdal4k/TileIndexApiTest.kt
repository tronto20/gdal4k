package dev.gdal4k

import dev.gdal4k.GdalFiles
import dev.gdal4k.GdalException
import dev.gdal4k.runtime.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class TileIndexApiTest {
    private suspend fun r() = Gdal4k.runtime()
    private suspend fun raster(path: String, x: Double = 100.0, crs: String? = "EPSG:3857", rotated: Boolean = false) {
        requireNotNull(r().getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 4, 3, 1, GdalDataType.Byte)).use { source ->
                // NoData holes do not change a tile index's full raster extent.
                assertEquals(0, source.writeRaster(IntArray(12) { if (it == 5) 0 else 7 }))
                assertEquals(0, source.setGeoTransform(doubleArrayOf(x, 2.0, if (rotated) 0.5 else 0.0, 50.0, if (rotated) 0.25 else 0.0, -3.0)))
                if (crs != null) r().createSpatialReference(crs).use { assertEquals(0, source.setSpatialRef(it)) }
                requireNotNull(source.getRasterBand(1)).use { assertEquals(0, it.setNoDataValue(0.0)) }
                source.translateTo(path)
            }
        }
    }
    private suspend fun <T> fixtures(block: suspend (List<String>) -> T): T {
        val paths = listOf("/vsimem/tile-index-tests/a.tif", "/vsimem/tile-index-tests/b.tif")
        // writeMemoryFile creates the owned test directory on every target.
        r().writeMemoryFile("/vsimem/tile-index-tests/sentinel", byteArrayOf(1))
        try { raster(paths[0], rotated = true); raster(paths[1], x = 200.0); return block(paths) }
        finally { r().deleteMemoryFile("/vsimem/tile-index-tests") }
    }
    private suspend fun locations(files: GdalFiles, field: String = "location"): List<String> = r().openDataset(files).use { ds ->
        requireNotNull(ds.getLayer(0)).use { layer ->
            buildList {
                while (true) {
                    val feature = layer.getNextFeature() ?: break
                    feature.use { add(it.getFieldAsString(field)) }
                }
            }
        }
    }

    @Test fun multipleInputsRotatedFullExtentAndNoDataDoesNotClip() = runTest(timeout = 120.seconds) { fixtures { paths ->
        r().openDataset(r().tileIndex(paths)).use { ds -> requireNotNull(ds.getLayer(0)).use { layer ->
            assertEquals(2L, layer.getFeatureCount(true))
            requireNotNull(layer.getNextFeature()).use { feature ->
                assertEquals(paths[0], feature.getFieldAsString("location"))
                requireNotNull(feature.getGeometryRef()).use { polygon ->
                    requireNotNull(polygon.getSpatialReference()).use { assertTrue(it.exportToWkt().contains("3857")) }
                    assertEquals(73.5, polygon.getArea(), 1e-9) // 4*3*abs(2*(-3)-0.5*0.25)
                    assertTrue(polygon.isValid())
                    requireNotNull(polygon.getGeometryRef(0)).use { ring ->
                        val expected = listOf(100.0 to 50.0, 108.0 to 51.0, 109.5 to 42.0, 101.5 to 41.0, 100.0 to 50.0)
                        assertEquals(5, ring.getPointCount())
                        expected.forEachIndexed { i, p -> val point = ring.getPoint(i); assertEquals(p.first, point[0]); assertEquals(p.second, point[1]) }
                    }
                }
            }
            requireNotNull(layer.getNextFeature()).use { feature ->
                assertEquals(paths[1], feature.getFieldAsString("location"))
                requireNotNull(feature.getGeometryRef()).use { assertEquals(72.0, it.getArea()) }
            }
            assertNull(layer.getNextFeature())
        } }
        r().openDataset(paths[0]).use { assertEquals(4, it.getRasterXSize()) }
    } }

    @Test fun namedFieldsCrsMetadataAndIndependentOutputAfterSourcesRemoved() = runTest(timeout = 120.seconds) {
        val files = fixtures { paths ->
            val result = r().tileIndex(paths, "-src_srs_name", "crs", "-src_srs_format", "EPSG", locationField = "raster", layerName = "catalog")
            assertEquals(paths, locations(result, "raster"))
            r().openDataset(result).use { ds -> requireNotNull(ds.getLayer("catalog")).use { layer ->
                requireNotNull(layer.getNextFeature()).use { assertEquals("EPSG:3857", it.getFieldAsString("crs")) }
            } }
            result
        }
        // Independent vector geometry remains usable; referenced rasters are not copied or leased.
        assertEquals(2, locations(files, "raster").size)
        assertFails { r().openDataset("/vsimem/tile-index-tests/a.tif") }
    }

    @Test fun mixedCrsRequiresTargetAndReprojectionMatchesWarpGrid() = runTest(timeout = 120.seconds) { fixtures { paths ->
        r().deleteMemoryFile(paths[1]); raster(paths[1], x = 1.0, crs = "EPSG:4326")
        assertFailsWith<GdalException> { r().tileIndex(paths) }
        val files = r().tileIndex(paths, "-t_srs", "EPSG:4326")
        r().openDataset(files).use { ds -> requireNotNull(ds.getLayer(0)).use { layer ->
            assertEquals(2L, layer.getFeatureCount(true))
            requireNotNull(layer.getNextFeature()).use { feature -> requireNotNull(feature.getGeometryRef()).use { geometry ->
                requireNotNull(geometry.getSpatialReference()).use { assertTrue(it.exportToWkt().contains("4326")) }
                // Pinned GDAL uses a warped VRT grid for reprojected bounds. Independently
                // generate that grid and compare its affine extent instead of assuming 4 corners.
                r().openDataset(paths[0]).use { source -> r().openDataset(source.warp("EPSG:4326")).use { warped ->
                    val gt = requireNotNull(warped.getGeoTransform())
                    val expected = doubleArrayOf(gt[0], gt[0] + gt[1] * warped.getRasterXSize(), gt[3] + gt[5] * warped.getRasterYSize(), gt[3])
                    val envelope = geometry.getEnvelope()
                    expected.forEachIndexed { i, value -> assertEquals(value, envelope[i], 1e-10) }
                    assertTrue(envelope[1] < 0.002 && envelope[3] < 0.002)
                } }
            } }
        } }
    } }

    @Test fun memorySidecarsDirectFormatsExistingOutputAndSourceOwnership() = runTest(timeout = 120.seconds) { fixtures { paths ->
        val shp = r().tileIndex(paths, driver = "ESRI Shapefile", name = "tiles.shp")
        assertTrue(shp.files.map { it.name }.containsAll(listOf("tiles.shp", "tiles.shx", "tiles.dbf", "tiles.prj")))
        assertEquals(paths, locations(shp))
        val orphan = "/vsimem/tile-index-tests/orphan.dbf"
        r().writeMemoryFile(orphan, byteArrayOf(42))
        assertFails { r().tileIndexTo(paths, "/vsimem/tile-index-tests/orphan.shp", driver = "ESRI Shapefile") }
        assertContentEquals(byteArrayOf(42), r().readMemoryFile(orphan))
        for ((format, extension) in listOf("GPKG" to "gpkg", "GeoJSON" to "geojson", "ESRI Shapefile" to "shp")) {
            val path = datasetOutputPath("tile-index-direct.$extension")
            requireNotNull(r().getDriver(format)).use { driver -> try {
                r().tileIndexTo(paths, path, driver = format)
                r().openDataset(path).use { ds -> requireNotNull(ds.getLayer(0)).use { assertEquals(2L, it.getFeatureCount(true)) } }
                assertFailsWith<GdalException> { r().tileIndexTo(paths, path, driver = format) }
                r().openDataset(path).use { assertEquals(1, it.getLayerCount()) }
            } finally { driver.delete(path) } }
        }
        r().openDataset(paths[0]).use { source ->
            val values = IntArray(12); assertEquals(0, source.readRaster(values)); assertEquals(0, values[5]); assertEquals(77, values.sum())
        }
    } }

    @Test fun virtualDependenciesNoCrsAndAbsoluteMemoryPaths() = runTest(timeout = 120.seconds) { fixtures { paths ->
        val vrt = "/vsimem/tile-index-tests/source.vrt"
        r().writeMemoryFile(vrt, """<VRTDataset rasterXSize="4" rasterYSize="3"><SRS>EPSG:3857</SRS><GeoTransform>100,2,0,50,0,-3</GeoTransform><VRTRasterBand dataType="Byte" band="1"><SimpleSource><SourceFilename relativeToVRT="1">a.tif</SourceFilename><SourceBand>1</SourceBand></SimpleSource></VRTRasterBand></VRTDataset>""".encodeToByteArray())
        assertEquals(listOf(vrt), locations(r().tileIndex(listOf(vrt), absolutePaths = true)))
        r().openDataset(vrt).use { source -> val values = IntArray(12); assertEquals(0, source.readRaster(values)); assertEquals(77, values.sum()) }
        r().deleteMemoryFile(paths[1]); raster(paths[1], crs = null)
        assertFails { r().tileIndex(paths) }
        assertFails { r().tileIndex(listOf(paths[1]), "-t_srs", "EPSG:4326") }
        assertEquals(listOf(paths[1]), locations(r().tileIndex(listOf(paths[1]))))
    } }

    @Test fun cancellationCallbackFailureAndSubsequentOperation() = runTest(timeout = 120.seconds) { fixtures { paths ->
        assertFailsWith<CancellationException> { withGdalProgress({ false }) { r().tileIndex(paths) } }
        val failure = IllegalStateException("tile index callback")
        val error = assertFailsWith<IllegalStateException> { withGdalProgress({ throw failure }) { r().tileIndex(paths) } }
        assertTrue(error === failure || error.cause === failure)
        assertEquals(paths, locations(r().tileIndex(paths)))
        r().openDataset(paths[0]).use { assertEquals(4, it.getRasterXSize()) }
    } }

    @Test fun invalidFilesDirectoriesProvidersOptionsAndFormatsFailExplicitly() = runTest(timeout = 120.seconds) { fixtures { paths ->
        for (sources in listOf(emptyList(), listOf(paths[0], paths[0]), listOf(""), listOf("/vsimem/tile-index-tests"),
            listOf("/vsimem/missing.tif"), listOf("*.tif"), listOf("https://invalid.test/a.tif"),
            listOf("/vsicurl/https://invalid.test/a.tif"), listOf("opfs:/tile-index.tif"), listOf("/vsigdal4k-input/private.tif"),
            listOf("/vsigdal4k-output/private.tif"), listOf("GTIFF_DIR:1:file.tif"), listOf("bad\u0000path"))) {
            assertFails { r().tileIndex(sources) }
        }
        for (args in listOf(listOf("-recursive"), listOf("-overwrite"), listOf("-of", "GPKG"), listOf("-lyr_name", "other"),
            listOf("-tileindex", "other"), listOf("-gti_filename", "other.xml"), listOf("-skip_different_projection"),
            listOf("-t_srs"), listOf("-t_srs", "invalid"), listOf("-src_srs_format", "bad"), listOf("-src_srs_name", "location"), listOf("unexpected.tif"))) {
            assertFails { r().tileIndex(paths, arguments = args) }
        }
        for (format in listOf("MEM", "Memory", "VRT", "GTiff", "missing")) assertFails { r().tileIndex(paths, driver = format) }
        assertFails { r().tileIndex(paths, name = "../bad.gpkg") }
        assertFails { r().tileIndex(paths, layerName = "") }; assertFails { r().tileIndex(paths, locationField = "") }
        assertFails { r().tileIndexTo(paths, "") }
        val bad = "/vsimem/tile-index-tests/vector.geojson"
        r().writeMemoryFile(bad, """{"type":"FeatureCollection","features":[]}""".encodeToByteArray())
        assertFails { r().tileIndex(listOf(bad)) }
        // A source without georeferencing must fail before any output is created.
        requireNotNull(r().getDriver("GTiff")).use { driver -> requireNotNull(driver.create("/vsimem/tile-index-tests/plain.tif", 1, 1, 1, GdalDataType.Byte)).close() }
        assertFails { r().tileIndex(listOf("/vsimem/tile-index-tests/plain.tif")) }
        assertEquals(paths, locations(r().tileIndex(paths)))
    } }
}
