package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.test.runTest
import kotlin.test.*
import kotlin.time.Duration.Companion.seconds

/** Generated into the isolated profile only, including independent consumers. */
class DriverProfileApiTest {
    private val profile = "@PROFILE@"

    @Test fun requiredDriversAndRealDataRoundTrip() = runTest(timeout = 120.seconds) {
        val r = Gdal4k.runtime()
        when (profile) {
            "http" -> r.openDataset("@HTTP_BASE@/required/$datasetOutputPrefix/fixture.tif", DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requireStrongETag = true))).use {
                assertEquals(2048, it.getRasterXSize()); assertEquals(2048, it.getRasterYSize())
            }
            "scientific" -> {
                requireNotNull(r.getDriver("HDF5")).use { assertEquals("HDF5", it.getShortName()) }
                r.openDataset("HDF5:\"@HDF5_PATH@\"://values").use {
                    assertEquals(2, it.getRasterXSize()); assertEquals(2, it.getRasterYSize())
                    assertContentEquals(doubleArrayOf(1.0, 2.0, 3.0, 4.0), it.readRaster())
                }
                requireNotNull(r.getDriver("netCDF")).use { driver ->
                    val path = datasetOutputPath("profile.nc")
                    try {
                        requireNotNull(driver.create(path, 2, 2, 1, GdalDataType.Int16)).use { ds ->
                            requireNotNull(ds.getRasterBand(1)).use { band -> assertEquals(0, band.writeRaster(doubleArrayOf(5.0, 6.0, 7.0, 8.0))) }
                        }
                        r.openDataset(path).use { assertContentEquals(doubleArrayOf(5.0, 6.0, 7.0, 8.0), it.readRaster()) }
                    } finally { assertEquals(0, driver.delete(path)) }
                }
            }
            "parquet" -> {
                requireNotNull(r.getDriver("Parquet")).use { driver ->
                    val path = datasetOutputPath("profile.parquet")
                    try {
                        r.openDataset("input.geojson", """{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"value":42},"geometry":{"type":"Point","coordinates":[3,4]}}]}""".encodeToByteArray()).use { ds ->
                            ds.translateVectorTo(path, "Parquet", emptyList())
                        }
                        r.openDataset(path).use { ds ->
                            requireNotNull(ds.getLayer(0)).use { layer ->
                                assertEquals(1, layer.getFeatureCount(true))
                                requireNotNull(layer.getNextFeature()).use { feature ->
                                    assertEquals(42, feature.getFieldAsInteger("value"))
                                    requireNotNull(feature.getGeometryRef()).use { assertContentEquals(doubleArrayOf(3.0, 4.0, 0.0), it.getPoint(0)) }
                                }
                            }
                        }
                    } finally { assertEquals(0, driver.delete(path)) }
                }
            }
            "postgresql" -> {
                // Dedicated disposable CI service. PG: is also recognized by PostGISRaster; select vector explicitly.
                requireNotNull(r.getDriver("PostgreSQL")).use { assertEquals("PostgreSQL", it.getShortName()) }
                r.openDataset("PG:host=127.0.0.1 port=5432 dbname=gdal4k user=gdal4k password=profile-fixture", DatasetOpenOptions(accessMode = DatasetAccessMode.Update, kind = DatasetKind.Vector)).use { ds ->
                    val table = "profile_$datasetOutputPrefix".replace('-', '_')
                    requireNotNull(ds.createLayer(table, geometryType = 1)).use { layer ->
                        r.createFieldDefn("value", 0).use { assertEquals(0, layer.createField(it)) }
                        r.createFeature(requireNotNull(layer.getLayerDefn())).use { feature ->
                            feature.setField("value", 42)
                            r.createGeometryFromWkt("POINT (3 4)").use { assertEquals(0, feature.setGeometry(it)) }
                            assertEquals(0, layer.createFeature(feature))
                        }
                        assertEquals(0, ds.flushCache())
                        layer.resetReading()
                        requireNotNull(layer.getNextFeature()).use { assertEquals(42, it.getFieldAsInteger("value")) }
                    }
                    // Drop only the table created by this test invocation.
                    val index = (0 until ds.getLayerCount()).first { i -> requireNotNull(ds.getLayer(i)).use { it.getName() == table } }
                    assertEquals(0, ds.deleteLayer(index))
                }
            }
            else -> error("Unknown optional profile")
        }
    }
}
