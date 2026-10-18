package dev.gdal4k

import dev.gdal4k.GdalFile
import dev.gdal4k.GdalFiles
import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

class GridViewshedApiTest {
    private suspend fun <T> points(block: suspend (Dataset) -> T): T {
        val json = """{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:3857"}},"features":[
            {"type":"Feature","properties":{"value":1},"geometry":{"type":"Point","coordinates":[0.5,1.5,11]}},
            {"type":"Feature","properties":{"value":2},"geometry":{"type":"Point","coordinates":[1.5,1.5,12]}},
            {"type":"Feature","properties":{"value":3},"geometry":{"type":"Point","coordinates":[0.5,0.5,13]}},
            {"type":"Feature","properties":{"value":4},"geometry":{"type":"Point","coordinates":[1.5,0.5,14]}}]}"""
        return Gdal4k.runtime().openDataset(GdalFiles(GdalFile("points.geojson", json.encodeToByteArray()))).use { block(it) }
    }
    private val gridArguments = listOf("-a", "nearest", "-txe", "0", "2", "-tye", "0", "2", "-outsize", "2", "2", "-zfield", "value")
    private suspend fun pixels(files: GdalFiles): DoubleArray = Gdal4k.runtime().openDataset(files).use { it.readRaster() }
    private suspend fun <T> dem(block: suspend (Dataset) -> T): T = requireNotNull(Gdal4k.runtime().getDriver("MEM")).use { driver ->
        requireNotNull(driver.create("", 5, 5, 1, GdalDataType.Float64)).use { ds ->
            ds.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.0, 5.0, 0.0, -1.0))
            Gdal4k.runtime().createSpatialReference("EPSG:3857").use { ds.setSpatialRef(it) }
            requireNotNull(ds.getRasterBand(1)).use { it.fill(0.0) }; block(ds)
        }
    }
    @Test fun gridNearestValuesAndGeoreference() = runTest(timeout = 120.seconds) {
        val result = points { ds -> ds.grid(arguments = gridArguments) }
        assertContentEquals(doubleArrayOf(1.0, 2.0, 3.0, 4.0), pixels(result))
        Gdal4k.runtime().openDataset(result).use { ds ->
            assertContentEquals(doubleArrayOf(0.0, 1.0, 0.0, 2.0, 0.0, -1.0), ds.getGeoTransform())
            assertTrue(ds.getProjection().contains("3857"))
        }
        points { ds ->
            val arguments = gridArguments.filterIndexed { i, _ -> i < gridArguments.size - 2 } + listOf("-a_srs", "EPSG:32631")
            assertContentEquals(doubleArrayOf(11.0, 12.0, 13.0, 14.0), pixels(ds.grid(*arguments.toTypedArray())))
        }
    }
    @Test fun gridDirectOutputAndSelection() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = datasetOutputPath("grid-api.tif")
        requireNotNull(runtime.getDriver("GTiff")).use { driver -> try {
            points { ds ->
                ds.gridTo(path, *(gridArguments + listOf("-ot", "Float32", "-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES")).toTypedArray())
                assertFails { ds.gridTo(path, *gridArguments.toTypedArray()) }
                val selected = ds.grid(arguments = gridArguments + listOf("-where", "value >= 4"))
                assertContentEquals(DoubleArray(4) { 4.0 }, pixels(selected))
            }
            runtime.openDataset(path).use { ds ->
                assertContentEquals(doubleArrayOf(1.0, 2.0, 3.0, 4.0), ds.readRaster())
                assertTrue(ds.info().contains("JXL"))
            }
        } finally { driver.delete(path) } }
    }
    @Test fun viewshedFlatTerrainAndHeightOutputs() = runTest(timeout = 120.seconds) {
        val options = ViewshedOptions(2.5, 2.5, curvatureCoefficient = 0.0)
        val result = dem { ds ->
            for (mode in ViewshedMode.entries) assertContentEquals(DoubleArray(25) { 255.0 }, pixels(ds.viewshed(options.copy(mode = mode))))
            ds.viewshed(options)
        }
        assertContentEquals(DoubleArray(25) { 255.0 }, pixels(result))
        Gdal4k.runtime().openDataset(result).use { ds ->
            assertEquals(5, ds.getRasterXSize()); assertEquals(5, ds.getRasterYSize())
            assertTrue(ds.getProjection().contains("3857"))
            requireNotNull(ds.getRasterBand(1)).use { assertEquals(GdalDataType.Byte, it.getRasterDataType()); assertNull(it.getNoDataValue()) }
        }
        dem { ds ->
            val limited = ds.viewshed(options.copy(output = ViewshedOutput.MinimumHeightFromGround,
                maxDistance = 2.0, outOfRangeValue = -99.0))
            assertEquals(-99.0, pixels(limited)[0])
        }
        dem { ds -> for (mode in listOf(ViewshedOutput.MinimumHeightFromDem, ViewshedOutput.MinimumHeightFromGround)) {
            val height = ds.viewshed(options.copy(output = mode))
            Gdal4k.runtime().openDataset(height).use { out ->
                requireNotNull(out.getRasterBand(1)).use { assertEquals(GdalDataType.Float64, it.getRasterDataType()) }
                assertTrue(out.readRaster().all { it.isFinite() && it >= 0.0 })
            }
        } }
    }
    @Test fun viewshedOcclusionAndDirectOutput() = runTest(timeout = 120.seconds) {
        val runtime = Gdal4k.runtime(); val path = datasetOutputPath("viewshed-api.tif")
        requireNotNull(runtime.getDriver("GTiff")).use { driver -> try {
            dem { ds ->
                requireNotNull(ds.getRasterBand(1)).use { band ->
                    band.writeRaster(DoubleArray(25) { if (it % 5 == 2) 100.0 else 0.0 })
                }
                val options = ViewshedOptions(0.5, 2.5, curvatureCoefficient = 0.0, visibleValue = 1.0, invisibleValue = 2.0, noDataValue = 9.0)
                ds.viewshedTo(path, options, "COMPRESS=JXL", "JXL_LOSSLESS=YES")
                assertFails { ds.viewshedTo(path, options) }
                val small = ds.viewshed(options.copy(maxDistance = 1.0))
                runtime.openDataset(small).use { assertTrue(it.getRasterXSize() < 5) }
            }
            runtime.openDataset(path).use { ds ->
                val data = ds.readRaster(); assertEquals(1.0, data[10]); assertEquals(2.0, data[14])
                requireNotNull(ds.getRasterBand(1)).use { assertEquals(9.0, it.getNoDataValue()) }
                assertTrue(ds.info().contains("JXL"))
            }
        } finally { driver.delete(path) } }
    }
    @Test fun invalidArgumentsAndSourceTypes() = runTest(timeout = 120.seconds) {
        points { ds ->
            for (driver in listOf("MEM", "VRT", "stream", "missing")) assertFails { ds.grid(driver = driver, arguments = gridArguments) }
            assertFails { ds.grid("-a", "missing") }; assertFails { ds.gridTo("", *gridArguments.toTypedArray()) }
            assertFails { ds.grid(name = "../bad.tif", arguments = gridArguments) }
            assertFails { ds.viewshed(ViewshedOptions(0.5, 0.5)) }
            ds.close(); assertFails { ds.grid(arguments = gridArguments) }
        }
        dem { ds ->
            val options = ViewshedOptions(2.5, 2.5)
            for (invalid in listOf(options.copy(observerX = Double.NaN), options.copy(observerHeight = Double.POSITIVE_INFINITY), options.copy(maxDistance = -1.0), options.copy(visibleValue = 256.0), options.copy(noDataValue = 300.0))) assertFails { ds.viewshed(invalid) }
            assertFails { ds.viewshed(options, band = 0) }; assertFails { ds.viewshed(options, driver = "COG") }
            assertFails { ds.viewshed(options, "BAD") }; assertFails { ds.viewshedTo("", options) }
            assertFails { ds.grid(arguments = gridArguments) }
            Gdal4k.runtime().createSpatialReference("EPSG:4326").use { ds.setSpatialRef(it) }
            assertFails { ds.viewshed(options) }
            Gdal4k.runtime().createSpatialReference("EPSG:3857").use { ds.setSpatialRef(it) }
            ds.setGeoTransform(doubleArrayOf(0.0, 1.0, 0.2, 5.0, 0.0, -1.0)); assertFails { ds.viewshed(options) }
        }
    }
}
