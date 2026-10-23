package dev.gdal4k

import dev.gdal4k.GdalException
import dev.gdal4k.GdalFiles
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
            "http" -> DriverProfileFixtures.requiredHttp(r)
            "scientific" -> DriverProfileFixtures.scientific(r)
            "parquet" -> DriverProfileFixtures.parquet(r) { _, _ -> }
            "postgresql" -> DriverProfileFixtures.postgresql(r)
            "combined" -> {
                // Preserve the four ordinary required HTTP contexts. Diagnostic calls
                // below use the separate "combined" case and fresh keys instead.
                DriverProfileFixtures.requiredHttp(r)
                combinedRoundTrip(r)
            }
            else -> error("Unknown optional profile")
        }
    }
}

/** One supplied Runtime and fresh owned names per call, including diagnostic rounds. */
internal suspend fun combinedRoundTrip(r: GdalRuntime) = DriverProfileFixtures.combined(r)

private object DriverProfileFixtures {
    private const val postgresqlConnection = "@POSTGRESQL_DSN@"
    private fun token() = kotlin.random.Random.nextLong().toULong().toString(16)

    suspend fun requiredHttp(r: GdalRuntime) {
        r.openDataset("@HTTP_BASE@/required/$datasetOutputPrefix/fixture.tif",
            DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requireStrongETag = true))).use {
            assertEquals(2048, it.getRasterXSize()); assertEquals(2048, it.getRasterYSize())
        }
    }

    suspend fun combined(r: GdalRuntime) {
        val name = "combined-${token()}"
        assertEquals("3120400", r.versionInfo("VERSION_NUM"))
        defaultJxl(r, "$name-before")
        scientific(r, "$name-before")
        // The original HDF5 handle closes before this independent MEM copy is retained.
        val retained = hdf5Copy(r)
        val detached = retained.use { hdf ->
            parquet(r, name) { _, layer ->
                // HDF5/MEM, Parquet and HTTP-backed VRT handles coexist while PG copies
                // the Parquet layer. Closing PG and dropping its table must not steal them.
                combinedHttpLease(r, "$datasetOutputPrefix-$name").use { remote ->
                    val fromPostgresql = postgresql(r, layer, name)
                    verifyPointLayer(layer)
                    verifyHdf5Copy(hdf)
                    verifyHttpPixels(remote)
                    val fromHttp = remote.translate("GTiff", "$name-http.tif", listOf(
                        "-srcwin", "1792", "1792", "16", "16", "-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES"))
                    Pair(fromPostgresql, fromHttp)
                }
            }
        }
        // PG connections/table, Parquet file, HTTP source/VRT and retained MEM copy are
        // all closed/deleted. Only the independent file bytes remain for these reads.
        r.openDataset(detached.first.mainFile().name, detached.first.mainFile().bytes).use { ds ->
            requireNotNull(ds.getLayer(0)).use { verifyPointLayer(it) }
        }
        r.openDataset(detached.second.mainFile().name, detached.second.mainFile().bytes).use { ds ->
            verifyHttpPixels(ds, 0, 0, 1792, 1792)
            assertTrue(r.datasetInfo(ds, DatasetInfoOptions(listOf("-json"))).contains("JXL"))
        }
        defaultJxl(r, "$name-after")
        scientific(r, "$name-after")
    }

    private suspend fun defaultJxl(r: GdalRuntime, name: String) {
        val expected = byteArrayOf(0, 1, 127, -1)
        val files = requireNotNull(r.getDriver("MEM")).use { driver ->
            requireNotNull(driver.create("", 2, 2, 1, GdalDataType.Byte)).use { source ->
                requireNotNull(source.getRasterBand(1)).use { assertEquals(0, it.writeRaster(expected)) }
                listOf("JPEGXL", "GTiff", "COG").map { format ->
                    val options = if (format == "JPEGXL") listOf("-co", "LOSSLESS=YES")
                        else listOf("-co", "COMPRESS=JXL", "-co", "JXL_LOSSLESS=YES")
                    format to source.translate(format, "$name-${format.lowercase()}" + if (format == "JPEGXL") ".jxl" else ".tif", options)
                }
            }
        }
        for ((format, output) in files) r.openDataset(output.mainFile().name, output.mainFile().bytes).use { ds ->
            assertEquals(2, ds.getRasterXSize()); assertEquals(2, ds.getRasterYSize())
            requireNotNull(ds.getRasterBand(1)).use { band ->
                val actual = ByteArray(4); assertEquals(0, band.readRaster(actual)); assertContentEquals(expected, actual)
            }
            if (format != "JPEGXL") assertTrue(r.datasetInfo(ds, DatasetInfoOptions(listOf("-json"))).contains("JXL"))
        }
    }

    suspend fun scientific(r: GdalRuntime, name: String = "profile") {
        requireNotNull(r.getDriver("HDF5")).use { assertEquals("HDF5", it.getShortName()) }
        r.openDataset("HDF5:\"@HDF5_PATH@\"://values").use {
            assertEquals(2, it.getRasterXSize()); assertEquals(2, it.getRasterYSize())
            assertContentEquals(doubleArrayOf(1.0, 2.0, 3.0, 4.0), it.readRaster())
        }
        hdf5Copy(r).use { verifyHdf5Copy(it) }
        ownedFile(r, "netCDF", "$name.nc") { driver, path ->
            requireNotNull(driver.create(path, 2, 2, 1, GdalDataType.Int16)).use { ds ->
                requireNotNull(ds.getRasterBand(1)).use { band ->
                    assertEquals(0, band.writeRaster(doubleArrayOf(5.0, 6.0, 7.0, 8.0)))
                }
            }
            r.openDataset(path).use { assertContentEquals(doubleArrayOf(5.0, 6.0, 7.0, 8.0), it.readRaster()) }
        }
        scientificConversion(r, name)
    }

    private suspend fun hdf5Copy(r: GdalRuntime): Dataset =
        r.openDataset("@HDF5_PATH@", DatasetOpenOptions(kind = DatasetKind.Multidimensional)).use {
            it.multiDimTranslate(emptyList())
        }

    private suspend fun verifyHdf5Copy(copy: Dataset) {
        requireNotNull(copy.getRootGroup()).use { group ->
            verifyArray(group, "values", MDDataType.Numeric(GdalDataType.Int16), longArrayOf(2, 2),
                listOf(1, 2, 3, 4).map { MDValue.Numeric(it) })
        }
    }

    suspend fun <T> parquet(r: GdalRuntime, name: String = "profile", block: suspend (Dataset, Layer) -> T): T =
        ownedFile(r, "Parquet", "$name.parquet") { _, path ->
            r.openDataset("input.geojson", """{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"value":42},"geometry":{"type":"Point","coordinates":[3,4]}}]}""".encodeToByteArray()).use {
                it.translateVectorTo(path, "Parquet", emptyList())
            }
            r.openDataset(path).use { ds -> requireNotNull(ds.getLayer(0)).use { layer ->
                verifyPointLayer(layer)
                val result = block(ds, layer)
                verifyPointLayer(layer)
                result
            } }
        }

    private suspend fun verifyPointLayer(layer: Layer) {
        assertEquals(1, layer.getFeatureCount(true))
        layer.resetReading()
        requireNotNull(layer.getNextFeature()).use { feature ->
            assertEquals(42, feature.getFieldAsInteger("value"))
            requireNotNull(feature.getGeometryRef()).use {
                assertContentEquals(doubleArrayOf(3.0, 4.0, 0.0), it.getPoint(0))
            }
        }
        assertNull(layer.getNextFeature())
    }

    private suspend fun verifyHttpPixels(ds: Dataset, x: Int = 1792, y: Int = 1792, sourceX: Int = x, sourceY: Int = y) {
        requireNotNull(ds.getRasterBand(1)).use { band ->
            val actual = ByteArray(16 * 16)
            assertEquals(0, band.readRaster(BandRasterIOOptions(x, y, 16, 16), actual))
            val expected = ByteArray(actual.size) { i ->
                val px = sourceX + i % 16; val py = sourceY + i / 16
                ((px * 31 + py * 17 + (px xor py)) and 255).toByte()
            }
            assertContentEquals(expected, actual)
        }
    }

    private suspend fun combinedHttpLease(r: GdalRuntime, key: String): Dataset {
        val options = DatasetOpenOptions(httpInputPolicy = HttpInputPolicy(requireStrongETag = true))
        return r.openDataset("@HTTP_BASE@/combined/$key/fixture.tif", options).use { source ->
            assertEquals(2048, source.getRasterXSize()); assertEquals(2048, source.getRasterYSize())
            verifyHttpPixels(source)
            r.openDataset("@HTTPS_BASE@/combined/$key/fixture.tif", options).use { verifyHttpPixels(it) }
            for (origin in listOf("@UNTRUSTED_BASE@", "@WRONG_NAME_BASE@")) {
                assertFailsWith<GdalException> {
                    r.openDataset("$origin/combined/$key/fixture.tif", options).use { verifyHttpPixels(it) }
                }
            }
            // buildVRT retains the HTTP lease after this original Dataset closes.
            r.buildVRT(listOf(source), emptyList())
        }
    }

    private suspend fun layerIndex(ds: Dataset, name: String): Int? {
        for (i in 0 until ds.getLayerCount()) {
            val matches = requireNotNull(ds.getLayer(i)).use { it.getName() == name }
            if (matches) return i
        }
        return null
    }

    suspend fun postgresql(r: GdalRuntime, source: Layer? = null, name: String = "profile"): GdalFiles {
        requireNotNull(r.getDriver("PostgreSQL")).use { assertEquals("PostgreSQL", it.getShortName()) }
        // Fresh, bounded identifiers avoid collisions across diagnostic rounds. No SQL
        // text or broad prefix deletion is used; only this exact absent-before-create name.
        val table = "p_${datasetOutputPrefix}_${token()}".replace('-', '_')
        var ownsName = false
        var committed = false
        var operationFailure: Throwable? = null
        try {
            r.openDataset(postgresqlConnection, DatasetOpenOptions(accessMode = DatasetAccessMode.Update, kind = DatasetKind.Vector)).use { ds ->
                assertNull(layerIndex(ds, table), "Refusing to replace an existing fixture table")
                var transaction = false
                if (source == null) {
                    assertEquals(0, ds.startTransaction())
                    transaction = true
                }
                var writeFailure: Throwable? = null
                try {
                    ownsName = true
                    val created = if (source == null) ds.createLayer(table, geometryType = 1)
                        else {
                            // GDAL CopyLayer owns its target-layer batch transactions.
                            // PG rejects nesting them inside a Dataset user transaction.
                            source.resetReading()
                            ds.copyLayer(source, table)
                        }
                    requireNotNull(created).use { layer ->
                        if (source == null) {
                            r.createFieldDefn("value", 0).use { assertEquals(0, layer.createField(it)) }
                            requireNotNull(layer.getLayerDefn()).use { definition ->
                                r.createFeature(definition).use { feature ->
                                    feature.setField("value", 42)
                                    r.createGeometryFromWkt("POINT (3 4)").use { assertEquals(0, feature.setGeometry(it)) }
                                    assertEquals(0, layer.createFeature(feature))
                                }
                            }
                        }
                        assertEquals(0, layer.syncToDisk())
                        verifyPointLayer(layer)
                    }
                    if (transaction) {
                        assertEquals(0, ds.commitTransaction())
                        transaction = false
                    } else {
                        assertEquals(0, ds.flushCache())
                    }
                    committed = true
                } catch (failure: Throwable) {
                    writeFailure = failure
                    throw failure
                } finally {
                    if (transaction) withContext(NonCancellable) {
                        try { assertEquals(0, ds.rollbackTransaction()) }
                        catch (cleanupFailure: Throwable) {
                            val failure = writeFailure
                            if (failure == null) throw cleanupFailure
                            failure.addSuppressed(cleanupFailure)
                        }
                    }
                }
            }
            // A new connection proves persisted fields and geometry, rather than only
            // the original writer's cursor/cache. Exported bytes outlive table deletion.
            return r.openDataset(postgresqlConnection, DatasetOpenOptions(kind = DatasetKind.Vector)).use { ds ->
                requireNotNull(ds.getLayer(table)).use { verifyPointLayer(it) }
                ds.translateVector("GeoJSON", "$name-pg.geojson", listOf(table))
            }
        } catch (failure: Throwable) {
            operationFailure = failure
            throw failure
        } finally {
            if (ownsName) withContext(NonCancellable) {
                try {
                    // Writer/read connections have closed before cleanup, including a
                    // failed or cancelled transaction. A partial created table is owned too.
                    r.openDataset(postgresqlConnection, DatasetOpenOptions(accessMode = DatasetAccessMode.Update, kind = DatasetKind.Vector)).use { ds ->
                        val index = layerIndex(ds, table)
                        if (committed) assertNotNull(index, "Owned committed table disappeared before cleanup")
                        if (index != null) assertEquals(0, ds.deleteLayer(index))
                        assertEquals(0, ds.flushCache())
                        assertNull(layerIndex(ds, table))
                    }
                    r.openDataset(postgresqlConnection, DatasetOpenOptions(kind = DatasetKind.Vector)).use { assertNull(layerIndex(it, table)) }
                } catch (cleanupFailure: Throwable) {
                    val failure = operationFailure
                    if (failure == null) throw cleanupFailure
                    failure.addSuppressed(cleanupFailure)
                }
            }
        }
    }

    private suspend fun <T> ownedFile(r: GdalRuntime, format: String, name: String, block: suspend (Driver, String) -> T): T {
        val path = datasetOutputPath(name)
        return requireNotNull(r.getDriver(format)).use { driver ->
            var operationFailure: Throwable? = null
            try { block(driver, path) }
            catch (failure: Throwable) { operationFailure = failure; throw failure }
            finally {
                withContext(NonCancellable) {
                    try { assertEquals(0, driver.delete(path)) }
                    catch (cleanupFailure: Throwable) {
                        val failure = operationFailure
                        if (failure == null) throw cleanupFailure
                        failure.addSuppressed(cleanupFailure)
                    }
                }
            }
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

    /** Actual OS NC4 output in scientific and combined host profiles. */
    private suspend fun scientificConversion(r: GdalRuntime, name: String) {
        val signed = listOf(Long.MIN_VALUE.toString(), "-9007199254740993", "9007199254740993", Long.MAX_VALUE.toString()).map { MDValue.Numeric(it) }
        val unsigned = listOf("0", "9007199254740993", "18446744073709551614", "18446744073709551615").map { MDValue.Numeric(it) }
        val floating = listOf("0.25", "-2.5", "3.75", "4.5").map { MDValue.Numeric(it) }
        val complex = listOf(MDValue.Numeric("1.25", "-2.5"), MDValue.Numeric("3.5", "4.75"),
            MDValue.Numeric("-5.25", "6.5"), MDValue.Numeric("7.75", "-8"))
        val text = listOf("", "한글 😀", "scene", "last").map { MDValue.Text(it) }
        val path = datasetOutputPath("$name-md-exact.nc")
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
