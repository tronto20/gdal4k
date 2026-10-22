package dev.gdal4k

import dev.gdal4k.runtime.*
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
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
                // HDF5 is read from the real, already closed h5py fixture. The MEM result
                // must remain readable after its source dataset has closed.
                val hdfCopy = r.openDataset("@HDF5_PATH@", DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use {
                    it.multiDimTranslate(emptyList())
                }
                hdfCopy.use { copy -> requireNotNull(copy.getRootGroup()).use { group ->
                    verifyArray(group, "values", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2, 2),
                        listOf(1, 2, 3, 4).map { MDValue.Numeric(it) })
                } }
                requireNotNull(r.getDriver("netCDF")).use { driver ->
                    val path = datasetOutputPath("profile.nc")
                    try {
                        requireNotNull(driver.create(path, 2, 2, 1, GdalDataType.Int16)).use { ds ->
                            requireNotNull(ds.getRasterBand(1)).use { band -> assertEquals(0, band.writeRaster(doubleArrayOf(5.0, 6.0, 7.0, 8.0))) }
                        }
                        r.openDataset(path).use { assertContentEquals(doubleArrayOf(5.0, 6.0, 7.0, 8.0), it.readRaster()) }
                    } finally { assertEquals(0, driver.delete(path)) }
                }
                scientificConversion(r)
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

    private suspend fun verifyArray(group: Group, name: String, type: MDDataType, shape: LongArray, values: List<MDValue>) {
        requireNotNull(group.openArray(name)).use { array ->
            assertEquals(type, array.getExtendedDataType())
            val actualShape = mutableListOf<Long>()
            for (dimension in array.getDimensions()) dimension.use { actualShape.add(it.getSize()) }
            assertContentEquals(shape, actualShape.toLongArray())
            assertEquals(values, array.readValues(LongArray(shape.size), shape))
        }
    }

    /** Actual OS NC4 output, used only by the two scientific profile host jobs. */
    private suspend fun scientificConversion(r: GdalRuntime) {
        val signed = listOf(Long.MIN_VALUE.toString(), "-9007199254740993", "9007199254740993", Long.MAX_VALUE.toString()).map { MDValue.Numeric(it) }
        val unsigned = listOf("0", "9007199254740993", "18446744073709551614", "18446744073709551615").map { MDValue.Numeric(it) }
        val floating = listOf("0.25", "-2.5", "3.75", "4.5").map { MDValue.Numeric(it) }
        val complex = listOf(MDValue.Numeric("1.25", "-2.5"), MDValue.Numeric("3.5", "4.75"),
            MDValue.Numeric("-5.25", "6.5"), MDValue.Numeric("7.75", "-8"))
        val text = listOf("", "한글 😀", "scene", "last").map { MDValue.Text(it) }
        val path = datasetOutputPath("profile-md-exact.nc")
        val schema = listOf("signed" to MDDataType.Numeric(GdalDataType.Int64),
            "unsigned" to MDDataType.Numeric(GdalDataType.UInt64), "floating" to MDDataType.Numeric(GdalDataType.Float64),
            "complex" to MDDataType.Numeric(GdalDataType.CFloat64), "text" to MDDataType.Text())
        val data: Map<String, List<MDValue>> = mapOf("signed" to signed, "unsigned" to unsigned, "floating" to floating,
            "complex" to complex, "text" to text)
        requireNotNull(r.getDriver("netCDF")).use { nc ->
            var completed = false
            var conversionFailure: Throwable? = null
            try {
                requireNotNull(r.getDriver("MEM")).use { driver ->
                    requireNotNull(driver.createMultidimensional("")).use { source ->
                        requireNotNull(source.getRootGroup()).use { group ->
                            val dimension = requireNotNull(group.createDimension("n", 4))
                            dimension.use {
                                for ((name, type) in schema) requireNotNull(group.createArray(name, listOf(dimension), type)).use { array ->
                                    assertTrue(array.writeValues(longArrayOf(0), longArrayOf(4), data.getValue(name)))
                                    when (name) {
                                        "signed" -> assertTrue(array.setNoDataValue(MDValue.Numeric(Long.MIN_VALUE)))
                                        "unsigned" -> assertTrue(array.setNoDataValue(unsigned.last()))
                                        "complex" -> assertTrue(array.setNoDataValue(complex.first()))
                                        "floating" -> {
                                            assertTrue(array.setNoDataValue(MDValue.Numeric("-9999.25")))
                                            assertTrue(array.setUnit("K")); assertTrue(array.setScale(0.5)); assertTrue(array.setOffset(273.15))
                                            requireNotNull(array.createAttribute("ids", longArrayOf(2), MDDataType.Numeric(GdalDataType.UInt64))).use { attribute ->
                                                assertTrue(attribute.writeValues(listOf(unsigned[1], unsigned[3])))
                                            }
                                        }
                                    }
                                }
                            }
                            requireNotNull(group.createAttribute("marker", longArrayOf(), MDDataType.Text())).use { attribute ->
                                assertTrue(attribute.writeValues(listOf(MDValue.Text("MD conversion 영상"))))
                            }
                        }
                        source.multiDimTranslateTo(path, driver = "netCDF", arguments = listOf("-co", "FORMAT=NC4"))
                        completed = true
                    }
                }
                // The native writer and original MEM dataset are both closed before readback.
                val copy = r.openDataset(path, DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use { source ->
                    requireNotNull(source.getRootGroup()).use { group ->
                        for ((name, type) in schema) verifyArray(group, name, type, longArrayOf(4), data.getValue(name))
                    }
                    source.multiDimTranslate(emptyList())
                }
                copy.use { result -> requireNotNull(result.getRootGroup()).use { group ->
                    for ((name, type) in schema) verifyArray(group, name, type, longArrayOf(4), data.getValue(name))
                    requireNotNull(group.getAttribute("marker")).use { assertEquals(listOf(MDValue.Text("MD conversion 영상")), it.readValues()) }
                    requireNotNull(group.openArray("signed")).use { assertEquals(MDValue.Numeric(Long.MIN_VALUE), it.getNoDataValue()) }
                    requireNotNull(group.openArray("unsigned")).use { assertEquals(unsigned.last(), it.getNoDataValue()) }
                    requireNotNull(group.openArray("complex")).use { assertEquals(complex.first(), it.getNoDataValue()) }
                    requireNotNull(group.openArray("floating")).use { array ->
                        assertEquals(MDValue.Numeric("-9999.25"), array.getNoDataValue())
                        assertEquals("K", array.getUnit()); assertEquals(0.5, array.getScale()); assertEquals(273.15, array.getOffset())
                        requireNotNull(array.getAttribute("ids")).use { assertEquals(listOf(unsigned[1], unsigned[3]), it.readValues()) }
                    }
                } }
            } catch (failure: Throwable) {
                conversionFailure = failure
                throw failure
            } finally {
                withContext(NonCancellable) {
                    try {
                        val deleted = nc.delete(path)
                        if (completed) assertEquals(0, deleted)
                    } catch (cleanupFailure: Throwable) {
                        val failure = conversionFailure
                        if (failure == null) throw cleanupFailure
                        failure.addSuppressed(cleanupFailure)
                    }
                }
            }
        }
    }
}
